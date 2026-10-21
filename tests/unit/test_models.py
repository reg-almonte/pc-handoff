"""Unit tests for ``pc_handoff.domain.models`` (TASK-003 AC-6, AC-8; ADR-007 R11)."""

import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from pc_handoff.detection.provider import ConnectionType
from pc_handoff.domain.models import (
    ActivityEvent,
    EndCause,
    EventReason,
    EventType,
    PcStatus,
    SessionState,
    StatusDocument,
    StatusSession,
    TrackedSession,
    UsagePeriod,
    newEndEvent,
    newStartEvent,
)

TZ = timezone(timedelta(hours=9))
PERIOD_ID = "0b6f0b1e-3c55-4c86-9d64-6a9d1fd0c0a1"
LOGGER = logging.getLogger("pc_handoff.domain.test")

# Requirement section 5, listed verbatim and in order.
START_EVENT_KEYS = [
    "eventId",
    "eventType",
    "usagePeriodId",
    "timestamp",
    "sessionId",
    "userName",
    "domain",
    "logonTime",
    "clientName",
    "clientAddress",
    "connectionType",
    "connectTime",
    "usagePeriodStart",
    "durationApproximate",
    "reason",
]
END_ONLY_KEYS = ["periodEnd", "durationSeconds", "endCause"]
STATUS_KEYS = [
    "schemaVersion",
    "status",
    "lastKnownStatus",
    "lastKnownStatusAt",
    "lastSuccessfulPollAt",
    "updatedAt",
    "detectionHealthy",
    "sessions",
]
STATUS_SESSION_KEYS = [
    "sessionId",
    "userName",
    "domain",
    "state",
    "connectionType",
    "clientName",
    "clientAddress",
    "logonTime",
    "firstDetectedAt",
    "connectTime",
    "usagePeriodId",
    "usagePeriodStart",
    "durationApproximate",
    "disconnectedSince",
]


def t(hhmmss: str | None) -> datetime | None:
    if hhmmss is None:
        return None
    h, m, s = (int(part) for part in hhmmss.split(":"))
    return datetime(2026, 10, 21, h, m, s, tzinfo=TZ)


def makePeriod(
    start: str = "10:00:00",
    startEvent: str | None = "10:00:03",
    approximate: bool = False,
    periodId: str = PERIOD_ID,
) -> UsagePeriod:
    return UsagePeriod(
        usagePeriodId=periodId,
        usagePeriodStart=t(start),
        durationApproximate=approximate,
        startEventTimestamp=t(startEvent),
        connectTimeAtStart=None if approximate else t(start),
        connectionTypeAtStart=ConnectionType.RDP,
    )


def makeSession(
    state: SessionState = SessionState.ACTIVE,
    openPeriod: UsagePeriod | None = None,
    clientName: str | None = None,
    clientAddress: str | None = None,
    disconnectedSince: datetime | None = None,
    connectionType: ConnectionType = ConnectionType.RDP,
) -> TrackedSession:
    return TrackedSession(
        sessionId=2,
        userName="alice",
        domain="TEAM",
        state=state,
        connectionType=connectionType,
        clientName=clientName,
        clientAddress=clientAddress,
        logonTime=t("09:00:00"),
        firstDetectedAt=t("10:00:03"),
        connectTime=t("10:00:00"),
        osDisconnectTime=None,
        disconnectedSince=disconnectedSince,
        openPeriod=openPeriod,
        consecutiveTransitionalPolls=0,
    )


def warningsAndErrors(lines: list[dict]) -> list[dict]:
    return [line for line in lines if line["level"] in ("WARNING", "ERROR")]


# --- enums ------------------------------------------------------------------------------


def test_enum_values_match_requirement() -> None:
    assert [s.value for s in SessionState] == ["Active", "Disconnected"]
    assert [c.value for c in EndCause] == ["disconnect", "logoff", "reconnect_detected", "unknown"]
    assert [r.value for r in EventReason] == [
        "observed",
        "detected_after_outage",
        "detected_at_startup",
    ]
    assert [e.value for e in EventType] == ["session_start", "session_end"]


# --- newStartEvent ----------------------------------------------------------------------


def test_new_start_event_fields() -> None:
    period = makePeriod()
    sess = makeSession(openPeriod=period, clientName="ALICE-LAPTOP", clientAddress="192.168.1.20")
    event = newStartEvent(sess, period, EventReason.OBSERVED, t("10:00:03"))

    assert uuid.UUID(event.eventId).version == 4
    assert event.eventType is EventType.SESSION_START
    assert event.usagePeriodId == PERIOD_ID
    assert event.timestamp == t("10:00:03")
    assert (event.sessionId, event.userName, event.domain) == (2, "alice", "TEAM")
    assert event.logonTime == t("09:00:00")
    assert (event.clientName, event.clientAddress) == ("ALICE-LAPTOP", "192.168.1.20")
    assert event.connectionType is ConnectionType.RDP
    assert event.connectTime == t("10:00:00")
    assert event.usagePeriodStart == t("10:00:00")
    assert event.durationApproximate is False
    assert event.reason is EventReason.OBSERVED
    assert (event.periodEnd, event.durationSeconds, event.endCause) == (None, None, None)


def test_new_start_event_ids_are_unique() -> None:
    period = makePeriod()
    sess = makeSession(openPeriod=period)
    ids = {newStartEvent(sess, period, EventReason.OBSERVED, t("10:00:03")).eventId for _ in "abc"}
    assert len(ids) == 3


def test_event_connection_fields_come_from_the_period() -> None:
    # After a type change the session already reports Console and no connect time; the end
    # event still describes the ended period's own connection.
    period = makePeriod()
    sess = replace(makeSession(), connectionType=ConnectionType.CONSOLE, connectTime=None)
    event = newEndEvent(
        sess,
        period,
        t("10:00:03"),
        EndCause.RECONNECT_DETECTED,
        EventReason.OBSERVED,
        True,
        t("10:10:00"),
        LOGGER,
    )
    assert event.connectionType is ConnectionType.RDP
    assert event.connectTime == t("10:00:00")


# --- AC-6: newEndEvent and the R11 clamps -------------------------------------------------


def test_ac06_backward_step_clamps_both_fields_with_one_warning(
    capturedAppLog: Callable[[], list[dict]],
) -> None:
    period = makePeriod("10:00:00", "10:00:03")
    event = newEndEvent(
        makeSession(SessionState.DISCONNECTED),
        period,
        t("09:59:30"),
        EndCause.DISCONNECT,
        EventReason.OBSERVED,
        False,
        t("09:59:30"),
        LOGGER,
    )
    assert event.eventType is EventType.SESSION_END
    assert event.timestamp == t("10:00:03")
    assert event.periodEnd == t("10:00:00")
    assert event.durationSeconds == 0

    logged = warningsAndErrors(capturedAppLog())
    assert len(logged) == 1
    assert logged[0]["level"] == "WARNING"
    assert logged[0]["message"] == (
        "wall clock moved backwards by 33 s; end clamped "
        f"(usagePeriodId {PERIOD_ID}, fields timestamp, periodEnd)"
    )


def test_ac06_no_step_nothing_changed_nothing_logged(
    capturedAppLog: Callable[[], list[dict]],
) -> None:
    period = makePeriod("10:00:00", "10:00:03")
    event = newEndEvent(
        makeSession(SessionState.DISCONNECTED),
        period,
        t("10:05:00"),
        EndCause.DISCONNECT,
        EventReason.OBSERVED,
        False,
        t("10:05:00"),
        LOGGER,
    )
    assert event.timestamp == t("10:05:00")
    assert event.periodEnd == t("10:05:00")
    assert event.durationSeconds == 300
    assert capturedAppLog() == []


def test_ac06_null_start_event_timestamp_keeps_now(
    capturedAppLog: Callable[[], list[dict]],
) -> None:
    # Source-2 period (no start event known): the timestamp is not clamped, only periodEnd.
    period = makePeriod("10:00:00", None)
    event = newEndEvent(
        makeSession(SessionState.DISCONNECTED),
        period,
        t("09:59:30"),
        EndCause.LOGOFF,
        EventReason.DETECTED_AT_STARTUP,
        True,
        t("09:59:30"),
        LOGGER,
    )
    assert event.timestamp == t("09:59:30")
    assert event.periodEnd == t("10:00:00")
    assert event.durationSeconds == 0
    logged = warningsAndErrors(capturedAppLog())
    assert [line["level"] for line in logged] == ["WARNING"]
    assert logged[0]["message"].endswith("fields periodEnd)")
    assert "by 30 s" in logged[0]["message"]


def test_timestamp_only_clamp_names_only_timestamp(
    capturedAppLog: Callable[[], list[dict]],
) -> None:
    period = makePeriod("10:00:00", "10:00:03")
    event = newEndEvent(
        makeSession(),
        period,
        None,
        EndCause.LOGOFF,
        EventReason.DETECTED_AT_STARTUP,
        True,
        t("10:00:01"),
        LOGGER,
    )
    assert event.timestamp == t("10:00:03")
    assert event.periodEnd is None
    assert event.durationSeconds is None
    logged = warningsAndErrors(capturedAppLog())
    assert len(logged) == 1
    assert logged[0]["message"] == (
        "wall clock moved backwards by 2 s; end clamped "
        f"(usagePeriodId {PERIOD_ID}, fields timestamp)"
    )


def test_end_event_fields_are_carried(capturedAppLog: Callable[[], list[dict]]) -> None:
    period = makePeriod("10:00:00", "10:00:03", approximate=True)
    event = newEndEvent(
        makeSession(),
        period,
        t("10:30:00"),
        EndCause.RECONNECT_DETECTED,
        EventReason.DETECTED_AFTER_OUTAGE,
        True,
        t("10:40:00"),
        LOGGER,
    )
    assert event.timestamp == t("10:40:00")
    assert event.periodEnd == t("10:30:00")
    assert event.durationSeconds == 1800
    assert event.endCause is EndCause.RECONNECT_DETECTED
    assert event.reason is EventReason.DETECTED_AFTER_OUTAGE
    assert event.durationApproximate is True
    assert event.usagePeriodStart == t("10:00:00")
    assert event.connectTime is None  # approximate period: no connect time at start
    assert capturedAppLog() == []


# --- AC-8: JSON shape and round trip ------------------------------------------------------


def test_ac08_event_json_shape_nulls_and_round_trip() -> None:
    period = makePeriod()
    sess = makeSession(openPeriod=period, clientName=None, clientAddress=None)
    start = newStartEvent(sess, period, EventReason.OBSERVED, t("10:00:03"))
    end = newEndEvent(
        replace(sess, state=SessionState.DISCONNECTED, openPeriod=None),
        period,
        t("10:30:01"),
        EndCause.DISCONNECT,
        EventReason.OBSERVED,
        False,
        t("10:30:05"),
        LOGGER,
    )

    startJson = start.toJson()
    endJson = end.toJson()
    assert list(startJson) == START_EVENT_KEYS
    assert list(endJson) == START_EVENT_KEYS + END_ONLY_KEYS

    # Nulls survive serialization as JSON null.
    for doc in (startJson, endJson):
        text = json.dumps(doc)
        assert '"clientName": null' in text
        assert '"clientAddress": null' in text

    assert startJson["eventType"] == "session_start"
    assert startJson["timestamp"] == "2026-10-21T10:00:03+09:00"
    assert startJson["connectionType"] == "RDP"
    assert startJson["reason"] == "observed"
    assert startJson["sessionId"] == 2
    assert startJson["durationApproximate"] is False
    assert endJson["eventType"] == "session_end"
    assert endJson["periodEnd"] == "2026-10-21T10:30:01+09:00"
    assert endJson["durationSeconds"] == 1801
    assert endJson["endCause"] == "disconnect"

    assert ActivityEvent.fromJson(json.loads(json.dumps(startJson))) == start
    assert ActivityEvent.fromJson(json.loads(json.dumps(endJson))) == end


def test_end_event_with_null_period_end_serializes_nulls() -> None:
    period = makePeriod("10:00:00", None)
    end = newEndEvent(
        makeSession(),
        period,
        None,
        EndCause.LOGOFF,
        EventReason.DETECTED_AT_STARTUP,
        True,
        t("11:00:00"),
        LOGGER,
    )
    doc = end.toJson()
    assert doc["periodEnd"] is None
    assert doc["durationSeconds"] is None
    assert ActivityEvent.fromJson(doc) == end


def test_from_json_ignores_unknown_keys() -> None:
    period = makePeriod()
    start = newStartEvent(
        makeSession(openPeriod=period), period, EventReason.OBSERVED, t("10:00:03")
    )
    doc = start.toJson() | {"futureField": 1}
    assert ActivityEvent.fromJson(doc) == start


def validStartJson() -> dict:
    period = makePeriod()
    return newStartEvent(
        makeSession(openPeriod=period), period, EventReason.OBSERVED, t("10:00:03")
    ).toJson()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.pop("userName"),
        lambda d: d.update(eventType="session_pause"),
        lambda d: d.update(reason="guess"),
        lambda d: d.update(timestamp="2026-10-21T10:00:03"),  # naive
        lambda d: d.update(timestamp=12),
        lambda d: d.update(connectionType="Telnet"),
    ],
    ids=["missing-key", "bad-event-type", "bad-reason", "naive-ts", "ts-not-str", "bad-type"],
)
def test_event_from_json_rejects_bad_input(mutate: Callable[[dict], object]) -> None:
    doc = validStartJson()
    mutate(doc)
    with pytest.raises(ValueError):
        ActivityEvent.fromJson(doc)


def test_end_event_from_json_requires_end_keys() -> None:
    doc = validStartJson() | {"eventType": "session_end"}
    with pytest.raises(ValueError):
        ActivityEvent.fromJson(doc)


def test_event_from_json_rejects_non_object() -> None:
    with pytest.raises(ValueError):
        ActivityEvent.fromJson(["not", "an", "object"])  # type: ignore[arg-type]


# --- StatusDocument -----------------------------------------------------------------------


def test_status_session_from_active_tracked_session() -> None:
    period = makePeriod(approximate=True)
    # A stale disconnectedSince on an Active record is never serialized.
    sess = makeSession(openPeriod=period, disconnectedSince=t("09:30:00"))
    entry = StatusSession.fromTracked(sess)
    assert entry.usagePeriodId == PERIOD_ID
    assert entry.usagePeriodStart == t("10:00:00")
    assert entry.durationApproximate is True
    assert entry.disconnectedSince is None
    assert entry.state is SessionState.ACTIVE


def test_status_session_from_disconnected_tracked_session() -> None:
    sess = makeSession(SessionState.DISCONNECTED, openPeriod=None, disconnectedSince=t("10:30:01"))
    entry = StatusSession.fromTracked(sess)
    assert entry.usagePeriodId is None
    assert entry.usagePeriodStart is None
    assert entry.durationApproximate is False
    assert entry.disconnectedSince == t("10:30:01")


def test_status_session_disconnected_ignores_leftover_period() -> None:
    # Defensive: a Disconnected record never exposes a period, even if one were left behind.
    sess = makeSession(
        SessionState.DISCONNECTED,
        openPeriod=makePeriod(approximate=True),
        disconnectedSince=t("10:30:01"),
    )
    entry = StatusSession.fromTracked(sess)
    assert (entry.usagePeriodId, entry.usagePeriodStart) == (None, None)
    assert entry.durationApproximate is False


def test_status_session_active_without_period_is_rejected() -> None:
    # Only a tracker bug can produce this; serializing it would write a status.json that the
    # ADR-005 startup check rejects (Active requires a non-null usagePeriodId/usagePeriodStart).
    with pytest.raises(ValueError, match=r"\(sessionId 2\)"):
        StatusSession.fromTracked(makeSession(openPeriod=None))


def test_status_document_json_shape_and_round_trip() -> None:
    period = makePeriod()
    active = makeSession(openPeriod=period, clientName=None, clientAddress=None)
    disconnected = replace(
        makeSession(SessionState.DISCONNECTED, disconnectedSince=t("10:20:00")),
        sessionId=3,
        userName="bob",
        domain=None,
        logonTime=None,
        connectTime=None,
        connectionType=ConnectionType.CONSOLE,
    )
    doc = StatusDocument(
        status=PcStatus.IN_USE,
        lastKnownStatus=PcStatus.IN_USE,
        lastKnownStatusAt=t("10:30:05"),
        lastSuccessfulPollAt=t("10:30:05"),
        updatedAt=t("10:30:05"),
        detectionHealthy=True,
        sessions=(StatusSession.fromTracked(active), StatusSession.fromTracked(disconnected)),
    )
    data = doc.toJson()
    assert list(data) == STATUS_KEYS
    assert data["schemaVersion"] == 1
    assert data["status"] == "In Use"
    assert data["lastKnownStatus"] == "In Use"
    assert data["updatedAt"] == "2026-10-21T10:30:05+09:00"
    assert [list(s) for s in data["sessions"]] == [STATUS_SESSION_KEYS, STATUS_SESSION_KEYS]

    first, second = data["sessions"]
    assert first["state"] == "Active"
    assert first["usagePeriodId"] == PERIOD_ID
    assert first["usagePeriodStart"] == "2026-10-21T10:00:00+09:00"
    assert first["disconnectedSince"] is None
    assert first["clientName"] is None and first["clientAddress"] is None
    assert second["state"] == "Disconnected"
    assert second["connectionType"] == "Console"
    assert second["usagePeriodId"] is None and second["usagePeriodStart"] is None
    assert second["durationApproximate"] is False
    assert second["disconnectedSince"] == "2026-10-21T10:20:00+09:00"
    assert second["domain"] is None and second["logonTime"] is None

    assert StatusDocument.fromJson(json.loads(json.dumps(data))) == doc


def test_status_document_unknown_with_nulls_round_trip() -> None:
    # REQ AC-27 shape: first poll failed on a fresh install.
    doc = StatusDocument(
        status=PcStatus.UNKNOWN,
        lastKnownStatus=None,
        lastKnownStatusAt=None,
        lastSuccessfulPollAt=None,
        updatedAt=t("10:00:00"),
        detectionHealthy=False,
        sessions=(),
    )
    data = doc.toJson()
    assert data == {
        "schemaVersion": 1,
        "status": "Unknown",
        "lastKnownStatus": None,
        "lastKnownStatusAt": None,
        "lastSuccessfulPollAt": None,
        "updatedAt": "2026-10-21T10:00:00+09:00",
        "detectionHealthy": False,
        "sessions": [],
    }
    assert StatusDocument.fromJson(data) == doc


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.pop("updatedAt"),
        lambda d: d.update(status="Maybe"),
        lambda d: d.update(sessions=[{"sessionId": 2}]),
        lambda d: d.update(sessions=None),
    ],
    ids=["missing-key", "bad-status", "bad-session", "sessions-null"],
)
def test_status_document_from_json_rejects_bad_input(mutate: Callable[[dict], object]) -> None:
    data = StatusDocument(
        status=PcStatus.AVAILABLE,
        lastKnownStatus=PcStatus.AVAILABLE,
        lastKnownStatusAt=t("10:00:00"),
        lastSuccessfulPollAt=t("10:00:00"),
        updatedAt=t("10:00:00"),
        detectionHealthy=True,
        sessions=(),
    ).toJson()
    mutate(data)
    with pytest.raises(ValueError):
        StatusDocument.fromJson(data)
