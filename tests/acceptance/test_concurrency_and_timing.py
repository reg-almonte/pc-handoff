"""REQ-001 end to end: concurrent viewers and timing targets (TASK-011; REQ AC-21, AC-13).

REQ AC-13 (LAN reachability) is manual only: ``test_ac13`` is skipped and points to
``docs/manual-acceptance.md``.

Real time throughout: the production poller on ``SystemClock`` with the default
``sessionPollIntervalSeconds`` (5 s) and the production enumeration timeout (3 s). The
p95 poll-cycle budget (<= 1 s over 100 polls at 10,000 events) is the TASK-006 benchmark in
``tests/perf`` (``pytest -m perf --no-cov``) and a shared-PC step of
``docs/manual-acceptance.md``; it is not measured here.
"""

import json
import logging
import random
import threading
import time

import pytest
from streamlit.testing.v1 import AppTest

from pc_handoff.app_logging import LOGGER_NAME
from pc_handoff.clock import SystemClock
from pc_handoff.detection.timed_source import ENUMERATION_TIMEOUT_SECONDS
from pc_handoff.runtime import registry
from pc_handoff.runtime.poller import SKIPPED_BUSY_ENUMERATION, Poller
from tests.acceptance.conftest import (
    APPTEST_TIMEOUT_SECONDS,
    DASHBOARD_SCRIPT,
    MANUAL_REASON,
    AcceptanceApp,
    pageFrom,
)
from tests.fakes.builders import osSession

VIEWERS = 5
POLLER_LOGGER = logging.getLogger(f"{LOGGER_NAME}.runtime.poller")


def waitUntil(predicate, timeout: float, step: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


class Viewers:
    """``VIEWERS`` dashboard sessions open at once, re-rendered continuously on one thread
    while the poller runs on its own."""

    def __init__(self) -> None:
        self.sessions = [
            AppTest.from_file(DASHBOARD_SCRIPT, default_timeout=APPTEST_TIMEOUT_SECONDS).run()
            for _ in range(VIEWERS)
        ]
        self.renders = 0
        self.slowest = 0.0
        self.errors: list[BaseException] = []
        self.stopEvent = threading.Event()
        self.thread = threading.Thread(target=self.loop, name="acceptance-viewers")

    def loop(self) -> None:
        while not self.stopEvent.is_set():
            for session in self.sessions:
                started = time.monotonic()
                try:
                    session.run()
                    pageFrom(session)
                except BaseException as exc:  # reported by the test, never lost
                    self.errors.append(exc)
                    return
                self.slowest = max(self.slowest, time.monotonic() - started)
                self.renders += 1

    def __enter__(self) -> "Viewers":
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stopEvent.set()
        self.thread.join(APPTEST_TIMEOUT_SECONDS)


@pytest.mark.slow
def test_ac21_concurrent_viewers_never_duplicate_detection_and_timing_targets_hold(
    tmp_path, checkedDataDirs
):
    dataDir = tmp_path / "data"
    checkedDataDirs.append(dataDir)
    clock = SystemClock()
    app = AcceptanceApp(dataDir, clock=clock).start()  # default config, production timeout
    runtime = app.runtime
    assert (app.interval, runtime.source.timeoutSeconds) == (5, ENUMERATION_TIMEOUT_SECONDS)
    assert ENUMERATION_TIMEOUT_SECONDS == 3
    statusPath = dataDir / "status.json"

    def status() -> str | None:
        try:
            return json.loads(statusPath.read_text(encoding="utf-8"))["status"]
        except (OSError, ValueError):
            return None

    peak = {"active": 0, "max": 0}
    peakLock = threading.Lock()
    realPollOnce = runtime.pollOnce

    def countedPollOnce(traceId=None):
        with peakLock:
            peak["active"] += 1
            peak["max"] = max(peak["max"], peak["active"])
        try:
            return realPollOnce(traceId)
        finally:
            with peakLock:
                peak["active"] -= 1

    runtime.pollOnce = countedPollOnce
    poller = Poller(runtime, runtime.source, app.interval, clock, POLLER_LOGGER)
    poller.start()
    registry.registerRuntime(runtime)
    try:
        assert waitUntil(lambda: status() == "Available", 3)
        connectA = clock.now()
        script = [
            ("logon", [osSession(2, "alice", connectTime=connectA)], "In Use"),
            ("disconnect", None, "Available"),
            ("reconnect", None, "In Use"),
            ("logoff", [], "Available"),
        ]
        delays: dict[str, float] = {}
        with Viewers() as viewers:
            for step, sessions, expected in script:
                time.sleep(random.uniform(0.1, 1.0))  # a random moment between polls
                now = clock.now()
                if step == "disconnect":
                    sessions = [
                        osSession(
                            2, "alice", "Disconnected", connectTime=connectA, disconnectTime=now
                        )
                    ]
                elif step == "reconnect":
                    sessions = [osSession(2, "alice", connectTime=now, disconnectTime=now)]
                app.setSessions(*sessions)
                changedAt = time.monotonic()
                assert waitUntil(lambda e=expected: status() == e, 8, step=0.02), step
                delays[step] = time.monotonic() - changedAt
                if step != "logoff":
                    # Steps at least 2 poll cycles apart: one more poll sees the same state.
                    polled = poller.pollCount
                    assert waitUntil(lambda p=polled: poller.pollCount > p, 7)
        assert viewers.errors == []
        assert viewers.renders >= VIEWERS

        # Each change reaches status.json within sessionPollIntervalSeconds + 2 s (<= 7 s).
        assert all(delay <= app.interval + 2 for delay in delays.values()), delays
        events = app.events()
        assert [e["eventType"] for e in events] == [
            "session_start",
            "session_end",
            "session_start",
            "session_end",
        ]
        assert len({e["usagePeriodId"] for e in events}) == 2
        # Viewers never triggered an enumeration: one per poll, whatever the number of viewers.
        assert app.provider.callCount == poller.pollCount
        assert viewers.slowest < 3, viewers.slowest

        # A blocked enumeration: a page request still completes within 3 s, shows the last
        # persisted status, and no overlapping poll is started.
        gate = threading.Event()
        calls = app.provider.callCount
        app.provider.blockNext(gate)
        try:
            assert waitUntil(lambda: app.provider.callCount > calls, 7)
            persisted = status()
            started = time.monotonic()
            page = pageFrom(
                AppTest.from_file(DASHBOARD_SCRIPT, default_timeout=APPTEST_TIMEOUT_SECONDS).run()
            )
            assert time.monotonic() - started < 3
            assert page.badge == persisted == "Available"
            assert waitUntil(
                lambda: any(line["message"] == SKIPPED_BUSY_ENUMERATION for line in app.appLog()),
                12,
            ), "no tick came due while the enumeration was blocked"
            assert app.provider.callCount == calls + 1  # nothing overlapped it
        finally:
            gate.set()
    finally:
        assert poller.stop()
    assert peak["max"] == 1 and poller.refusedCount == 0


@pytest.mark.skip(reason=MANUAL_REASON)
def test_ac13_dashboard_is_reachable_from_another_lan_machine():
    """REQ AC-13 is checked by hand on the shared PC (``docs/manual-acceptance.md``).

    It needs a second machine on the LAN and the host firewall rule (A4). The automatable
    parts are covered elsewhere: the server binds 0.0.0.0 on ``dashboardPort`` and answers
    on a non-loopback address (TASK-009 AC-9, ``tests/integration/test_server_smoke.py``),
    and the page has no login (TASK-010 AC-10, ``tests/ui/test_dashboard_apptest.py``).
    """
