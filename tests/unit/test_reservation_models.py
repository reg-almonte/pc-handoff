"""Unit tests for reservation, handoff, status note, and audit event domain models (TASK-012)."""

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from pc_handoff.domain.models import EventReason
from pc_handoff.domain.reservations import (
    HandoffSession,
    HandoffState,
    HandoffStatus,
    QueueState,
    Reservation,
    ReservationAuditEvent,
    ReservationEventType,
    ReservationsDocument,
    ReservationStatus,
    StatusNote,
    emptyQueueState,
    newReservationEvent,
    newStatusNoteEvent,
    normalizeUserKey,
    validateDurationMinutes,
    validateMessage,
    validateNotes,
    validateTtlMinutes,
    validateUserName,
)

TZ = timezone(timedelta(hours=9))
T0 = datetime(2026, 10, 23, 19, 0, 0, tzinfo=TZ)
RES_ID = "7f9c2ba0-4d3b-4c6e-8112-90a2b3c4d5e6"
NOTE_ID = "3a1b2c3d-4e5f-4a6b-8c9d-0e1f2a3b4c5d"


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


# --- enums & aliases ----------------------------------------------------------------------


def test_reservation_and_handoff_enums() -> None:
    assert [s.value for s in ReservationStatus] == [
        "QUEUED",
        "READY",
        "ACTIVE",
        "COMPLETED",
        "CANCELLED",
        "EXPIRED",
    ]
    assert not ReservationStatus.QUEUED.isTerminal
    assert not ReservationStatus.READY.isTerminal
    assert not ReservationStatus.ACTIVE.isTerminal
    assert ReservationStatus.COMPLETED.isTerminal
    assert ReservationStatus.CANCELLED.isTerminal
    assert ReservationStatus.EXPIRED.isTerminal

    assert [s.value for s in HandoffStatus] == ["IDLE", "OFFERED", "CLAIMED", "TIMED_OUT"]
    assert [e.value for e in ReservationEventType] == [
        "reservation_queued",
        "reservation_cancelled",
        "handoff_initiated",
        "handoff_claimed",
        "reservation_expired",
        "status_note_posted",
    ]
    assert HandoffSession is HandoffState
    assert ReservationsDocument is QueueState


# --- validation helpers -------------------------------------------------------------------


def test_validation_helpers_accept_valid_inputs() -> None:
    assert normalizeUserKey("  Alice  ") == "alice"
    assert validateUserName("  Alice  ") == "Alice"
    assert validateDurationMinutes(5) == 5
    assert validateDurationMinutes(480) == 480
    assert validateNotes(None) is None
    assert validateNotes("   ") is None
    assert validateNotes("  running tests ") == "running tests"
    assert validateMessage("  hello team ") == "hello team"
    assert validateTtlMinutes(5) == 5
    assert validateTtlMinutes(1440) == 1440


@pytest.mark.parametrize(
    ("fn", "arg"),
    [
        (validateUserName, 123),
        (validateUserName, "   "),
        (validateUserName, "a" * 65),
        (validateDurationMinutes, True),
        (validateDurationMinutes, "60"),
        (validateDurationMinutes, 4),
        (validateDurationMinutes, 481),
        (validateNotes, 42),
        (validateNotes, "x" * 281),
        (validateMessage, None),
        (validateMessage, "   "),
        (validateMessage, "m" * 281),
        (validateTtlMinutes, False),
        (validateTtlMinutes, "30"),
        (validateTtlMinutes, 4),
        (validateTtlMinutes, 1441),
    ],
)
def test_validation_helpers_reject_invalid_inputs(fn, arg) -> None:
    with pytest.raises(ValueError):
        fn(arg)


# --- Reservation, HandoffState, StatusNote, QueueState JSON round-trip --------------------


def test_reservation_json_round_trip() -> None:
    res = Reservation(
        reservationId=RES_ID,
        userName="alice",
        requestedDurationMinutes=60,
        queuedAt=at(0),
        status=ReservationStatus.READY,
        notes="Hardware debug",
        readyAt=at(5),
        claimExpiresAt=at(10),
        activatedAt=None,
        endedAt=None,
    )
    doc = res.toJson()
    assert doc["status"] == "READY"
    assert doc["readyAt"] == "2026-10-23T19:05:00+09:00"
    assert Reservation.fromJson(json.loads(json.dumps(doc))) == res


def test_handoff_state_json_round_trip() -> None:
    idle = HandoffState.idle()
    assert HandoffState.fromJson(idle.toJson()) == idle

    offered = HandoffState(
        status=HandoffStatus.OFFERED,
        reservationId=RES_ID,
        offeredByUserName="alice",
        targetUserName="bob",
        offeredAt=at(0),
        expiresAt=at(5),
    )
    assert HandoffState.fromJson(json.loads(json.dumps(offered.toJson()))) == offered


def test_status_note_json_round_trip() -> None:
    note = StatusNote(
        noteId=NOTE_ID,
        userName="alice",
        message="Finishing in 10 mins",
        postedAt=at(0),
        expiresAt=at(120),
    )
    assert StatusNote.fromJson(json.loads(json.dumps(note.toJson()))) == note


def test_queue_state_helpers_and_json_round_trip() -> None:
    empty = emptyQueueState(at(0))
    assert empty.activeReservation is None
    assert empty.readyReservation is None
    assert empty.queuedReservations == ()
    assert empty.nonTerminalReservations == ()
    assert empty.findByUser("alice") is None
    assert empty.findById(RES_ID) is None

    rActive = Reservation(
        reservationId=str(uuid.uuid4()),
        userName="Alice",
        requestedDurationMinutes=30,
        queuedAt=at(0),
        status=ReservationStatus.ACTIVE,
        activatedAt=at(1),
    )
    rReady = Reservation(
        reservationId=RES_ID,
        userName="Bob",
        requestedDurationMinutes=45,
        queuedAt=at(2),
        status=ReservationStatus.READY,
        readyAt=at(5),
        claimExpiresAt=at(10),
    )
    rQueued = Reservation(
        reservationId=str(uuid.uuid4()),
        userName="Carol",
        requestedDurationMinutes=60,
        queuedAt=at(3),
        status=ReservationStatus.QUEUED,
    )
    rDone = Reservation(
        reservationId=str(uuid.uuid4()),
        userName="Dave",
        requestedDurationMinutes=15,
        queuedAt=at(0),
        status=ReservationStatus.COMPLETED,
        endedAt=at(4),
    )
    note = StatusNote(
        noteId=NOTE_ID,
        userName="Alice",
        message="Handing off to Bob",
        postedAt=at(4),
        expiresAt=at(60),
    )
    state = QueueState(
        updatedAt=at(5),
        reservations=(rActive, rReady, rQueued, rDone),
        handoff=HandoffState(
            status=HandoffStatus.OFFERED,
            reservationId=RES_ID,
            offeredByUserName="Alice",
            targetUserName="Bob",
            offeredAt=at(5),
            expiresAt=at(10),
        ),
        statusNotes=(note,),
    )
    assert state.activeReservation == rActive
    assert state.readyReservation == rReady
    assert state.queuedReservations == (rQueued,)
    assert state.nonTerminalReservations == (rActive, rReady, rQueued)
    assert state.findByUser("  bob ") == rReady
    assert state.findByUser("dave") is None  # terminal reservations are ignored by findByUser
    assert state.findById(RES_ID) == rReady
    assert state.findById("non-existent") is None

    raw = json.loads(json.dumps(state.toJson()))
    assert QueueState.fromJson(raw) == state


@pytest.mark.parametrize(
    ("cls", "badDoc"),
    [
        (Reservation, {"reservationId": RES_ID}),
        (HandoffState, {"status": "INVALID"}),
        (StatusNote, {"noteId": NOTE_ID}),
        (QueueState, {"schemaVersion": 1}),
    ],
)
def test_models_from_json_reject_malformed_dicts(cls, badDoc) -> None:
    with pytest.raises(ValueError):
        cls.fromJson(badDoc)


# --- ReservationAuditEvent ----------------------------------------------------------------


def test_reservation_audit_events_round_trip() -> None:
    res = Reservation(
        reservationId=RES_ID,
        userName="bob",
        requestedDurationMinutes=60,
        queuedAt=at(0),
        status=ReservationStatus.READY,
        notes="Urgent build",
    )
    ev = newReservationEvent(
        res,
        ReservationEventType.HANDOFF_INITIATED,
        at(5),
        reason=EventReason.OBSERVED,
        offeredByUserName="alice",
    )
    doc = ev.toJson()
    assert doc["eventType"] == "handoff_initiated"
    assert doc["offeredByUserName"] == "alice"
    assert ReservationAuditEvent.fromJson(doc) == ev

    evNoOfferer = newReservationEvent(
        res,
        ReservationEventType.RESERVATION_QUEUED,
        at(0),
    )
    assert ReservationAuditEvent.fromJson(evNoOfferer.toJson()) == evNoOfferer

    note = StatusNote(
        noteId=NOTE_ID,
        userName="alice",
        message="Tests running",
        postedAt=at(1),
        expiresAt=at(61),
    )
    noteEv = newStatusNoteEvent(note, at(1))
    noteDoc = noteEv.toJson()
    assert noteDoc["eventType"] == "status_note_posted"
    assert ReservationAuditEvent.fromJson(noteDoc) == noteEv


def test_reservation_audit_event_to_json_validates_required_fields() -> None:
    badNoteEvent = ReservationAuditEvent(
        eventId=str(uuid.uuid4()),
        eventType=ReservationEventType.STATUS_NOTE_POSTED,
        timestamp=at(0),
        userName="alice",
        reason=EventReason.OBSERVED,
    )
    with pytest.raises(ValueError, match="status_note_posted"):
        badNoteEvent.toJson()

    badResEvent = ReservationAuditEvent(
        eventId=str(uuid.uuid4()),
        eventType=ReservationEventType.RESERVATION_QUEUED,
        timestamp=at(0),
        userName="alice",
        reason=EventReason.OBSERVED,
    )
    with pytest.raises(ValueError, match="reservation_queued"):
        badResEvent.toJson()
