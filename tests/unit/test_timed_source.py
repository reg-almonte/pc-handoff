"""Tests for pc_handoff.detection.timed_source (TASK-002 AC-6, AC-7; Critic TASK-001 S5)."""

import logging
import threading
import time
from collections.abc import Callable, Iterator

import pytest

from pc_handoff.app_logging import traceScope
from pc_handoff.detection.provider import OsSession, SessionEnumerationError
from pc_handoff.detection.timed_source import ENUMERATION_TIMEOUT_SECONDS, TimedSessionSource
from tests.fakes import FakeSessionProvider, makeSession

FAST_TIMEOUT = 0.2
SLACK = 0.5


@pytest.fixture
def release() -> Iterator[threading.Event]:
    """An event that blocks a fake call; always set at teardown so no worker is left hung."""
    event = threading.Event()
    yield event
    event.set()


def sessions() -> list[OsSession]:
    return [makeSession(2, "alice"), makeSession(3, "bob", state="Disconnected")]


def waitForWorker(source: TimedSessionSource) -> None:
    worker = source.abandonedWorker
    assert worker is not None
    worker.join(5)
    assert not worker.is_alive()


class LoggingProvider:
    """Provider that logs from inside ``enumerateSessions`` (i.e. inside the worker)."""

    def __init__(self) -> None:
        self.threadName: str | None = None

    def enumerateSessions(self) -> list[OsSession]:
        self.threadName = threading.current_thread().name
        logging.getLogger("pc_handoff.detection.test").warning("inside worker")
        return []


class TestSuccessAndErrors:
    def test_returns_provider_sessions(self) -> None:
        provider = FakeSessionProvider(sessions())
        source = TimedSessionSource(provider, timeoutSeconds=FAST_TIMEOUT)
        assert source.enumerate() == sessions()
        assert source.isBusy() is False
        assert provider.callCount == 1

    @pytest.mark.parametrize("kind", ["error", "access_denied", "timeout"])
    def test_enumeration_error_passes_through(self, kind: str) -> None:
        provider = FakeSessionProvider(sessions())
        provider.failNext(kind)
        with pytest.raises(SessionEnumerationError) as info:
            TimedSessionSource(provider, timeoutSeconds=FAST_TIMEOUT).enumerate()
        assert info.value.kind == kind

    def test_other_exception_wrapped_as_error(self) -> None:
        provider = FakeSessionProvider(sessions())
        original = ValueError("bad struct")
        provider.failNext(original)
        with pytest.raises(SessionEnumerationError) as info:
            TimedSessionSource(provider, timeoutSeconds=FAST_TIMEOUT).enumerate()
        assert info.value.kind == "error"
        assert "ValueError: bad struct" in str(info.value)
        assert info.value.__cause__ is original

    def test_fail_next_times(self) -> None:
        provider = FakeSessionProvider(sessions())
        provider.failNext("error", times=2)
        source = TimedSessionSource(provider, timeoutSeconds=FAST_TIMEOUT)
        for _ in range(2):
            with pytest.raises(SessionEnumerationError):
                source.enumerate()
        assert source.enumerate() == sessions()


class TestTimeout:
    def test_ac06_blocked_call_times_out_and_reports_busy(self, release: threading.Event) -> None:
        provider = FakeSessionProvider(sessions())
        provider.blockNext(release)
        source = TimedSessionSource(provider, timeoutSeconds=FAST_TIMEOUT)

        started = time.monotonic()
        with pytest.raises(SessionEnumerationError) as info:
            source.enumerate()
        elapsed = time.monotonic() - started

        assert info.value.kind == "timeout"
        assert FAST_TIMEOUT <= elapsed < FAST_TIMEOUT + SLACK
        assert source.isBusy() is True
        time.sleep(0.05)
        assert source.isBusy() is True  # still blocked: no overlapping enumeration allowed
        with pytest.raises(RuntimeError, match="still running"):
            source.enumerate()
        assert provider.callCount == 1  # the refused call never reached the provider

        release.set()
        waitForWorker(source)
        assert source.isBusy() is False
        assert source.abandonedWorker is None
        assert source.enumerate() == sessions()
        assert provider.callCount == 2

    @pytest.mark.slow
    def test_ac07_production_timeout_is_three_seconds(self, release: threading.Event) -> None:
        assert ENUMERATION_TIMEOUT_SECONDS == 3
        provider = FakeSessionProvider(sessions())
        provider.blockNext(release)
        source = TimedSessionSource(provider)
        assert source.timeoutSeconds == 3

        started = time.monotonic()
        with pytest.raises(SessionEnumerationError) as info:
            source.enumerate()
        elapsed = time.monotonic() - started

        assert info.value.kind == "timeout"
        assert 3.0 <= elapsed < 3.5
        assert source.isBusy() is True
        release.set()
        waitForWorker(source)
        assert source.isBusy() is False


class TestTraceIdInWorker:
    def test_worker_log_lines_carry_callers_trace_id(
        self, capturedAppLog: Callable[[], list[dict]]
    ) -> None:
        provider = LoggingProvider()
        source = TimedSessionSource(provider, timeoutSeconds=FAST_TIMEOUT)
        with traceScope("poll-trace-1"):
            source.enumerate()
        assert provider.threadName == "pc-handoff-session-enumeration"
        assert provider.threadName != threading.current_thread().name
        line = next(x for x in capturedAppLog() if x["message"] == "inside worker")
        assert line["traceId"] == "poll-trace-1"

    def test_each_call_uses_the_current_trace_id(
        self, capturedAppLog: Callable[[], list[dict]]
    ) -> None:
        source = TimedSessionSource(LoggingProvider(), timeoutSeconds=FAST_TIMEOUT)
        for traceId in ("first", "second"):
            with traceScope(traceId):
                source.enumerate()
        source.enumerate()  # outside any scope
        traceIds = [x["traceId"] for x in capturedAppLog() if x["message"] == "inside worker"]
        assert traceIds == ["first", "second", None]

    @pytest.mark.parametrize("failLate", [False, True])
    def test_abandoned_worker_logs_completion_with_trace_id(
        self,
        release: threading.Event,
        capturedAppLog: Callable[[], list[dict]],
        failLate: bool,
    ) -> None:
        provider = FakeSessionProvider(sessions())
        provider.blockNext(release)
        if failLate:
            provider.failNext("access_denied")
        source = TimedSessionSource(provider, timeoutSeconds=FAST_TIMEOUT)
        with traceScope("hung-poll"), pytest.raises(SessionEnumerationError):
            source.enumerate()

        with traceScope("later-poll"):
            release.set()
            waitForWorker(source)

        line = next(
            x for x in capturedAppLog() if x["message"].startswith("abandoned session enumeration")
        )
        assert line["level"] == "INFO"
        assert line["traceId"] == "hung-poll"
        assert line["detail"]["elapsedSeconds"] >= FAST_TIMEOUT
        expectedOutcome = "SessionEnumerationError: access_denied: fake access_denied"
        assert line["detail"]["outcome"] == (expectedOutcome if failLate else "ok")

    def test_completed_call_logs_no_abandoned_line(
        self, capturedAppLog: Callable[[], list[dict]]
    ) -> None:
        TimedSessionSource(FakeSessionProvider(sessions()), timeoutSeconds=FAST_TIMEOUT).enumerate()
        assert not [x for x in capturedAppLog() if "abandoned" in x["message"]]
