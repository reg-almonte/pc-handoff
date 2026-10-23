"""Startup loading and reconciliation on real temp directories (TASK-008 AC-1 to AC-11).

Each test writes fixture files, runs ``loadStartup`` (and ``reconcileStartup`` with a fake
snapshot), appends the resulting events through the context's ``ActivityLogStore`` and checks
the files with the real validators and the cross-file pairing helper. The poll loop itself is
TASK-009; ``pollOnce`` below is a minimal stand-in for AC-27 ordering only.
"""

import json
import logging
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from pc_handoff.app_logging import LOGGER_NAME, AppLogFileHandler, configureAppLogging
from pc_handoff.detection.provider import ConnectionType, SessionEnumerationError
from pc_handoff.domain.models import (
    ActivityEvent,
    EndCause,
    EventReason,
    EventType,
    PcStatus,
    SessionState,
    StatusDocument,
    StatusSession,
)
from pc_handoff.domain.tracker import SessionTracker, TrackerState
from pc_handoff.persistence.activity_log_store import serializeCanonical
from pc_handoff.persistence.config_store import DEFAULT_CONFIG_BYTES, loadConfig
from pc_handoff.persistence.paths import DataPaths
from pc_handoff.persistence.schemas import (
    decodeJson,
    validateActivityDocument,
    validateStatusDocument,
)
from pc_handoff.persistence.startup_loader import (
    SOURCE_THREE_WARNING,
    OpenPeriodSource,
    StartupContext,
    loadStartup,
    pendingDisplayState,
    reconcileStartup,
)
from tests.conftest import FAKE_CLOCK_START
from tests.fakes import FakeClock, FakeSessionProvider
from tests.fakes.activity_checks import activityDocumentProblems
from tests.fakes.builders import osSession
from tests.fakes.log_factory import endEvent, newId, startEvent
from tests.fakes.pairing import assertPairingAcross, pairingProblemsAcross, readableEvents

R = FAKE_CLOCK_START  # 2026-10-21T09:00:00+09:00
STAMP = "20261021T090000"
P = R - timedelta(minutes=10)
C1 = R - timedelta(minutes=30)
S1 = C1 + timedelta(seconds=2)
LOGON = R - timedelta(hours=1)
EMPTY_LOG = b'{"schemaVersion": 1, "events": []}\n'


# --- fixtures and file helpers ------------------------------------------------------------


@pytest.fixture
def dataDir(tmp_path: Path) -> Path:
    directory = tmp_path / "data"
    directory.mkdir()
    return directory


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(R)


@pytest.fixture
def logger() -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.startup")


@pytest.fixture
def tracker(clock: FakeClock) -> SessionTracker:
    return SessionTracker(clock, logging.getLogger(f"{LOGGER_NAME}.domain.tracker"))


def load(dataDir: Path, clock: FakeClock, logger: logging.Logger) -> StartupContext:
    return loadStartup(DataPaths(dataDir), clock, logger)


def writeLog(dataDir: Path, events: list[ActivityEvent]) -> bytes:
    data = serializeCanonical({}, [e.toJson() for e in events])
    (dataDir / "activity_log.json").write_bytes(data)
    return data


def activeEntry(
    sessionId: int,
    user: str,
    periodId: str,
    *,
    connectTime: datetime | None = C1,
    usagePeriodStart: datetime | None = None,
    approximate: bool = False,
    firstDetectedAt: datetime | None = None,
) -> StatusSession:
    return StatusSession(
        sessionId=sessionId,
        userName=user,
        domain="CORP",
        state=SessionState.ACTIVE,
        connectionType=ConnectionType.RDP,
        clientName="CLIENT-PC",
        clientAddress="192.168.1.20",
        logonTime=LOGON,
        firstDetectedAt=firstDetectedAt,
        connectTime=connectTime,
        usagePeriodId=periodId,
        usagePeriodStart=usagePeriodStart or connectTime,
        durationApproximate=approximate,
        disconnectedSince=None,
    )


def disconnectedEntry(sessionId: int, user: str, since: datetime) -> StatusSession:
    return StatusSession(
        sessionId=sessionId,
        userName=user,
        domain="CORP",
        state=SessionState.DISCONNECTED,
        connectionType=ConnectionType.RDP,
        clientName=None,
        clientAddress=None,
        logonTime=LOGON,
        firstDetectedAt=since,
        connectTime=since - timedelta(hours=1),
        usagePeriodId=None,
        usagePeriodStart=None,
        durationApproximate=False,
        disconnectedSince=since,
    )


def writeStatus(dataDir: Path, sessions: list[StatusSession], p: datetime | None = P) -> bytes:
    inUse = any(s.state is SessionState.ACTIVE for s in sessions)
    status = PcStatus.IN_USE if inUse else PcStatus.AVAILABLE
    doc = StatusDocument(
        status=status,
        lastKnownStatus=status if p else None,
        lastKnownStatusAt=p,
        lastSuccessfulPollAt=p,
        updatedAt=p or R,
        detectionHealthy=True,
        sessions=tuple(sessions),
    ).toJson()
    assert validateStatusDocument(doc) == []
    data = (json.dumps(doc, indent=2) + "\n").encode("utf-8")
    (dataDir / "status.json").write_bytes(data)
    return data


def aliceStart(periodId: str, *, connectTime: datetime | None = C1, **overrides) -> ActivityEvent:
    return startEvent(
        periodId,
        S1,
        sessionId=2,
        userName="alice",
        usagePeriodStart=C1,
        logonTime=LOGON,
        connectTime=connectTime,
        durationApproximate=connectTime is None,
        **overrides,
    )


def alice(**overrides):
    overrides.setdefault("connectTime", C1)
    overrides.setdefault("logonTime", LOGON)
    return osSession(2, "alice", **overrides)


def logDocument(dataDir: Path) -> dict:
    doc = decodeJson((dataDir / "activity_log.json").read_bytes())
    assert validateActivityDocument(doc) == []
    return doc  # type: ignore[return-value]


def statusDocument(dataDir: Path) -> dict:
    doc = decodeJson((dataDir / "status.json").read_bytes())
    assert validateStatusDocument(doc) == []
    return doc  # type: ignore[return-value]


def corruptFiles(dataDir: Path) -> list[str]:
    return sorted(p.name for p in dataDir.glob("*.corrupt-*"))


def messages(lines, level: str) -> list[str]:
    return [line["message"] for line in lines() if line["level"] == level]


def persist(ctx: StartupContext, events: list[ActivityEvent]) -> None:
    assert activityDocumentProblems(events) == []
    result = ctx.activityStore.appendBatch(events)
    assert result.written is True


# --- AC-1: corrupt status.json, periods rebuilt from the log (REQ AC-17) ------------------


class TestAc01CorruptStatus:
    def test_u1_continues_and_u2_closed_at_its_start_event(self, dataDir, clock, logger, tracker):
        u1, u2 = newId(), newId()
        b2 = R - timedelta(hours=2)
        s2 = b2 + timedelta(seconds=3)
        writeLog(
            dataDir,
            [
                aliceStart(u1),
                startEvent(u2, s2, sessionId=3, userName="bob", usagePeriodStart=b2),
            ],
        )
        broken = writeStatus(dataDir, [activeEntry(2, "alice", u1)]).replace(
            b'"status": "In Use"', b'"status": "Maybe"'
        )
        (dataDir / "status.json").write_bytes(broken)

        ctx = load(dataDir, clock, logger)

        backup = dataDir / f"status.json.corrupt-{STAMP}"
        assert backup.read_bytes() == broken
        assert not (dataDir / "status.json").exists()
        assert [n.text for n in ctx.notices] == [f"Status data was reset (backup: {backup.name})"]
        assert ctx.openPeriodSource is OpenPeriodSource.ACTIVITY_LOG
        assert ctx.lastSuccessfulPollAt is None

        outcome = reconcileStartup(ctx, tracker, [alice()], R)
        persist(ctx, outcome.events)
        ctx.statusStore.write(outcome.state)

        events = logDocument(dataDir)["events"]
        assert [e["eventType"] for e in events] == ["session_start"] * 2 + ["session_end"]
        end = events[-1]
        assert end["usagePeriodId"] == u2
        assert end["reason"] == "detected_at_startup"
        assert end["endCause"] == "logoff"
        assert end["durationApproximate"] is True
        assert ActivityEvent.fromJson(end).periodEnd == s2
        assert end["durationSeconds"] == int((s2 - b2).total_seconds())
        assert outcome.state.sessions[2].openPeriod.usagePeriodId == u1
        assert outcome.state.sessions[2].openPeriod.usagePeriodStart == C1
        status = statusDocument(dataDir)
        assert status["sessions"][0]["usagePeriodId"] == u1
        assertPairingAcross(dataDir)


# --- AC-2 / AC-3: corrupt or missing log, periods rebuilt from status.json (REQ AC-18) ----


class TestAc02CorruptActivityLog:
    @pytest.mark.parametrize("kind", ["unparseable", "schema-version"])
    def test_u1_continues_and_later_end_goes_to_the_fresh_file(
        self, dataDir, clock, logger, tracker, kind
    ):
        u1 = newId()
        if kind == "unparseable":
            broken = b'{"schemaVersion": 1, "events": ['
        else:  # parseable, so its U1 start counts for the pairing check across files
            broken = json.dumps({"schemaVersion": 2, "events": [aliceStart(u1).toJson()]})
            broken = broken.encode("utf-8")
        (dataDir / "activity_log.json").write_bytes(broken)
        writeStatus(dataDir, [activeEntry(2, "alice", u1)])

        ctx = load(dataDir, clock, logger)

        backup = dataDir / f"activity_log.json.corrupt-{STAMP}"
        assert backup.read_bytes() == broken
        assert (dataDir / "activity_log.json").read_bytes() == EMPTY_LOG
        [notice] = ctx.notices
        assert notice.fileName == "activity_log.json"
        assert notice.backupName == backup.name
        assert "activity_log.json" in notice.text
        assert backup.name in notice.text
        assert ctx.openPeriodSource is OpenPeriodSource.STATUS
        assert ctx.lastSuccessfulPollAt == P

        outcome = reconcileStartup(ctx, tracker, [alice()], R)
        assert outcome.events == []
        assert outcome.state.sessions[2].openPeriod.usagePeriodId == u1
        persist(ctx, outcome.events)
        assert (dataDir / "activity_log.json").read_bytes() == EMPTY_LOG

        clock.advance(60)
        d = clock.now() - timedelta(seconds=20)
        later = tracker.applySuccess(outcome.state, [alice(state="Disconnected", disconnectTime=d)])
        persist(ctx, later.events)

        [end] = logDocument(dataDir)["events"]
        assert end["eventType"] == "session_end"
        assert end["usagePeriodId"] == u1
        assert ActivityEvent.fromJson(end).usagePeriodStart == C1
        assert ActivityEvent.fromJson(end).periodEnd == d
        assertPairingAcross(dataDir)
        if kind == "schema-version":
            # U1's start really is in the backup, so the check above paired across files.
            backupEvents = readableEvents(backup)
            assert [(e["eventType"], e["usagePeriodId"]) for e in backupEvents] == [
                ("session_start", u1)
            ]


class TestAc03MissingActivityLog:
    def test_missing_log_falls_back_to_status_json(self, dataDir, clock, logger, tracker):
        u1 = newId()
        writeStatus(dataDir, [activeEntry(2, "alice", u1)])

        ctx = load(dataDir, clock, logger)

        assert ctx.activityLogUsable is False
        assert ctx.openPeriodSource is OpenPeriodSource.STATUS
        assert [r.usagePeriodId for r in ctx.openPeriods] == [u1]
        assert (dataDir / "activity_log.json").read_bytes() == EMPTY_LOG
        assert ctx.notices == ()
        outcome = reconcileStartup(ctx, tracker, [alice()], R)
        assert outcome.events == []
        assert outcome.state.sessions[2].openPeriod.usagePeriodId == u1
        assert corruptFiles(dataDir) == []


# --- AC-4: restart keeps the ongoing period (REQ AC-19) -----------------------------------


class TestAc04Restart:
    @pytest.mark.parametrize("approximate", [False, True], ids=["exact", "approximate"])
    def test_period_matched_and_log_bytes_unchanged(
        self, dataDir, clock, logger, tracker, approximate
    ):
        u1, old = newId(), newId()
        earlier = C1 - timedelta(hours=3)
        history = [
            startEvent(old, earlier, sessionId=2, userName="alice"),
            endEvent(old, earlier + timedelta(hours=1), usagePeriodStart=earlier, userName="alice"),
        ]
        connectTime = None if approximate else C1
        logBytes = writeLog(dataDir, [*history, aliceStart(u1, connectTime=connectTime)])
        statusBytes = writeStatus(
            dataDir,
            [
                activeEntry(
                    2,
                    "alice",
                    u1,
                    connectTime=connectTime,
                    usagePeriodStart=C1,
                    approximate=approximate,
                )
            ],
        )

        ctx = load(dataDir, clock, logger)
        assert ctx.openPeriodSource is OpenPeriodSource.ACTIVITY_LOG
        snapshotConnect = C1 + timedelta(minutes=7) if approximate else C1
        outcome = reconcileStartup(ctx, tracker, [alice(connectTime=snapshotConnect)], R)
        persist(ctx, outcome.events)

        assert outcome.events == []
        period = outcome.state.sessions[2].openPeriod
        assert period.usagePeriodId == u1
        assert period.usagePeriodStart == C1
        assert period.durationApproximate is approximate
        assert period.startEventTimestamp == S1
        assert (dataDir / "activity_log.json").read_bytes() == logBytes
        assert (dataDir / "status.json").read_bytes() == statusBytes  # startup never writes it

    def test_different_connect_time_closes_and_restarts(self, dataDir, clock, logger, tracker):
        u1 = newId()
        writeLog(dataDir, [aliceStart(u1)])
        writeStatus(dataDir, [activeEntry(2, "alice", u1)])
        ctx = load(dataDir, clock, logger)

        c2 = R - timedelta(minutes=3)
        outcome = reconcileStartup(ctx, tracker, [alice(connectTime=c2)], R)
        persist(ctx, outcome.events)

        events = logDocument(dataDir)["events"]
        assert [(e["eventType"], e["reason"]) for e in events[1:]] == [
            ("session_end", "detected_at_startup"),
            ("session_start", "detected_at_startup"),
        ]
        assert events[1]["usagePeriodId"] == u1
        assert events[1]["endCause"] == "reconnect_detected"
        assert events[2]["usagePeriodId"] != u1
        assert ActivityEvent.fromJson(events[2]).usagePeriodStart == c2
        assertPairingAcross(dataDir)


# --- AC-5: downtime changes under the blind rule (REQ AC-20) ------------------------------


class TestAc05DowntimeChanges:
    def test_bob_dave_carol(self, dataDir, clock, logger, tracker):
        ub, ud = newId(), newId()
        cb, cd = R - timedelta(hours=3), R - timedelta(hours=1)
        sb, sd = cb + timedelta(seconds=2), cd + timedelta(seconds=2)
        writeLog(
            dataDir,
            [
                startEvent(
                    ub, sb, sessionId=3, userName="bob", usagePeriodStart=cb, logonTime=LOGON
                ),
                startEvent(
                    ud, sd, sessionId=4, userName="dave", usagePeriodStart=cd, logonTime=LOGON
                ),
            ],
        )
        writeStatus(
            dataDir,
            [
                activeEntry(3, "bob", ub, connectTime=cb),
                activeEntry(4, "dave", ud, connectTime=cd),
            ],
        )
        ctx = load(dataDir, clock, logger)
        assert ctx.lastSuccessfulPollAt == P

        d = R - timedelta(minutes=25)
        snapshot = [
            osSession(4, "dave", "Disconnected", connectTime=cd, disconnectTime=d, logonTime=LOGON),
            osSession(5, "carol", connectTime=R - timedelta(minutes=2)),
        ]
        outcome = reconcileStartup(ctx, tracker, snapshot, R)
        persist(ctx, outcome.events)

        events = [ActivityEvent.fromJson(e) for e in logDocument(dataDir)["events"][2:]]
        bobEnd, daveEnd, carolStart = events
        assert (bobEnd.usagePeriodId, bobEnd.endCause) == (ub, EndCause.LOGOFF)
        assert bobEnd.periodEnd == max(P, sb)
        assert bobEnd.durationSeconds == int((bobEnd.periodEnd - cb).total_seconds())
        assert (daveEnd.usagePeriodId, daveEnd.endCause) == (ud, EndCause.DISCONNECT)
        assert daveEnd.periodEnd == d
        assert daveEnd.durationSeconds == int((d - cd).total_seconds())
        for end in (bobEnd, daveEnd):
            assert end.reason is EventReason.DETECTED_AT_STARTUP
            assert end.durationApproximate is True
        assert carolStart.eventType is EventType.SESSION_START
        assert carolStart.reason is EventReason.DETECTED_AT_STARTUP
        assert outcome.state.status is PcStatus.IN_USE
        assert outcome.state.sessions[4].state is SessionState.DISCONNECTED

        ctx.statusStore.write(outcome.state)
        status = statusDocument(dataDir)
        assert status["status"] == "In Use"
        assert [s["state"] for s in status["sessions"]] == ["Disconnected", "Active"]


# --- AC-6 / AC-7: first run, first poll failing (REQ AC-16, AC-27) ------------------------


def pollOnce(ctx, tracker, provider, state, reconciled, clock):
    """Minimal stand-in for the TASK-009 poll cycle: reconcile once, at the first success."""
    try:
        sessions = provider.enumerateSessions()
    except SessionEnumerationError as exc:
        return tracker.applyFailure(state, exc, clock.now()), reconciled
    if not reconciled:
        return reconcileStartup(ctx, tracker, sessions, clock.now()), True
    return tracker.applySuccess(state, sessions, clock.now()), reconciled


class TestAc06FirstPollFails:
    def test_reconciliation_waits_for_the_first_successful_poll(
        self, dataDir, clock, logger, tracker
    ):
        provider = FakeSessionProvider([osSession(3, "erin", connectionType="Console")])
        provider.failNext("timeout")
        ctx = load(dataDir, clock, logger)

        state = pendingDisplayState(ctx)
        assert state.status is PcStatus.UNKNOWN
        assert state.detectionHealthy is False
        assert (state.lastKnownStatus, state.lastKnownStatusAt) == (None, None)
        assert state.lastSuccessfulPollAt is None
        assert state.sessions == {}

        outcome, reconciled = pollOnce(ctx, tracker, provider, state, False, clock)
        assert reconciled is False
        assert outcome.events == []
        persist(ctx, outcome.events)
        ctx.statusStore.write(outcome.state)
        assert (dataDir / "activity_log.json").read_bytes() == EMPTY_LOG
        status = statusDocument(dataDir)
        assert (status["status"], status["detectionHealthy"]) == ("Unknown", False)
        assert status["lastKnownStatus"] is None
        assert status["lastKnownStatusAt"] is None
        assert status["lastSuccessfulPollAt"] is None
        assert status["sessions"] == []

        clock.advance(5)
        outcome, reconciled = pollOnce(ctx, tracker, provider, outcome.state, False, clock)
        assert reconciled is True
        [start] = outcome.events
        assert start.eventType is EventType.SESSION_START
        assert start.reason is EventReason.DETECTED_AT_STARTUP
        assert start.userName == "erin"
        persist(ctx, outcome.events)
        assert len(logDocument(dataDir)["events"]) == 1


@pytest.fixture
def appLogHandlerCleanup() -> Iterator[None]:
    yield
    root = logging.getLogger(LOGGER_NAME)
    for handler in list(root.handlers):
        if isinstance(handler, AppLogFileHandler):
            root.removeHandler(handler)
            handler.close()


class TestAc07FirstRun:
    def test_everything_created_without_notices(
        self, tmp_path, clock, logger, tracker, capturedAppLog, appLogHandlerCleanup
    ):
        dataDir = tmp_path / "missing" / "data"
        configureAppLogging(dataDir)
        ctx = load(dataDir, clock, logger)
        outcome = reconcileStartup(ctx, tracker, [osSession(2, "alice")], R)
        persist(ctx, outcome.events)
        ctx.statusStore.write(outcome.state)

        assert (dataDir / "config.json").read_bytes() == DEFAULT_CONFIG_BYTES
        [start] = logDocument(dataDir)["events"]
        assert (start["eventType"], start["reason"]) == ("session_start", "detected_at_startup")
        assert statusDocument(dataDir)["status"] == "In Use"
        assert (dataDir / "logs" / "app.log").is_file()
        assert ctx.notices == ()
        assert ctx.configWarnings == ()
        assert ctx.openPeriodSource is OpenPeriodSource.NONE
        assert messages(capturedAppLog, "WARNING") == []
        assert messages(capturedAppLog, "ERROR") == []
        assert corruptFiles(dataDir) == []


# --- AC-8: transitional at the first poll -------------------------------------------------


class TestAc08Transitional:
    def test_transitional_alice_keeps_u1(self, dataDir, clock, logger, tracker):
        u1 = newId()
        writeLog(dataDir, [aliceStart(u1)])
        ctx = load(dataDir, clock, logger)

        outcome = reconcileStartup(ctx, tracker, [alice(state="ConnectQuery")], R)
        assert outcome.events == []
        assert outcome.state.sessions[2].state is SessionState.ACTIVE
        assert outcome.state.sessions[2].openPeriod.usagePeriodId == u1

        clock.advance(5)
        later = tracker.applySuccess(outcome.state, [alice()])
        assert later.events == []
        assert later.state.sessions[2].openPeriod.usagePeriodId == u1


# --- AC-9: both files lost (source 3) ----------------------------------------------------


class TestAc09BothFilesLost:
    def test_both_backed_up_limitation_logged_and_alice_started(
        self, dataDir, clock, logger, tracker, capturedAppLog
    ):
        (dataDir / "status.json").write_bytes(b"{nope")
        (dataDir / "activity_log.json").write_bytes(b"[1, 2")

        ctx = load(dataDir, clock, logger)

        assert corruptFiles(dataDir) == [
            f"activity_log.json.corrupt-{STAMP}",
            f"status.json.corrupt-{STAMP}",
        ]
        assert ctx.openPeriodSource is OpenPeriodSource.NONE
        assert ctx.openPeriods == ()
        assert SOURCE_THREE_WARNING in messages(capturedAppLog, "WARNING")
        outcome = reconcileStartup(ctx, tracker, [alice()], R)
        [start] = outcome.events
        assert (start.eventType, start.reason) == (
            EventType.SESSION_START,
            EventReason.DETECTED_AT_STARTUP,
        )
        assert start.userName == "alice"
        persist(ctx, outcome.events)

    def test_corrupt_status_and_missing_log_is_source_three(
        self, dataDir, clock, logger, capturedAppLog
    ):
        (dataDir / "status.json").write_bytes(b"{nope")
        ctx = load(dataDir, clock, logger)
        assert ctx.openPeriodSource is OpenPeriodSource.NONE
        assert SOURCE_THREE_WARNING in messages(capturedAppLog, "WARNING")

    def test_fresh_install_logs_no_limitation(self, dataDir, clock, logger, capturedAppLog):
        ctx = load(dataDir, clock, logger)
        assert ctx.openPeriodSource is OpenPeriodSource.NONE
        assert SOURCE_THREE_WARNING not in messages(capturedAppLog, "WARNING")


# --- AC-10: restart after a backward clock step (R11) -------------------------------------


class TestAc10BackwardClockStep:
    def test_restart_leaves_the_log_valid_with_no_backup(
        self, dataDir, clock, logger, capturedAppLog
    ):
        u1 = newId()
        s = R
        writeLog(dataDir, [startEvent(u1, s, sessionId=2, userName="alice")])
        writeStatus(
            dataDir, [activeEntry(2, "alice", u1, connectTime=s)], p=s + timedelta(seconds=60)
        )

        clock.set(s - timedelta(seconds=600))
        tracker = SessionTracker(clock, logging.getLogger(f"{LOGGER_NAME}.domain.tracker"))
        ctx = load(dataDir, clock, logger)
        outcome = reconcileStartup(ctx, tracker, [], clock.now())
        persist(ctx, outcome.events)
        ctx.statusStore.write(outcome.state)

        [end] = [ActivityEvent.fromJson(e) for e in logDocument(dataDir)["events"][1:]]
        assert end.usagePeriodId == u1
        assert end.timestamp >= s
        assert end.periodEnd >= end.usagePeriodStart
        assert end.durationSeconds >= 0
        assert any("wall clock moved backwards" in m for m in messages(capturedAppLog, "WARNING"))

        # Second restart, clock still stepped back.
        before = (dataDir / "activity_log.json").read_bytes()
        again = load(dataDir, clock, logger)
        assert again.activityLogUsable is True
        assert again.openPeriods == ()
        assert corruptFiles(dataDir) == []
        second = reconcileStartup(again, tracker, [], clock.now())
        assert second.events == []
        persist(again, second.events)
        assert (dataDir / "activity_log.json").read_bytes() == before


# --- AC-11: type change during downtime (I3) ----------------------------------------------


class TestAc11TypeChange:
    @pytest.mark.parametrize("changedConnect", [True, False], ids=["c2", "unchanged-c1"])
    def test_console_after_rdp_is_a_reconnect(
        self, dataDir, clock, logger, tracker, changedConnect
    ):
        u1 = newId()
        writeLog(dataDir, [aliceStart(u1)])
        writeStatus(dataDir, [activeEntry(2, "alice", u1)])
        ctx = load(dataDir, clock, logger)

        c2 = R - timedelta(minutes=4)
        snapshot = [alice(connectionType="Console", connectTime=c2 if changedConnect else C1)]
        outcome = reconcileStartup(ctx, tracker, snapshot, R)
        persist(ctx, outcome.events)

        end, start = outcome.events
        assert (end.usagePeriodId, end.endCause) == (u1, EndCause.RECONNECT_DETECTED)
        assert end.reason is EventReason.DETECTED_AT_STARTUP
        assert start.usagePeriodId != u1
        assert start.connectionType is ConnectionType.CONSOLE
        assert start.reason is EventReason.DETECTED_AT_STARTUP
        if changedConnect:
            assert (start.usagePeriodStart, start.durationApproximate) == (c2, False)
        else:
            assert (start.usagePeriodStart, start.durationApproximate) == (R, True)
        assertPairingAcross(dataDir)


# --- source selection details -------------------------------------------------------------


class TestSourceSelection:
    def test_activity_log_wins_when_both_usable_and_disagree(self, dataDir, clock, logger):
        u1, u9, u8 = newId(), newId(), newId()
        writeLog(dataDir, [aliceStart(u1)])
        writeStatus(dataDir, [activeEntry(2, "alice", u9), activeEntry(3, "bob", u8)])

        ctx = load(dataDir, clock, logger)

        assert ctx.openPeriodSource is OpenPeriodSource.ACTIVITY_LOG
        assert [r.usagePeriodId for r in ctx.openPeriods] == [u1]
        assert ctx.openPeriods[0].startEventTimestamp == S1
        assert ctx.lastSuccessfulPollAt == P
        assert ctx.statusUsable is True

    def test_type_invalid_status_is_backed_up_not_used(self, dataDir, clock, logger):
        """G3/G5: validated before conversion; a string sessionId is corrupt, not coerced."""
        writeLog(dataDir, [])
        raw = writeStatus(dataDir, [activeEntry(2, "alice", newId())]).replace(
            b'"sessionId": 2', b'"sessionId": "2"'
        )
        (dataDir / "status.json").write_bytes(raw)

        ctx = load(dataDir, clock, logger)

        assert ctx.statusUsable is False
        assert ctx.statusDocument is None
        assert ctx.lastSuccessfulPollAt is None
        assert (dataDir / f"status.json.corrupt-{STAMP}").read_bytes() == raw
        assert ctx.openPeriodSource is OpenPeriodSource.ACTIVITY_LOG

    def test_type_invalid_event_makes_the_log_unusable(self, dataDir, clock, logger):
        u1 = newId()
        raw = writeLog(dataDir, [aliceStart(u1)]).replace(
            b'"durationApproximate": false', b'"durationApproximate": "no"'
        )
        (dataDir / "activity_log.json").write_bytes(raw)
        writeStatus(dataDir, [activeEntry(2, "alice", u1)])

        ctx = load(dataDir, clock, logger)

        assert ctx.activityLogUsable is False
        assert ctx.openPeriodSource is OpenPeriodSource.STATUS
        assert [r.startEventTimestamp for r in ctx.openPeriods] == [None]

    def test_status_duplicates_are_taken_once(self, dataDir, clock, logger):
        """TASK-005 S10: status.json sessionIds and period ids are not checked for uniqueness."""
        u1 = newId()
        writeStatus(dataDir, [activeEntry(2, "alice", u1), activeEntry(2, "alice", u1)])
        (dataDir / "activity_log.json").unlink(missing_ok=True)
        ctx = load(dataDir, clock, logger)
        assert [r.usagePeriodId for r in ctx.openPeriods] == [u1]

    def test_source_is_logged(self, dataDir, clock, logger, capturedAppLog):
        writeLog(dataDir, [aliceStart(newId())])
        load(dataDir, clock, logger)
        assert "open usage periods rebuilt from activity_log.json: 1" in messages(
            capturedAppLog, "INFO"
        )


class TestLoadingOrderAndNotices:
    def test_leftover_temp_files_are_removed(self, dataDir, clock, logger):
        leftover = dataDir / "status.json.tmp-123-abcdef01"
        leftover.write_bytes(b"partial")
        other = dataDir / "notes.txt"
        other.write_bytes(b"keep")
        load(dataDir, clock, logger)
        assert not leftover.exists()
        assert other.exists()

    def test_config_warnings_are_collected(self, dataDir, clock, logger):
        (dataDir / "config.json").write_text(
            '{"schemaVersion": 1, "dashboardPort": 80}', encoding="utf-8"
        )
        ctx = load(dataDir, clock, logger)
        assert ctx.config.dashboardPort == 8501
        assert any(w.startswith("dashboardPort") for w in ctx.configWarnings)
        assert ctx.notices == ()

    def test_notices_in_file_order_config_status_log(self, dataDir, clock, logger):
        for name in ("config.json", "status.json", "activity_log.json"):
            (dataDir / name).write_bytes(b"\x00garbage")
        ctx = load(dataDir, clock, logger)
        assert [n.fileName for n in ctx.notices] == [
            "config.json",
            "status.json",
            "activity_log.json",
        ]
        assert len(corruptFiles(dataDir)) == 3


# --- pendingDisplayState -------------------------------------------------------------------


class TestPreloadedConfig:
    """TASK-009 S8: the launcher loads config.json first (to check the dashboard port before
    any other data file is touched) and hands the result to ``loadStartup``."""

    def test_a_preloaded_config_is_used_and_config_json_is_not_read_again(
        self, dataDir, clock, logger, monkeypatch
    ):
        (dataDir / "config.json").write_text("not json", encoding="utf-8")  # corrupt
        configResult = loadConfig(DataPaths(dataDir), clock, logger)
        assert configResult.notice is not None  # backed up and reset by loadConfig itself
        backups = sorted(p.name for p in dataDir.glob("config.json.corrupt-*"))

        def noSecondRead(*args, **kwargs):
            pytest.fail("config.json was read a second time")

        monkeypatch.setattr("pc_handoff.persistence.startup_loader.loadConfig", noSecondRead)
        ctx = loadStartup(DataPaths(dataDir), clock, logger, configResult=configResult)

        assert ctx.config == configResult.config
        assert ctx.notices == (configResult.notice,)
        assert sorted(p.name for p in dataDir.glob("config.json.corrupt-*")) == backups
        assert (dataDir / "activity_log.json").read_bytes() == EMPTY_LOG

    def test_without_a_preloaded_config_it_is_loaded_as_before(self, dataDir, clock, logger):
        (dataDir / "config.json").write_text(
            '{"schemaVersion": 1, "dashboardPort": 9000}', encoding="utf-8"
        )
        ctx = load(dataDir, clock, logger)
        assert ctx.config.dashboardPort == 9000


class TestPendingDisplayState:
    def test_last_known_snapshot_from_usable_status(self, dataDir, clock, logger, tracker):
        u1 = newId()
        since = R - timedelta(minutes=50)
        writeStatus(
            dataDir,
            [activeEntry(2, "alice", u1, firstDetectedAt=C1), disconnectedEntry(3, "bob", since)],
        )
        ctx = load(dataDir, clock, logger)

        state: TrackerState = pendingDisplayState(ctx)

        assert state.status is PcStatus.UNKNOWN
        assert state.detectionHealthy is False
        assert state.outagePending is False
        assert state.lastKnownStatus is PcStatus.IN_USE
        assert state.lastKnownStatusAt == P
        assert state.lastSuccessfulPollAt == P
        assert list(state.sessions) == [2, 3]
        assert state.sessions[2].openPeriod.usagePeriodId == u1
        assert state.sessions[2].firstDetectedAt == C1
        assert state.sessions[3].disconnectedSince == since
        assert state.lastPeriodEndBySessionId == {}

        # A failed first poll keeps it, and status.json written from it is valid.
        failed = tracker.applyFailure(state, SessionEnumerationError("timeout")).state
        ctx.statusStore.write(failed)
        status = statusDocument(dataDir)
        assert status["status"] == "Unknown"
        assert status["lastKnownStatus"] == "In Use"
        assert [s["sessionId"] for s in status["sessions"]] == [2, 3]

    def test_duplicate_session_ids_keep_the_first_entry(self, dataDir, clock, logger):
        u1, u2 = newId(), newId()
        writeStatus(dataDir, [activeEntry(2, "alice", u1), activeEntry(2, "zed", u2)])
        state = pendingDisplayState(load(dataDir, clock, logger))
        assert state.sessions[2].userName == "alice"

    def test_empty_when_status_is_not_usable(self, dataDir, clock, logger):
        (dataDir / "status.json").write_bytes(b"{")
        state = pendingDisplayState(load(dataDir, clock, logger))
        assert state.sessions == {}
        assert state.lastKnownStatus is None
        assert state.status is PcStatus.UNKNOWN


# --- the pairing helper itself ------------------------------------------------------------


class TestPairingAcrossHelper:
    def test_second_start_in_a_backup_is_reported(self, dataDir):
        u1 = newId()
        writeLog(dataDir, [aliceStart(u1)])
        (dataDir / f"activity_log.json.corrupt-{STAMP}").write_bytes(
            json.dumps({"schemaVersion": 2, "events": [aliceStart(u1).toJson()]}).encode()
        )
        (dataDir / "activity_log.json.corrupt-x").write_bytes(b"not json")
        (dataDir / "activity_log.json.corrupt-y").write_bytes(b'{"events": 3}')
        problems = pairingProblemsAcross(dataDir)
        assert len(problems) == 1
        assert "second session_start" in problems[0]

    def test_end_before_start_across_files_is_reported(self, dataDir):
        u1 = newId()
        writeLog(dataDir, [aliceStart(u1)])
        early = endEvent(u1, S1 - timedelta(seconds=1), usagePeriodStart=C1)
        (dataDir / f"activity_log.json.corrupt-{STAMP}").write_bytes(
            serializeCanonical({}, [early.toJson(), {"eventType": "other"}, 5])
        )
        [problem] = pairingProblemsAcross(dataDir)
        assert "precedes its start" in problem

    def test_missing_log_and_no_backups_is_fine(self, dataDir):
        assert pairingProblemsAcross(dataDir) == []
