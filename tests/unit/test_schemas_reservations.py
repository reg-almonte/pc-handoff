"""Unit tests for ``reservations.json`` and reservation activity event schemas (TASK-012)."""

import copy
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from pc_handoff.domain.models import EventReason
from pc_handoff.domain.reservations import (
    HandoffState,
    HandoffStatus,
    NoteCategory,
    QueueState,
    Reservation,
    ReservationEventType,
    ReservationStatus,
    StatusNote,
    newReservationEvent,
    newStatusNoteEvent,
)
from pc_handoff.persistence.schemas import (
    ValidationIssue,
    validateActivityDocument,
    validateReservationsDocument,
)

TZ = timezone(timedelta(hours=9))
T0 = datetime(2026, 10, 23, 19, 0, 0, tzinfo=TZ)
RES_ID_1 = "7f9c2ba0-4d3b-4c6e-8112-90a2b3c4d5e6"
RES_ID_2 = "8a0d3cb1-5e4c-4d7f-9223-01b3c4d5e6f7"
NOTE_ID_1 = "3a1b2c3d-4e5f-4a6b-8c9d-0e1f2a3b4c5d"


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def paths(issues: list[ValidationIssue]) -> list[str]:
    return [i.path for i in issues]


def goldenReservationsDoc() -> dict[str, Any]:
    rReady = Reservation(
        reservationId=RES_ID_1,
        userName="bob",
        requestedDurationMinutes=60,
        queuedAt=at(0),
        status=ReservationStatus.READY,
        notes="Hardware test",
        readyAt=at(5),
        claimExpiresAt=at(10),
    )
    rQueued = Reservation(
        reservationId=RES_ID_2,
        userName="carol",
        requestedDurationMinutes=30,
        queuedAt=at(2),
        status=ReservationStatus.QUEUED,
    )
    note = StatusNote(
        noteId=NOTE_ID_1,
        userName="alice",
        message="Finishing up soon",
        postedAt=at(4),
        expiresAt=at(124),
    )
    state = QueueState(
        updatedAt=at(5),
        handoff=HandoffState(
            status=HandoffStatus.OFFERED,
            reservationId=RES_ID_1,
            offeredByUserName="alice",
            targetUserName="bob",
            offeredAt=at(5),
            expiresAt=at(10),
        ),
        reservations=(rReady, rQueued),
        statusNotes=(note,),
    )
    return state.toJson()


# --- validateReservationsDocument ---------------------------------------------------------


def test_golden_and_empty_reservations_documents_are_valid() -> None:
    assert validateReservationsDocument(goldenReservationsDoc()) == []
    empty = QueueState(updatedAt=at(0)).toJson()
    assert validateReservationsDocument(empty) == []


def test_reservations_document_non_dict_and_missing_fields() -> None:
    assert paths(validateReservationsDocument([])) == ["$"]
    assert "$.schemaVersion" in paths(validateReservationsDocument({}))


def test_handoff_conditional_null_rules() -> None:
    # IDLE handoff with non-null reservationId is rejected.
    docIdleBad = QueueState(updatedAt=at(0)).toJson()
    docIdleBad["handoff"]["reservationId"] = RES_ID_1
    assert "$.handoff.reservationId" in paths(validateReservationsDocument(docIdleBad))

    # OFFERED handoff with null targetUserName is rejected.
    docOfferedBad = goldenReservationsDoc()
    docOfferedBad["handoff"]["targetUserName"] = None
    assert "$.handoff.targetUserName" in paths(validateReservationsDocument(docOfferedBad))


def test_reservation_conditional_nulls_and_uniqueness_rules() -> None:
    # QUEUED with non-null readyAt is rejected.
    docQueuedBad = goldenReservationsDoc()
    docQueuedBad["reservations"][1]["readyAt"] = at(3).isoformat()
    assert "$.reservations[1].readyAt" in paths(validateReservationsDocument(docQueuedBad))

    # READY with null claimExpiresAt is rejected.
    docReadyBad = goldenReservationsDoc()
    docReadyBad["reservations"][0]["claimExpiresAt"] = None
    assert "$.reservations[0].claimExpiresAt" in paths(validateReservationsDocument(docReadyBad))

    # Duplicate reservationId is rejected.
    docDupRes = goldenReservationsDoc()
    docDupRes["reservations"][1]["reservationId"] = RES_ID_1
    assert "$.reservations[1].reservationId" in paths(validateReservationsDocument(docDupRes))

    # Two READY reservations are rejected.
    docTwoReady = goldenReservationsDoc()
    docTwoReady["reservations"][1] = copy.deepcopy(docTwoReady["reservations"][0])
    docTwoReady["reservations"][1]["reservationId"] = str(uuid.uuid4())
    assert "$.reservations[1].status" in paths(validateReservationsDocument(docTwoReady))

    # Two ACTIVE reservations are rejected.
    docTwoActive = goldenReservationsDoc()
    for idx in (0, 1):
        docTwoActive["reservations"][idx]["status"] = "ACTIVE"
        docTwoActive["reservations"][idx]["claimExpiresAt"] = None
        docTwoActive["reservations"][idx]["activatedAt"] = at(6).isoformat()
    assert "$.reservations[1].status" in paths(validateReservationsDocument(docTwoActive))


def test_status_notes_uniqueness_and_bounds() -> None:
    docDupNote = goldenReservationsDoc()
    docDupNote["statusNotes"].append(copy.deepcopy(docDupNote["statusNotes"][0]))
    assert "$.statusNotes[1].noteId" in paths(validateReservationsDocument(docDupNote))


@pytest.mark.parametrize(
    ("mutate", "expectedPath"),
    [
        (lambda d: d["reservations"][0].update(userName="   "), "$.reservations[0].userName"),
        (
            lambda d: d["reservations"][0].update(userName="u" * 65),
            "$.reservations[0].userName",
        ),
        (
            lambda d: d["reservations"][0].update(requestedDurationMinutes=3),
            "$.reservations[0].requestedDurationMinutes",
        ),
        (
            lambda d: d["reservations"][0].update(requestedDurationMinutes=500),
            "$.reservations[0].requestedDurationMinutes",
        ),
        (
            lambda d: d["reservations"][0].update(notes="n" * 281),
            "$.reservations[0].notes",
        ),
        (
            lambda d: d["statusNotes"][0].update(message="   "),
            "$.statusNotes[0].message",
        ),
        (
            lambda d: d["statusNotes"][0].update(message="m" * 281),
            "$.statusNotes[0].message",
        ),
        (lambda d: d["reservations"].append("not-a-dict"), "$.reservations[2]"),
        (lambda d: d["statusNotes"].append("not-a-dict"), "$.statusNotes[1]"),
        (
            lambda d: d["reservations"][0].update(reservationId="not-a-uuid"),
            "$.reservations[0].reservationId",
        ),
        (
            lambda d: d["statusNotes"][0].update(noteId="not-a-uuid"),
            "$.statusNotes[0].noteId",
        ),
        (
            lambda d: d["statusNotes"][0].update(category="invalid"),
            "$.statusNotes[0].category",
        ),
    ],
)
def test_reservations_field_bounds_and_types(mutate, expectedPath: str) -> None:
    doc = goldenReservationsDoc()
    mutate(doc)
    assert expectedPath in paths(validateReservationsDocument(doc))


# --- reservation & status note events in activity_log.json --------------------------------


@pytest.mark.parametrize(
    "eventType",
    [
        ReservationEventType.RESERVATION_QUEUED,
        ReservationEventType.RESERVATION_UPDATED,
        ReservationEventType.RESERVATION_CANCELLED,
        ReservationEventType.HANDOFF_INITIATED,
        ReservationEventType.HANDOFF_CLAIMED,
        ReservationEventType.RESERVATION_EXPIRED,
    ],
)
def test_reservation_events_pass_activity_document_validation(
    eventType: ReservationEventType,
) -> None:
    res = Reservation(
        reservationId=RES_ID_1,
        userName="bob",
        requestedDurationMinutes=60,
        queuedAt=at(0),
        status=ReservationStatus.READY,
        notes="Urgent",
    )
    ev = newReservationEvent(
        res,
        eventType,
        at(1),
        reason=EventReason.OBSERVED,
        offeredByUserName="alice",
    )
    doc = {"schemaVersion": 1, "events": [ev.toJson()]}
    assert validateActivityDocument(doc) == []


@pytest.mark.parametrize("category", list(NoteCategory))
def test_status_note_event_passes_activity_document_validation(
    category: NoteCategory,
) -> None:
    note = StatusNote(
        noteId=NOTE_ID_1,
        userName="alice",
        message="Finishing tests",
        postedAt=at(0),
        expiresAt=at(60),
        category=category,
    )
    ev = newStatusNoteEvent(note, at(0))
    doc = {"schemaVersion": 1, "events": [ev.toJson()]}
    assert validateActivityDocument(doc) == []


def test_invalid_reservation_and_status_note_events_are_reported() -> None:
    res = Reservation(
        reservationId=RES_ID_1,
        userName="bob",
        requestedDurationMinutes=60,
        queuedAt=at(0),
    )
    evJson = newReservationEvent(res, ReservationEventType.RESERVATION_QUEUED, at(0)).toJson()
    evJson["requestedDurationMinutes"] = 1
    assert paths(validateActivityDocument({"schemaVersion": 1, "events": [evJson]})) == [
        "$.events[0].requestedDurationMinutes"
    ]

    note = StatusNote(
        noteId=NOTE_ID_1,
        userName="alice",
        message="Hi",
        postedAt=at(0),
        expiresAt=at(60),
    )
    noteJson = newStatusNoteEvent(note, at(0)).toJson()
    noteJson["message"] = "   "
    assert paths(validateActivityDocument({"schemaVersion": 1, "events": [noteJson]})) == [
        "$.events[0].message"
    ]
