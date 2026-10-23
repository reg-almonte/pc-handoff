"""The poller thread: schedule, skips, no overlap, resilience (TASK-009 AC-4 to AC-7).

The poller runs on the real monotonic clock (``SystemClock``) with short intervals; the
runtime uses a ``FakeClock`` for detection times unless a test needs real time (AC-6).
"""

import json
import logging
import random
import threading
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from pc_handoff.app_logging import LOGGER_NAME
from pc_handoff.clock import SystemClock
from pc_handoff.domain.models import PcStatus
from pc_handoff.launcher import buildRuntime
from pc_handoff.runtime.app_runtime import AppRuntime
from pc_handoff.runtime.poller import (
    SKIPPED_BUSY_ENUMERATION,
    SKIPPED_POLL_RUNNING,
    THREAD_NAME,
    Poller,
)
from tests.conftest import FAKE_CLOCK_START
from tests.fakes import FakeClock, FakeSessionProvider
from tests.fakes.builders import osSession

POLLER_LOGGER = logging.getLogger(f"{LOGGER_NAME}.runtime.poller")


class ConcurrencyCountingProvider(FakeSessionProvider):
    """A fake whose every call takes ``callSeconds``; records the peak of concurrent calls."""

    def __init__(self, sessions, callSeconds: float = 0.0) -> None:
        super().__init__(sessions)
        self.callSeconds = callSeconds
        self.countLock = threading.Lock()
        self.active = 0
        self.peak = 0

    def enumerateSessions(self):
        with self.countLock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(self.callSeconds)
            return super().enumerateSessions()
        finally:
            with self.countLock:
                self.active -= 1


@pytest.fixture
def dataDir(tmp_path: Path) -> Path:
    directory = tmp_path / "data"
    directory.mkdir()
    return directory


@pytest.fixture
def pollers():
    """Pollers created by a test, always stopped afterwards."""
    created: list[Poller] = []
    yield created
    for poller in created:
        poller.stop()


def startPoller(pollers: list[Poller], runtime: AppRuntime, interval: float) -> Poller:
    poller = Poller(runtime, runtime.source, interval, SystemClock(), POLLER_LOGGER)
    pollers.append(poller)
    poller.start()
    return poller


def waitUntil(predicate: Callable[[], bool], timeout: float, step: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


def guardPollOnce(runtime: AppRuntime) -> dict[str, int]:
    """Wrap ``pollOnce`` with a concurrency counter (in addition to the poller's guard)."""
    stats = {"active": 0, "peak": 0, "calls": 0}
    lock = threading.Lock()
    original = runtime.pollOnce

    def counted(traceId=None):
        with lock:
            stats["active"] += 1
            stats["calls"] += 1
            stats["peak"] = max(stats["peak"], stats["active"])
        try:
            return original(traceId)
        finally:
            with lock:
                stats["active"] -= 1

    runtime.pollOnce = counted  # type: ignore[method-assign]
    return stats


def bob(**overrides: Any):
    overrides.setdefault("connectTime", FAKE_CLOCK_START - timedelta(minutes=30))
    return osSession(3, "bob", **overrides)


# --- AC-4: one poller, no overlap, readers never wait ------------------------------------


def runBlockedPoller(
    dataDir: Path, pollers: list[Poller], readers: int, seconds: float = 3.0
) -> dict[str, Any]:
    provider = ConcurrencyCountingProvider([bob()], callSeconds=1.0)
    runtime = buildRuntime(
        dataDir, provider, FakeClock(FAKE_CLOCK_START), enumerationTimeoutSeconds=0.5
    )
    stats = guardPollOnce(runtime)
    readTimes: list[float] = []
    readLock = threading.Lock()
    stop = threading.Event()

    def read() -> None:
        while not stop.is_set():
            started = time.perf_counter()
            snapshot = runtime.stateStore.current()
            elapsed = time.perf_counter() - started
            assert snapshot is not None
            with readLock:
                readTimes.append(elapsed)
            time.sleep(0.001)  # a viewer re-reads on a timer; it does not spin on the GIL

    threads = [threading.Thread(target=read) for _ in range(readers)]
    for thread in threads:
        thread.start()
    poller = startPoller(pollers, runtime, 0.2)
    time.sleep(seconds)
    stop.set()
    for thread in threads:
        thread.join()
    assert poller.stop()
    # Let abandoned workers finish so they cannot log into a later test.
    assert waitUntil(lambda: provider.active == 0, 2)
    return {
        "polls": poller.pollCount,
        "provider": provider,
        "stats": stats,
        "poller": poller,
        "maxRead": max(readTimes, default=0.0),
        "reads": len(readTimes),
    }


class TestAc04SinglePollerNoOverlap:
    def test_blocked_enumeration_skips_ticks_and_never_overlaps(
        self, tmp_path, pollers, capturedAppLog
    ):
        result = runBlockedPoller(tmp_path / "five", pollers, readers=5)
        provider, stats, poller = result["provider"], result["stats"], result["poller"]

        assert stats["peak"] == 1  # never two pollOnce at once
        assert poller.refusedCount == 0  # the non-blocking guard was never contended
        assert provider.peak == 1  # never two enumerations at once (incl. abandoned ones)
        assert result["polls"] >= 2
        assert result["reads"] > 500
        assert result["maxRead"] < 0.05

        messages = [line["message"] for line in capturedAppLog()]
        assert SKIPPED_POLL_RUNNING in messages
        assert SKIPPED_BUSY_ENUMERATION in messages
        skipIds = [
            line["traceId"]
            for line in capturedAppLog()
            if line["message"] in (SKIPPED_POLL_RUNNING, SKIPPED_BUSY_ENUMERATION)
        ]
        assert len(skipIds) == len(set(skipIds)) == poller.skippedCount
        assert all(isinstance(t, str) and len(t) == 32 for t in skipIds)

    def test_poll_count_does_not_depend_on_the_number_of_readers(self, tmp_path, pollers):
        withReaders = runBlockedPoller(tmp_path / "five", pollers, readers=5)
        withoutReaders = runBlockedPoller(tmp_path / "none", pollers, readers=0)
        assert abs(withReaders["polls"] - withoutReaders["polls"]) <= 1

    def test_an_abandoned_worker_logs_with_its_polls_trace_id(
        self, dataDir, pollers, capturedAppLog
    ):
        provider = ConcurrencyCountingProvider([bob()], callSeconds=0.6)
        runtime = buildRuntime(
            dataDir, provider, FakeClock(FAKE_CLOCK_START), enumerationTimeoutSeconds=0.2
        )
        poller = startPoller(pollers, runtime, 5.0)  # only tick 0 within the test
        message = "abandoned session enumeration finished after timeout"

        def lateLines() -> list[dict]:
            failed = [
                line for line in capturedAppLog() if line["message"] == "session enumeration failed"
            ]
            ids = {line["traceId"] for line in failed}
            assert all(line["detail"]["kind"] == "timeout" for line in failed)
            return [
                line
                for line in capturedAppLog()
                if line["message"] == message and line["traceId"] in ids
            ]

        assert waitUntil(lambda: len(lateLines()) == 1, 3)
        poller.stop()
        assert waitUntil(lambda: provider.active == 0, 2)

    def test_the_guard_refuses_a_concurrent_poll(self, dataDir):
        runtime = buildRuntime(dataDir, FakeSessionProvider([]), FakeClock(FAKE_CLOCK_START))
        release = threading.Event()
        entered = threading.Event()

        def blockingPoll(traceId=None):
            entered.set()
            release.wait(5)
            return "ok"

        runtime.pollOnce = blockingPoll  # type: ignore[method-assign]
        poller = Poller(runtime, runtime.source, 1.0, SystemClock(), POLLER_LOGGER)
        first = threading.Thread(target=poller.runTick)
        first.start()
        assert entered.wait(5)
        poller.runTick()  # a second caller while the first poll runs
        release.set()
        first.join(5)
        assert (poller.pollCount, poller.refusedCount) == (1, 1)


# --- AC-5: reads never block on a blocked enumeration ------------------------------------


class TestAc05ReadsDuringABlockedEnumeration:
    def test_current_returns_the_last_snapshot_within_50_ms(self, dataDir, pollers):
        provider = FakeSessionProvider([bob()])
        runtime = buildRuntime(dataDir, provider, FakeClock(FAKE_CLOCK_START))  # 3 s timeout
        runtime.pollOnce()
        published = runtime.stateStore.current()
        gate = threading.Event()
        provider.blockNext(gate)
        calls = provider.callCount
        poller = startPoller(pollers, runtime, 10.0)
        try:
            assert waitUntil(lambda: provider.callCount > calls, 2)
            for _ in range(20):
                started = time.perf_counter()
                snapshot = runtime.stateStore.current()
                assert time.perf_counter() - started < 0.05
                assert snapshot is published
        finally:
            gate.set()
        assert waitUntil(lambda: runtime.stateStore.current() is not published, 3)
        assert poller.stop()


# --- AC-6: changes reach status.json within the interval plus 2 s ------------------------


@pytest.mark.slow
class TestAc06ProductionInterval:
    def test_a_logoff_reaches_status_json_within_seven_seconds(self, dataDir, pollers):
        clock = SystemClock()
        now = clock.now()
        provider = FakeSessionProvider([bob(connectTime=now - timedelta(minutes=5))])
        runtime = buildRuntime(dataDir, provider, clock)
        statusPath = dataDir / "status.json"

        def status() -> str | None:
            try:
                return json.loads(statusPath.read_text(encoding="utf-8"))["status"]
            except (OSError, ValueError):
                return None

        startPoller(pollers, runtime, runtime.config.sessionPollIntervalSeconds)
        assert runtime.config.sessionPollIntervalSeconds == 5
        assert waitUntil(lambda: status() == "In Use", 3)
        time.sleep(random.uniform(0.1, 4.9))  # a random moment between ticks
        provider.setSessions([])  # bob logs off
        changedAt = time.monotonic()
        assert waitUntil(lambda: status() == "Available", 8, step=0.05)
        assert time.monotonic() - changedAt <= 7.0


# --- AC-7: an unexpected exception never ends the poller ---------------------------------


class TestAc07UnexpectedExceptions:
    def test_each_failing_tick_logs_an_error_and_the_poller_survives(
        self, dataDir, pollers, capturedAppLog, monkeypatch
    ):
        runtime = buildRuntime(dataDir, FakeSessionProvider([bob()]), FakeClock(FAKE_CLOCK_START))
        runtime.pollOnce()  # reconciled and healthy
        assert runtime.stateStore.current().status is PcStatus.IN_USE

        def broken(*args, **kwargs):
            raise RuntimeError("tracker bug")

        monkeypatch.setattr(runtime.tracker, "applySuccess", broken)
        poller = startPoller(pollers, runtime, 0.2)

        def errorLines() -> list[dict]:
            return [
                line
                for line in capturedAppLog()
                if line["message"] == "poll failed with an unexpected error"
            ]

        assert waitUntil(lambda: len(errorLines()) >= 3, 5)
        errors = errorLines()
        assert all(line["level"] == "ERROR" for line in errors)
        assert all("tracker bug" in line["detail"] for line in errors)

        def completed() -> dict[str, str]:
            return {
                line["traceId"]: line["detail"]["outcome"]
                for line in capturedAppLog()
                if line["message"] == "poll completed"
            }

        assert waitUntil(lambda: all(line["traceId"] in completed() for line in errors), 3)
        assert all(completed()[line["traceId"]] == "error" for line in errors)
        assert len({line["traceId"] for line in errors}) == len(errors)
        assert runtime.stateStore.current().status is PcStatus.UNKNOWN
        assert poller.isAlive()

        monkeypatch.undo()
        assert waitUntil(lambda: runtime.stateStore.current().status is PcStatus.IN_USE, 3)
        assert runtime.stateStore.current().detectionHealthy is True
        assert poller.isAlive()

    def test_failing_enumerations_keep_the_thread_alive(self, dataDir, pollers, capturedAppLog):
        provider = FakeSessionProvider([bob()])
        provider.failNext("access_denied", times=1000)
        runtime = buildRuntime(dataDir, provider, FakeClock(FAKE_CLOCK_START))
        poller = startPoller(pollers, runtime, 0.1)
        assert waitUntil(lambda: poller.pollCount >= 3, 5)
        assert poller.isAlive()
        assert poller.thread.name == THREAD_NAME
        assert poller.thread.daemon is True
        outcomes = [
            line["detail"]["outcome"]
            for line in capturedAppLog()
            if line["message"] == "poll completed"
        ]
        assert outcomes and set(outcomes) == {"enumeration_failed"}

    def test_an_exception_escaping_poll_once_is_contained_by_the_loop(
        self, dataDir, pollers, capturedAppLog
    ):
        runtime = buildRuntime(dataDir, FakeSessionProvider([]), FakeClock(FAKE_CLOCK_START))
        calls = {"n": 0}

        def escaping(traceId=None):
            calls["n"] += 1
            raise RuntimeError("escaped")

        runtime.pollOnce = escaping  # type: ignore[method-assign]
        poller = startPoller(pollers, runtime, 0.1)
        assert waitUntil(lambda: calls["n"] >= 3, 5)
        assert poller.isAlive()
        loopErrors = [
            line
            for line in capturedAppLog()
            if line["message"] == "poller loop error; polling continues"
        ]
        assert len(loopErrors) >= 3
        assert poller.pollGuard.locked() is False


# --- schedule and lifecycle ---------------------------------------------------------------


class TestScheduleAndLifecycle:
    def test_the_first_poll_runs_immediately_then_at_the_interval(self, dataDir, pollers):
        runtime = buildRuntime(dataDir, FakeSessionProvider([]), FakeClock(FAKE_CLOCK_START))
        times: list[float] = []
        original = runtime.pollOnce

        def timed(traceId=None):
            times.append(time.monotonic())
            return original(traceId)

        runtime.pollOnce = timed  # type: ignore[method-assign]
        started = time.monotonic()
        poller = startPoller(pollers, runtime, 0.3)
        assert waitUntil(lambda: len(times) >= 4, 5)
        poller.stop()
        assert times[0] - started < 0.25
        offsets = [t - times[0] for t in times[:4]]
        for k, offset in enumerate(offsets):
            assert offset == pytest.approx(0.3 * k, abs=0.12)
        assert poller.skippedCount == 0

    def test_a_slow_poll_skips_the_ticks_that_came_due(self, dataDir, pollers, capturedAppLog):
        runtime = buildRuntime(dataDir, FakeSessionProvider([]), FakeClock(FAKE_CLOCK_START))
        calls = {"n": 0}
        original = runtime.pollOnce

        def slowFirst(traceId=None):
            calls["n"] += 1
            if calls["n"] == 1:
                time.sleep(0.9)  # ticks 1 and 2 (0.4 s, 0.8 s) come due; tick 3 is at 1.2 s
            return original(traceId)

        runtime.pollOnce = slowFirst  # type: ignore[method-assign]
        poller = startPoller(pollers, runtime, 0.4)
        assert waitUntil(lambda: calls["n"] >= 2, 3)
        poller.stop()
        skipped = [line for line in capturedAppLog() if line["message"] == SKIPPED_POLL_RUNNING]
        assert len(skipped) == 2

    def test_stop_is_prompt_while_waiting(self, dataDir, pollers):
        runtime = buildRuntime(dataDir, FakeSessionProvider([]), FakeClock(FAKE_CLOCK_START))
        poller = startPoller(pollers, runtime, 60.0)
        assert waitUntil(lambda: poller.pollCount == 1, 3)
        started = time.monotonic()
        assert poller.stop() is True
        assert time.monotonic() - started < 1.0
        assert not poller.isAlive()

    def test_stop_reports_a_poll_that_does_not_finish_in_time(
        self, dataDir, pollers, capturedAppLog
    ):
        runtime = buildRuntime(dataDir, FakeSessionProvider([]), FakeClock(FAKE_CLOCK_START))
        release = threading.Event()
        runtime.pollOnce = lambda traceId=None: release.wait(5)  # type: ignore[method-assign]
        poller = startPoller(pollers, runtime, 1.0)
        assert waitUntil(lambda: poller.pollCount == 1, 3)
        assert poller.stop(timeout=0.1) is False
        assert any(
            line["message"] == "poller did not stop within 0.1 s" for line in capturedAppLog()
        )
        release.set()
        assert poller.stop() is True

    def test_stop_before_start_is_harmless(self, dataDir):
        runtime = buildRuntime(dataDir, FakeSessionProvider([]), FakeClock(FAKE_CLOCK_START))
        assert Poller(runtime, runtime.source, 1.0, SystemClock(), POLLER_LOGGER).stop() is True

    @pytest.mark.parametrize("interval", [0, -1])
    def test_the_interval_must_be_positive(self, dataDir, interval):
        runtime = buildRuntime(dataDir, FakeSessionProvider([]), FakeClock(FAKE_CLOCK_START))
        with pytest.raises(ValueError):
            Poller(runtime, runtime.source, interval, SystemClock(), POLLER_LOGGER)
