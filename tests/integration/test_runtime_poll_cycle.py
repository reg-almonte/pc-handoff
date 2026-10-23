"""``AppRuntime.pollOnce`` on real temp data directories (TASK-009 AC-1 to AC-3, AC-10, AC-11).

Every test builds the runtime through ``launcher.buildRuntime`` (startup load included) with a
``FakeSessionProvider`` and a ``FakeClock``, then drives ``pollOnce`` directly. The poller
thread is covered in ``test_poller_scheduling.py``.
"""

import json
import logging
import threading
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from pc_handoff.app_logging import LOGGER_NAME, AppLogFileHandler, configureAppLogging
from pc_handoff.detection.provider import SessionEnumerationError
from pc_handoff.domain.models import EventReason, PcStatus
from pc_handoff.launcher import buildRuntime
from pc_handoff.persistence import activity_log_store, status_store
from pc_handoff.persistence.atomic import PersistenceError
from pc_handoff.persistence.schemas import validateActivityDocument, validateStatusDocument
from pc_handoff.runtime import app_runtime
from pc_handoff.runtime.app_runtime import (
    POLL_ENUMERATION_FAILED,
    POLL_ERROR,
    POLL_OK,
    AppRuntime,
)
from pc_handoff.runtime.state_store import NoticeOrigin
from tests.conftest import FAKE_CLOCK_START
from tests.fakes import FakeClock, FakeSessionProvider
from tests.fakes.builders import osSession
from tests.fakes.pairing import assertPairingAcross

R = FAKE_CLOCK_START
C1 = R - timedelta(minutes=30)
C2 = R + timedelta(seconds=3)


# --- fixtures and helpers -----------------------------------------------------------------


@pytest.fixture
def dataDir(tmp_path: Path) -> Path:
    directory = tmp_path / "data"
    directory.mkdir()
    return directory


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(R)


@pytest.fixture
def appLog(dataDir: Path) -> Iterator[Path]:
    """The real ``app.log`` handler for ``dataDir``, closed afterwards (Windows file locks)."""
    logger = configureAppLogging(dataDir)
    try:
        yield dataDir / "logs" / "app.log"
    finally:
        for handler in list(logger.handlers):
            if isinstance(handler, AppLogFileHandler):
                logger.removeHandler(handler)
                handler.close()


def appLogLines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def bob(**overrides: Any):
    overrides.setdefault("connectTime", C1)
    return osSession(3, "bob", **overrides)


def statusJson(dataDir: Path) -> dict[str, Any]:
    return json.loads((dataDir / "status.json").read_text(encoding="utf-8"))


def activityJson(dataDir: Path) -> dict[str, Any]:
    return json.loads((dataDir / "activity_log.json").read_text(encoding="utf-8"))


def events(dataDir: Path) -> list[dict[str, Any]]:
    return activityJson(dataDir)["events"]


def build(dataDir: Path, provider: FakeSessionProvider, clock: FakeClock) -> AppRuntime:
    return buildRuntime(dataDir, provider, clock)


def reconciledWithBob(dataDir: Path, clock: FakeClock) -> tuple[AppRuntime, FakeSessionProvider]:
    """A runtime whose first poll reconciled ``bob`` Active (one session_start, U1)."""
    provider = FakeSessionProvider([bob()])
    runtime = build(dataDir, provider, clock)
    assert runtime.pollOnce("t-reconcile") == POLL_OK
    assert runtime.reconciliationDone
    assert [e["eventType"] for e in events(dataDir)] == ["session_start"]
    clock.advance(5)
    return runtime, provider


def raiseOnce(original: Callable[..., Any], error: BaseException) -> Callable[..., Any]:
    calls = {"n": 0}

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise error
        return original(*args, **kwargs)

    return wrapper


def linesWith(lines: list[dict[str, Any]], message: str) -> list[dict[str, Any]]:
    return [line for line in lines if line["message"] == message]


# --- AC-1: write order --------------------------------------------------------------------


class TestAc01WriteOrder:
    def test_activity_events_are_written_before_status_json_then_published(
        self, dataDir, clock, monkeypatch
    ):
        provider = FakeSessionProvider([])
        runtime = build(dataDir, provider, clock)
        runtime.pollOnce("t-0")
        clock.advance(5)
        provider.setSessions([bob()])

        calls: list[str] = []
        store, status = runtime.activityStore, runtime.statusStore
        originalAppend, originalWrite = store.appendBatch, status.write
        originalPublish = runtime.stateStore.publish

        def spyAppend(batch):
            calls.append("appendBatch")
            assert (dataDir / "status.json").exists()  # written by the previous poll only
            return originalAppend(batch)

        def spyWrite(state):
            calls.append("write")
            assert [e["eventType"] for e in events(dataDir)] == ["session_start"]
            return originalWrite(state)

        def spyPublish(snapshot):
            calls.append("publish")
            return originalPublish(snapshot)

        monkeypatch.setattr(store, "appendBatch", spyAppend)
        monkeypatch.setattr(status, "write", spyWrite)
        monkeypatch.setattr(runtime.stateStore, "publish", spyPublish)

        assert runtime.pollOnce("t-1") == POLL_OK

        assert calls == ["appendBatch", "write", "publish"]
        doc = statusJson(dataDir)
        assert doc["status"] == "In Use"
        assert [s["userName"] for s in doc["sessions"]] == ["bob"]
        [start] = events(dataDir)
        assert start["reason"] == "observed"
        assert doc["sessions"][0]["usagePeriodId"] == start["usagePeriodId"]
        snapshot = runtime.stateStore.current()
        assert snapshot.status is PcStatus.IN_USE
        assert [s.userName for s in snapshot.sessions] == ["bob"]
        assert snapshot.sessions[0].openPeriod.usagePeriodId == start["usagePeriodId"]

    def test_a_poll_without_events_or_pending_events_does_not_touch_the_activity_log(
        self, dataDir, clock, monkeypatch
    ):
        runtime, _ = reconciledWithBob(dataDir, clock)
        before = (dataDir / "activity_log.json").read_bytes()
        monkeypatch.setattr(
            runtime.activityStore,
            "appendBatch",
            lambda batch: pytest.fail("appendBatch called with nothing to write"),
        )
        assert runtime.pollOnce() == POLL_OK
        assert (dataDir / "activity_log.json").read_bytes() == before


# --- AC-2: failed poll --------------------------------------------------------------------


class TestAc02FailedPoll:
    def test_failure_publishes_unknown_keeps_last_known_and_logs_with_trace_id(
        self, dataDir, clock, appLog
    ):
        runtime, provider = reconciledWithBob(dataDir, clock)
        runtime.pollOnce("t-0")  # healthy, In Use
        before = statusJson(dataDir)
        eventsBefore = events(dataDir)
        clock.advance(5)
        provider.failNext("access_denied")

        assert runtime.pollOnce("t-1") == POLL_ENUMERATION_FAILED

        doc = statusJson(dataDir)
        assert doc["status"] == "Unknown"
        assert doc["detectionHealthy"] is False
        assert doc["lastKnownStatus"] == "In Use"
        assert doc["lastKnownStatusAt"] == before["lastKnownStatusAt"]
        assert doc["lastSuccessfulPollAt"] == before["lastSuccessfulPollAt"]
        assert doc["sessions"] == before["sessions"]
        assert validateStatusDocument(doc) == []
        assert events(dataDir) == eventsBefore

        snapshot = runtime.stateStore.current()
        assert (snapshot.status, snapshot.detectionHealthy) == (PcStatus.UNKNOWN, False)
        assert [s.userName for s in snapshot.sessions] == ["bob"]

        [warning] = linesWith(appLogLines(appLog), "session enumeration failed")
        assert warning["level"] == "WARNING"
        assert warning["traceId"] == "t-1"
        assert warning["detail"]["kind"] == "access_denied"
        assert "access_denied" in json.dumps(warning["detail"])

    def test_recovery_after_the_outage_rederives_status_within_one_poll(self, dataDir, clock):
        runtime, provider = reconciledWithBob(dataDir, clock)
        provider.failNext("timeout")
        runtime.pollOnce()
        clock.advance(5)
        provider.setSessions([])  # bob logged off during the outage
        assert runtime.pollOnce() == POLL_OK
        doc = statusJson(dataDir)
        assert (doc["status"], doc["detectionHealthy"]) == ("Available", True)
        assert doc["lastSuccessfulPollAt"] == clock.now().isoformat()
        end = events(dataDir)[-1]
        assert (end["eventType"], end["reason"], end["endCause"]) == (
            "session_end",
            "detected_after_outage",
            "logoff",
        )

    @pytest.mark.parametrize(
        "error",
        [
            RuntimeError("previous session enumeration is still running"),
            RuntimeError("can't start new thread"),
            ValueError("unexpected"),
        ],
        ids=["busy", "thread-start", "other"],
    )
    def test_any_exception_from_enumerate_is_a_failed_poll(
        self, dataDir, clock, capturedAppLog, monkeypatch, error
    ):
        # TASK-002 F2/S7: not just SessionEnumerationError.
        runtime, _ = reconciledWithBob(dataDir, clock)
        monkeypatch.setattr(runtime.source, "enumerate", raiseOnce(lambda: [], error))
        assert runtime.pollOnce("t-x") == POLL_ENUMERATION_FAILED
        assert statusJson(dataDir)["status"] == "Unknown"
        [warning] = linesWith(capturedAppLog(), "session enumeration failed")
        assert warning["traceId"] == "t-x"
        assert warning["detail"]["kind"] == "error"
        assert type(error).__name__ in warning["detail"]["message"]
        assert linesWith(capturedAppLog(), "poll failed with an unexpected error") == []

    def test_a_failed_thread_start_in_the_timed_source_is_a_failed_poll(
        self, dataDir, clock, monkeypatch
    ):
        runtime, _ = reconciledWithBob(dataDir, clock)

        def failingStart(self):
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(threading.Thread, "start", failingStart)
        assert runtime.pollOnce() == POLL_ENUMERATION_FAILED
        monkeypatch.undo()
        assert runtime.pollOnce() == POLL_OK

    def test_a_busy_source_fails_the_poll_without_a_second_enumeration(
        self, dataDir, clock, monkeypatch
    ):
        runtime, provider = reconciledWithBob(dataDir, clock)
        calls = provider.callCount
        monkeypatch.setattr(runtime.source, "isBusy", lambda: True)
        assert runtime.pollOnce() == POLL_ENUMERATION_FAILED
        assert provider.callCount == calls

    def test_an_enumeration_error_carries_its_winerror(self, dataDir, clock, capturedAppLog):
        runtime, provider = reconciledWithBob(dataDir, clock)
        provider.failNext(SessionEnumerationError("access_denied", "denied", winerror=5))
        runtime.pollOnce("t-w")
        [warning] = linesWith(capturedAppLog(), "session enumeration failed")
        assert warning["detail"] == {"kind": "access_denied", "message": "denied", "winerror": 5}


# --- AC-3 and the run-once reconciliation guard ------------------------------------------


class TestAc03FirstPollFailsOnFreshInstall:
    def test_unknown_status_json_then_reconciliation_at_the_first_success(self, tmp_path, clock):
        dataDir = tmp_path / "fresh"  # does not exist yet
        provider = FakeSessionProvider([bob(), osSession(4, "carol", connectionType="Console")])
        provider.failNext("timeout")
        runtime = build(dataDir, provider, clock)

        assert runtime.pollOnce() == POLL_ENUMERATION_FAILED
        doc = statusJson(dataDir)
        assert (doc["status"], doc["detectionHealthy"]) == ("Unknown", False)
        assert doc["lastKnownStatus"] is None
        assert doc["lastKnownStatusAt"] is None
        assert doc["lastSuccessfulPollAt"] is None
        assert doc["sessions"] == []
        assert activityJson(dataDir) == {"schemaVersion": 1, "events": []}
        assert runtime.reconciliationDone is False

        clock.advance(5)
        assert runtime.pollOnce() == POLL_OK
        assert runtime.reconciliationDone is True
        starts = events(dataDir)
        assert [(e["userName"], e["eventType"], e["reason"]) for e in starts] == [
            ("bob", "session_start", "detected_at_startup"),
            ("carol", "session_start", "detected_at_startup"),
        ]
        assert statusJson(dataDir)["status"] == "In Use"
        assert validateActivityDocument(activityJson(dataDir)) == []

    def test_before_the_first_success_only_the_pending_display_state_is_shown(self, dataDir, clock):
        # A previous run left alice Active in status.json; the first poll fails.
        provider = FakeSessionProvider([osSession(2, "alice", connectTime=C1)])
        runtime = build(dataDir, provider, clock)
        runtime.pollOnce()
        previous = statusJson(dataDir)
        assert previous["status"] == "In Use"
        clock.advance(60)
        restarted = build(dataDir, provider, clock)  # same files, new process
        snapshot = restarted.stateStore.current()
        assert (snapshot.status, snapshot.detectionHealthy) == (PcStatus.UNKNOWN, False)
        assert snapshot.lastKnownStatus is PcStatus.IN_USE
        assert [s.userName for s in snapshot.sessions] == ["alice"]

        provider.failNext("error")
        assert restarted.pollOnce() == POLL_ENUMERATION_FAILED
        doc = statusJson(dataDir)
        assert doc["status"] == "Unknown"
        assert doc["lastKnownStatus"] == "In Use"
        assert doc["lastSuccessfulPollAt"] == previous["lastSuccessfulPollAt"]
        assert doc["sessions"] == previous["sessions"]
        assert len(events(dataDir)) == 1  # nothing reconciled yet

        clock.advance(5)
        assert restarted.pollOnce() == POLL_OK  # alice matched: her period continues
        assert len(events(dataDir)) == 1
        assert (
            statusJson(dataDir)["sessions"][0]["usagePeriodId"]
            == events(dataDir)[0]["usagePeriodId"]
        )


class TestReconcileRunsExactlyOnce:
    @pytest.fixture
    def reconcileCalls(self, monkeypatch) -> list[int]:
        calls: list[int] = []
        original = app_runtime.reconcileStartup

        def spy(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        monkeypatch.setattr(app_runtime, "reconcileStartup", spy)
        return calls

    def test_only_at_the_first_successful_poll(self, dataDir, clock, reconcileCalls):
        provider = FakeSessionProvider([bob()])
        provider.failNext("timeout", times=2)
        runtime = build(dataDir, provider, clock)
        for _ in range(6):
            runtime.pollOnce()
            clock.advance(5)
        assert len(reconcileCalls) == 1
        assert [e["reason"] for e in events(dataDir)] == ["detected_at_startup"]

    def test_a_fault_after_the_commit_point_does_not_rerun_it(
        self, dataDir, clock, reconcileCalls, monkeypatch
    ):
        provider = FakeSessionProvider([bob()])
        runtime = build(dataDir, provider, clock)
        monkeypatch.setattr(
            runtime.statusStore, "write", raiseOnce(runtime.statusStore.write, RuntimeError("x"))
        )
        assert runtime.pollOnce() == POLL_ERROR
        assert runtime.reconciliationDone is True
        for _ in range(3):
            clock.advance(5)
            assert runtime.pollOnce() == POLL_OK
        assert len(reconcileCalls) == 1
        assert [e["eventType"] for e in events(dataDir)] == ["session_start"]

    def test_a_fault_before_the_commit_point_retries_it_at_the_next_poll(
        self, dataDir, clock, reconcileCalls, monkeypatch
    ):
        provider = FakeSessionProvider([bob()])
        runtime = build(dataDir, provider, clock)
        monkeypatch.setattr(
            app_runtime,
            "reconcileStartup",
            raiseOnce(app_runtime.reconcileStartup, RuntimeError("boom")),
        )
        assert runtime.pollOnce() == POLL_ERROR
        assert runtime.reconciliationDone is False
        assert statusJson(dataDir)["status"] == "Unknown"
        clock.advance(5)
        assert runtime.pollOnce() == POLL_OK
        assert runtime.reconciliationDone is True
        # The injected failure raised before reaching the spy; the retry is the one real run.
        assert len(reconcileCalls) == 1
        assert [e["reason"] for e in events(dataDir)] == ["detected_at_startup"]


# --- persistence failures and notices (TASK-006 constraints) ----------------------------


class TestPersistenceFailures:
    def test_a_status_write_persistence_error_is_a_failed_write_not_a_crash(
        self, dataDir, clock, capturedAppLog, monkeypatch
    ):
        runtime, provider = reconciledWithBob(dataDir, clock)
        before = (dataDir / "status.json").read_bytes()

        def failingWrite(path, data):
            raise PersistenceError("disk full")

        monkeypatch.setattr(status_store, "atomicWriteBytes", failingWrite)
        provider.setSessions([])
        assert runtime.pollOnce("t-s") == POLL_OK
        assert (dataDir / "status.json").read_bytes() == before
        # The in-memory state stays authoritative: the end is logged and published.
        assert [e["eventType"] for e in events(dataDir)] == ["session_start", "session_end"]
        assert runtime.stateStore.current().status is PcStatus.AVAILABLE
        assert runtime.stateStore.current().detectionHealthy is True
        [error] = linesWith(
            capturedAppLog(),
            "status.json could not be written; the in-memory state stays authoritative",
        )
        assert (error["level"], error["traceId"]) == ("ERROR", "t-s")
        assert "disk full" in error["detail"]

        monkeypatch.undo()
        clock.advance(5)
        runtime.pollOnce()
        assert statusJson(dataDir)["status"] == "Available"

    def test_runtime_notices_from_append_batch_reach_the_snapshot(self, dataDir, clock):
        runtime, provider = reconciledWithBob(dataDir, clock)
        (dataDir / "activity_log.json").unlink()
        provider.setSessions([])
        runtime.pollOnce()
        notices = runtime.stateStore.current().notices
        assert [(n.origin, n.text) for n in notices] == [
            (NoticeOrigin.RUNTIME, "activity_log.json was missing and has been recreated")
        ]
        (dataDir / "activity_log.json").write_bytes(b"not json")
        provider.setSessions([bob(connectTime=C2)])
        clock.advance(5)
        runtime.pollOnce()
        notices = runtime.stateStore.current().notices
        assert len(notices) == 2
        assert notices[1].backupName.startswith("activity_log.json.corrupt-")
        assert len({n.noticeId for n in notices}) == 2

    def test_a_log_that_cannot_be_recreated_adds_no_notice_per_poll(
        self, dataDir, clock, monkeypatch
    ):
        # Critic TASK-009 B1: the notice says "has been recreated", so it may only appear
        # once the file really is, and only once, however many polls the write fails.
        runtime, provider = reconciledWithBob(dataDir, clock)
        (dataDir / "activity_log.json").unlink()
        provider.setSessions([])  # bob logs off: one end event that cannot be written

        def failingWrite(path, data):
            raise PersistenceError("locked")

        monkeypatch.setattr(activity_log_store, "atomicWriteBytes", failingWrite)
        for _ in range(5):
            assert runtime.pollOnce() == POLL_OK
            assert runtime.stateStore.current().notices == ()
            assert runtime.activityStore.pendingCount() == 1
            clock.advance(5)
        assert not (dataDir / "activity_log.json").exists()

        monkeypatch.undo()
        assert runtime.pollOnce() == POLL_OK
        notices = runtime.stateStore.current().notices
        assert [(n.origin, n.text) for n in notices] == [
            (NoticeOrigin.RUNTIME, "activity_log.json was missing and has been recreated")
        ]
        [end] = events(dataDir)
        assert (end["eventType"], end["endCause"]) == ("session_end", "logoff")
        assert runtime.activityStore.pendingCount() == 0
        clock.advance(5)
        runtime.pollOnce()
        assert len(runtime.stateStore.current().notices) == 1

    def test_a_notice_is_surfaced_even_when_the_write_after_it_fails(
        self, dataDir, clock, monkeypatch
    ):
        # The runtime passes every notice through, whatever ``written`` says: the backup
        # of a damaged log happened even though the following write failed.
        runtime, provider = reconciledWithBob(dataDir, clock)
        (dataDir / "activity_log.json").write_bytes(b"not json")
        provider.setSessions([])

        def failingWrite(path, data):
            raise PersistenceError("locked")

        monkeypatch.setattr(activity_log_store, "atomicWriteBytes", failingWrite)
        runtime.pollOnce()
        [notice] = runtime.stateStore.current().notices
        assert notice.origin is NoticeOrigin.RUNTIME
        assert notice.backupName.startswith("activity_log.json.corrupt-")
        assert runtime.activityStore.pendingCount() == 1

    def test_startup_notices_and_config_warnings_are_in_every_snapshot(self, dataDir, clock):
        (dataDir / "status.json").write_text('{"status": "Maybe"}', encoding="utf-8")
        (dataDir / "config.json").write_text(
            '{"schemaVersion": 1, "dashboardPort": 80}', encoding="utf-8"
        )
        runtime = build(dataDir, FakeSessionProvider([bob()]), clock)
        pending = runtime.stateStore.current()
        runtime.pollOnce()
        polled = runtime.stateStore.current()
        assert polled is not pending
        for snapshot in (pending, polled):
            assert [n.origin for n in snapshot.notices] == [NoticeOrigin.STARTUP]
            assert snapshot.notices[0].text.startswith("Status data was reset (backup: ")
            assert any(w.startswith("dashboardPort:") for w in snapshot.configWarnings)
            assert snapshot.pollIntervalSeconds == 5
        assert pending.notices[0].noticeId == polled.notices[0].noticeId


# --- AC-10: exceptions after the commit point never duplicate periods --------------------


FAULTS = ["publish", "status-write", "before-append", "after-replace"]


def injectFault(runtime: AppRuntime, fault: str, monkeypatch: pytest.MonkeyPatch) -> None:
    error = RuntimeError(f"injected {fault}")
    if fault == "publish":  # (a) after appendBatch succeeded
        target, name = runtime.stateStore, "publish"
    elif fault == "status-write":  # (b)
        target, name = runtime.statusStore, "write"
    elif fault == "before-append":  # (c) after commit, before the appendBatch call
        target, name = runtime, "_beforeAppend"
    else:  # (d) os.replace succeeded inside appendBatch, the cache update did not
        target, name = runtime.activityStore, "commitCache"
    monkeypatch.setattr(target, name, raiseOnce(getattr(target, name), error))


class TestAc10NoDuplicatesAfterTheCommitPoint:
    @pytest.mark.parametrize("fault", FAULTS)
    def test_bob_logs_off_exactly_one_end(self, dataDir, clock, capturedAppLog, monkeypatch, fault):
        runtime, provider = reconciledWithBob(dataDir, clock)
        [start] = events(dataDir)
        u1 = start["usagePeriodId"]
        provider.setSessions([])
        injectFault(runtime, fault, monkeypatch)

        assert runtime.pollOnce("t-fault") == POLL_ERROR
        if fault == "before-append":
            assert len(events(dataDir)) == 1  # not written yet: queued for the next poll
            assert runtime.activityStore.pendingCount() == 1
        else:
            assert [e["eventType"] for e in events(dataDir)] == ["session_start", "session_end"]
        # Queued exactly once: by the handler for (c), by the store itself for (d) (never
        # re-queued by the handler), and not at all once the store has written it (a, b).
        expectedPending = 1 if fault in ("before-append", "after-replace") else 0
        assert runtime.activityStore.pendingCount() == expectedPending

        snapshot = runtime.stateStore.current()
        assert (snapshot.status, snapshot.detectionHealthy) == (PcStatus.UNKNOWN, False)
        [error] = linesWith(capturedAppLog(), "poll failed with an unexpected error")
        assert (error["level"], error["traceId"]) == ("ERROR", "t-fault")
        assert f"injected {fault}" in error["detail"]

        for traceId in ("t-2", "t-3"):
            clock.advance(5)
            assert runtime.pollOnce(traceId) == POLL_OK

        logged = events(dataDir)
        ends = [e for e in logged if e["eventType"] == "session_end"]
        assert len(logged) == 2
        assert [e["usagePeriodId"] for e in ends] == [u1]
        assert ends[0]["endCause"] == "logoff"
        assert validateActivityDocument(activityJson(dataDir)) == []
        assert runtime.activityStore.pendingCount() == 0
        assert runtime.stateStore.current().status is PcStatus.AVAILABLE
        assert statusJson(dataDir)["status"] == "Available"
        assertPairingAcross(dataDir)
        # Nothing was refused by the store's pairing guard (a re-derived duplicate would be).
        errors = [line for line in capturedAppLog() if line["level"] == "ERROR"]
        assert [line["message"] for line in errors] == ["poll failed with an unexpected error"]
        if fault == "after-replace":
            skipped = [line for line in capturedAppLog() if "already persisted" in line["message"]]
            assert skipped and skipped[0]["traceId"] == "t-2"

    @pytest.mark.parametrize("fault", FAULTS)
    def test_bob_reconnects_exactly_one_new_period(
        self, dataDir, clock, capturedAppLog, monkeypatch, fault
    ):
        runtime, provider = reconciledWithBob(dataDir, clock)
        u1 = events(dataDir)[0]["usagePeriodId"]
        provider.setSessions([bob(connectTime=C2)])  # disconnect + reconnect between polls
        injectFault(runtime, fault, monkeypatch)
        runtime.pollOnce()
        for _ in range(2):
            clock.advance(5)
            assert runtime.pollOnce() == POLL_OK

        logged = events(dataDir)
        periodIds = {e["usagePeriodId"] for e in logged}
        assert len(logged) == 3
        assert [(e["eventType"], e["usagePeriodId"] == u1) for e in logged] == [
            ("session_start", True),
            ("session_end", True),
            ("session_start", False),
        ]
        assert len(periodIds) == 2
        [newId] = periodIds - {u1}
        assert runtime.trackerState.sessions[3].openPeriod.usagePeriodId == newId
        assert statusJson(dataDir)["sessions"][0]["usagePeriodId"] == newId
        assert validateActivityDocument(activityJson(dataDir)) == []
        errors = [line for line in capturedAppLog() if line["level"] == "ERROR"]
        assert [line["message"] for line in errors] == ["poll failed with an unexpected error"]

    def test_a_fault_before_the_commit_point_counts_as_a_failed_poll(
        self, dataDir, clock, capturedAppLog, monkeypatch
    ):
        # Nothing was committed or emitted: the next poll re-diffs the same committed state,
        # under the outage rule because this poll's detection did not complete (A7).
        runtime, provider = reconciledWithBob(dataDir, clock)
        committed = runtime.trackerState
        provider.setSessions([])
        monkeypatch.setattr(
            runtime.tracker,
            "applySuccess",
            raiseOnce(runtime.tracker.applySuccess, RuntimeError("tracker bug")),
        )
        assert runtime.pollOnce("t-pre") == POLL_ERROR
        assert runtime.trackerState.sessions == committed.sessions
        assert runtime.trackerState.lastSuccessfulPollAt == committed.lastSuccessfulPollAt
        assert runtime.trackerState.outagePending is True
        assert runtime.activityStore.pendingCount() == 0
        assert statusJson(dataDir)["status"] == "Unknown"
        assert len(events(dataDir)) == 1
        clock.advance(5)
        assert runtime.pollOnce() == POLL_OK
        end = events(dataDir)[-1]
        assert (end["eventType"], end["reason"]) == ("session_end", "detected_after_outage")
        assert end["durationApproximate"] is True

    @pytest.mark.parametrize("fault", FAULTS)
    def test_after_a_fault_past_the_commit_point_the_committed_state_stays_authoritative(
        self, dataDir, clock, monkeypatch, fault
    ):
        # Critic TASK-009 S1 (interpretation 2): detection succeeded, so the fault must not turn
        # the next poll into an outage. bob's later logoff is an observed, exact end.
        runtime, provider = reconciledWithBob(dataDir, clock)
        carol = osSession(4, "carol", connectTime=R)
        provider.setSessions([bob(), carol])  # carol's start makes a batch, so (d) can fire
        injectFault(runtime, fault, monkeypatch)
        assert runtime.pollOnce("t-fault") == POLL_ERROR
        assert runtime.trackerState.outagePending is False
        assert runtime.trackerState.detectionHealthy is True
        assert runtime.stateStore.current().status is PcStatus.UNKNOWN  # display only

        clock.advance(5)
        provider.setSessions([carol])  # bob logs off
        assert runtime.pollOnce() == POLL_OK

        [end] = [e for e in events(dataDir) if e["eventType"] == "session_end"]
        assert (end["userName"], end["endCause"]) == ("bob", "logoff")
        assert end["reason"] == "observed"
        assert end["durationApproximate"] is False
        assert end["periodEnd"] == end["timestamp"] == clock.now().isoformat()
        starts = [e["userName"] for e in events(dataDir) if e["eventType"] == "session_start"]
        assert starts == ["bob", "carol"]

    def test_recovery_never_raises_even_when_publishing_keeps_failing(
        self, dataDir, clock, capturedAppLog, monkeypatch
    ):
        runtime, provider = reconciledWithBob(dataDir, clock)

        def broken(*args, **kwargs):
            raise RuntimeError("still broken")

        monkeypatch.setattr(runtime.stateStore, "publish", broken)
        monkeypatch.setattr(runtime.tracker, "applyFailure", broken)
        monkeypatch.setattr(runtime.tracker, "applySuccess", broken)
        monkeypatch.setattr(runtime.statusStore, "write", broken)
        assert runtime.pollOnce("t-r") == POLL_ERROR
        messages = [line["message"] for line in capturedAppLog() if line["traceId"] == "t-r"]
        assert "poll failed with an unexpected error" in messages
        assert "poll error recovery failed" in messages
        assert messages[-1] == "poll completed"


# --- AC-11: poll completed lines ----------------------------------------------------------


class TestAc11PollCompletedLine:
    def test_duration_outcome_and_pending_queue_per_poll(self, dataDir, clock, appLog, monkeypatch):
        provider = FakeSessionProvider([bob()])
        runtime = build(dataDir, provider, clock)

        originalEnumerate = runtime.source.enumerate

        def slowEnumerate():
            clock.advance(0.25)  # 250 ms of (fake) monotonic time inside the poll
            return originalEnumerate()

        monkeypatch.setattr(runtime.source, "enumerate", slowEnumerate)
        assert runtime.pollOnce("t-ok") == POLL_OK
        clock.advance(5)
        provider.failNext("error")
        assert runtime.pollOnce("t-fail") == POLL_ENUMERATION_FAILED
        clock.advance(5)
        provider.setSessions([])  # an end event that cannot be written

        def failingWrite(path, data):
            raise PersistenceError("locked")

        monkeypatch.setattr(activity_log_store, "atomicWriteBytes", failingWrite)
        assert runtime.pollOnce("t-pending") == POLL_OK

        completed = linesWith(appLogLines(appLog), "poll completed")
        assert [line["traceId"] for line in completed] == ["t-ok", "t-fail", "t-pending"]
        assert all(line["level"] == "INFO" for line in completed)
        details = [line["detail"] for line in completed]
        assert [d["outcome"] for d in details] == ["ok", "enumeration_failed", "ok"]
        assert all(type(d["durationMs"]) is int for d in details)
        assert details[0]["durationMs"] == 250
        assert [d["pendingEvents"] for d in details][:2] == [0, 0]
        assert details[2]["pendingEvents"] >= 1
        assert [d["eventsInBatch"] for d in details] == [1, 0, 1]

    def test_every_log_line_of_a_poll_carries_its_trace_id(self, dataDir, clock, capturedAppLog):
        runtime, provider = reconciledWithBob(dataDir, clock)
        before = len(capturedAppLog())
        provider.failNext("timeout")
        runtime.pollOnce("t-fail")
        failLines = capturedAppLog()[before:]
        before = len(capturedAppLog())
        (dataDir / "activity_log.json").unlink()  # the store warns while writing the end
        provider.setSessions([])
        clock.advance(5)
        runtime.pollOnce("t-store")
        storeLines = capturedAppLog()[before:]
        assert [line["message"] for line in failLines] == [
            "session enumeration failed",
            "poll completed",
        ]
        assert {line["traceId"] for line in failLines} == {"t-fail"}
        assert len(storeLines) >= 2
        assert any("was missing while running" in line["message"] for line in storeLines)
        assert {line["traceId"] for line in storeLines} == {"t-store"}

    def test_a_fresh_trace_id_is_used_when_none_is_given(self, dataDir, clock, capturedAppLog):
        runtime = build(dataDir, FakeSessionProvider([]), clock)
        runtime.pollOnce()
        runtime.pollOnce()
        ids = [line["traceId"] for line in linesWith(capturedAppLog(), "poll completed")]
        assert len(ids) == 2 and len(set(ids)) == 2
        assert all(isinstance(i, str) and len(i) == 32 for i in ids)


def test_runtime_logs_under_the_package_logger(dataDir, clock):
    runtime = build(dataDir, FakeSessionProvider([]), clock)
    assert runtime.logger.name.startswith(LOGGER_NAME)
    assert isinstance(runtime.logger, logging.Logger)


def test_detection_time_comes_from_the_clock_after_enumeration(dataDir, clock, monkeypatch):
    # Critic TASK-009 S2: the clock moves during enumerate, so a detection time read before
    # the enumeration would show up as a 2 s earlier timestamp.
    provider = FakeSessionProvider([])
    runtime = build(dataDir, provider, clock)
    runtime.pollOnce()
    provider.setSessions([bob(connectTime=None)])
    clock.advance(7)
    beforeEnumeration = clock.now()
    originalEnumerate = runtime.source.enumerate

    def slowEnumerate():
        sessions = originalEnumerate()
        clock.advance(2)  # the enumeration itself takes 2 s
        return sessions

    monkeypatch.setattr(runtime.source, "enumerate", slowEnumerate)
    runtime.pollOnce()
    [start] = events(dataDir)
    afterEnumeration = beforeEnumeration + timedelta(seconds=2)
    assert start["timestamp"] == afterEnumeration.isoformat()
    assert start["usagePeriodStart"] == afterEnumeration.isoformat()  # no connect time: approx.
    assert start["reason"] == EventReason.OBSERVED.value
