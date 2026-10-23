"""Unit tests for pure reservation and handoff state machine transitions (TASK-012)."""

import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from pc_handoff.detection.provider import ConnectionType
from pc_handoff.domain.models import SessionState, TrackedSession, UsagePeriod
from pc_handoff.domain.reservations import (
    CLAIM_GRACE_SECONDS,
    MAX_RETAINED_TERMINAL,
    MAX_STATUS_NOTES,
    HandoffStatus,
    Reservation,
    ReservationEventType,
    ReservationStatus,
    cancelReservation,
    claimHandoff,
    clearStatusNote,
    emptyQueueState,
    enqueueReservation,
    evaluateTick,
    initiateHandoff,
    postStatusNote,
)

TZ = timezone(timedelta(hours=9))
T0 = datetime(2026, 10, 23, 19, 0, 0, tzinfo=TZ)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def makeSession(
    userName: str,
    state: SessionState = SessionState.ACTIVE,
    sessionId: int = 2,
) -> TrackedSession:
    period = (
        UsagePeriod(
            usagePeriodId=str(uuid.uuid4()),
            usagePeriodStart=T0,
            durationApproximate=False,
            startEventTimestamp=T0,
            connectTimeAtStart=T0,
            connectionTypeAtStart=ConnectionType.RDP,
        )
        if state is SessionState.ACTIVE
        else None
    )
    return TrackedSession(
        sessionId=sessionId,
        userName=userName,
        domain="CORP",
        state=state,
        connectionType=ConnectionType.RDP,
        clientName="CLIENT",
        clientAddress="192.168.1.10",
        logonTime=T0,
        firstDetectedAt=T0,
        connectTime=T0,
        osDisconnectTime=None,
        disconnectedSince=None if state is SessionState.ACTIVE else T0,
        openPeriod=period,
    )


# --- enqueueReservation -------------------------------------------------------------------


def test_enqueue_when_pc_in_use_stays_queued() -> None:
    state = emptyQueueState(at(0))
    outcome = enqueueReservation(
        state,
        "alice",
        60,
        at(1),
        notes="Regression suite",
        pcAvailable=False,
    )
    assert len(outcome.state.reservations) == 1
    res = outcome.state.reservations[0]
    assert res.userName == "alice"
    assert res.status is ReservationStatus.QUEUED
    assert res.notes == "Regression suite"
    assert outcome.state.handoff.status is HandoffStatus.IDLE
    assert [e.eventType for e in outcome.events] == [ReservationEventType.RESERVATION_QUEUED]


def test_enqueue_when_pc_available_promotes_head_to_ready() -> None:
    state = emptyQueueState(at(0))
    first = enqueueReservation(state, "alice", 45, at(1), pcAvailable=True)
    res1 = first.state.reservations[0]
    assert res1.status is ReservationStatus.READY
    assert res1.readyAt == at(1)
    assert res1.claimExpiresAt == at(1) + timedelta(seconds=CLAIM_GRACE_SECONDS)
    assert first.state.handoff.status is HandoffStatus.OFFERED
    assert first.state.handoff.targetUserName == "alice"
    assert [e.eventType for e in first.events] == [
        ReservationEventType.RESERVATION_QUEUED,
        ReservationEventType.HANDOFF_INITIATED,
    ]

    # Second user joins while first is READY: stays QUEUED.
    second = enqueueReservation(first.state, "bob", 30, at(2), pcAvailable=True)
    assert [r.status for r in second.state.reservations] == [
        ReservationStatus.READY,
        ReservationStatus.QUEUED,
    ]
    assert [e.eventType for e in second.events] == [ReservationEventType.RESERVATION_QUEUED]


def test_enqueue_rejects_duplicate_non_terminal_user_case_insensitively() -> None:
    state = enqueueReservation(emptyQueueState(at(0)), "Alice", 60, at(1)).state
    with pytest.raises(ValueError, match="already has an active or queued reservation"):
        enqueueReservation(state, "  ALICE ", 30, at(2))


def test_terminal_reservations_are_bounded_to_max_retained() -> None:
    terminal = tuple(
        Reservation(
            reservationId=str(uuid.uuid4()),
            userName=f"user-{i}",
            requestedDurationMinutes=30,
            queuedAt=at(i),
            status=ReservationStatus.COMPLETED,
            endedAt=at(i + 1),
        )
        for i in range(MAX_RETAINED_TERMINAL + 5)
    )
    state = replace(emptyQueueState(at(0)), reservations=terminal)
    outcome = enqueueReservation(state, "new-user", 30, at(100))
    assert len(outcome.state.reservations) == MAX_RETAINED_TERMINAL + 1
    assert outcome.state.reservations[0].userName == "new-user"


# --- cancelReservation --------------------------------------------------------------------


def test_cancel_ready_reservation_promotes_next_queued_and_resets_when_empty() -> None:
    s0 = emptyQueueState(at(0))
    s1 = enqueueReservation(s0, "alice", 60, at(1), pcAvailable=True).state
    s2 = enqueueReservation(s1, "bob", 30, at(2), pcAvailable=True).state
    aliceId = s2.reservations[0].reservationId
    bobId = s2.reservations[1].reservationId

    # Cancel alice (READY) -> bob becomes READY.
    out1 = cancelReservation(s2, aliceId, at(3), pcAvailable=False)
    assert out1.state.findById(aliceId).status is ReservationStatus.CANCELLED
    assert out1.state.findById(bobId).status is ReservationStatus.READY
    assert out1.state.handoff.reservationId == bobId
    assert [e.eventType for e in out1.events] == [
        ReservationEventType.RESERVATION_CANCELLED,
        ReservationEventType.HANDOFF_INITIATED,
    ]

    # Cancel bob (READY, no one else waiting) -> handoff resets to IDLE.
    out2 = cancelReservation(out1.state, bobId, at(4), pcAvailable=False)
    assert out2.state.findById(bobId).status is ReservationStatus.CANCELLED
    assert out2.state.handoff.status is HandoffStatus.IDLE
    assert [e.eventType for e in out2.events] == [ReservationEventType.RESERVATION_CANCELLED]


def test_cancel_queued_reservation_leaves_ready_untouched() -> None:
    s1 = enqueueReservation(emptyQueueState(at(0)), "alice", 60, at(1), pcAvailable=True).state
    s2 = enqueueReservation(s1, "bob", 30, at(2), pcAvailable=True).state
    bobId = s2.reservations[1].reservationId

    out = cancelReservation(s2, bobId, at(3))
    assert out.state.readyReservation.userName == "alice"
    assert out.state.handoff.targetUserName == "alice"
    assert [e.eventType for e in out.events] == [ReservationEventType.RESERVATION_CANCELLED]


def test_cancel_unknown_or_terminal_reservation_raises() -> None:
    s1 = enqueueReservation(emptyQueueState(at(0)), "alice", 60, at(1)).state
    aliceId = s1.reservations[0].reservationId
    s2 = cancelReservation(s1, aliceId, at(2)).state

    with pytest.raises(ValueError, match="not found"):
        cancelReservation(s2, "missing-id", at(3))
    with pytest.raises(ValueError, match="not found"):
        cancelReservation(s2, aliceId, at(3))


# --- initiateHandoff & claimHandoff -------------------------------------------------------


def test_initiate_handoff_completes_active_and_promotes_queued() -> None:
    s0 = emptyQueueState(at(0))
    s1 = enqueueReservation(s0, "alice", 60, at(1), pcAvailable=True).state
    aliceId = s1.reservations[0].reservationId
    s2 = claimHandoff(s1, aliceId, at(2)).state
    s3 = enqueueReservation(s2, "bob", 45, at(3), pcAvailable=False).state

    out = initiateHandoff(s3, "alice", at(10))
    assert out.state.findById(aliceId).status is ReservationStatus.COMPLETED
    assert out.state.readyReservation.userName == "bob"
    assert out.state.handoff.status is HandoffStatus.OFFERED
    assert out.state.handoff.offeredByUserName == "alice"
    assert out.state.handoff.targetUserName == "bob"
    assert [e.eventType for e in out.events] == [ReservationEventType.HANDOFF_INITIATED]


def test_initiate_handoff_refreshes_existing_ready_and_handles_empty_queue() -> None:
    # Existing READY reservation gets refreshed with offeredByUserName.
    s1 = enqueueReservation(emptyQueueState(at(0)), "bob", 45, at(1), pcAvailable=True).state
    out1 = initiateHandoff(s1, "alice", at(3))
    assert out1.state.readyReservation.readyAt == at(3)
    assert out1.state.handoff.offeredByUserName == "alice"
    assert [e.eventType for e in out1.events] == [ReservationEventType.HANDOFF_INITIATED]

    # Empty queue sets handoff to IDLE.
    outEmpty = initiateHandoff(emptyQueueState(at(0)), None, at(1))
    assert outEmpty.state.handoff.status is HandoffStatus.IDLE
    assert outEmpty.events == ()


def test_claim_handoff_from_ready_and_head_queued_and_error_cases() -> None:
    # Claim directly from head QUEUED when no READY/ACTIVE exists.
    s1 = enqueueReservation(emptyQueueState(at(0)), "alice", 60, at(1), pcAvailable=False).state
    s2 = enqueueReservation(s1, "bob", 60, at(2), pcAvailable=False).state
    aliceId = s2.reservations[0].reservationId
    bobId = s2.reservations[1].reservationId

    # Bob is not at head of queue -> rejected.
    with pytest.raises(ValueError, match="not at the head"):
        claimHandoff(s2, bobId, at(3))

    # Alice is at head -> succeeds.
    claimedOut = claimHandoff(s2, aliceId, at(3))
    assert claimedOut.state.activeReservation.userName == "alice"
    assert claimedOut.state.handoff.status is HandoffStatus.CLAIMED
    assert [e.eventType for e in claimedOut.events] == [ReservationEventType.HANDOFF_CLAIMED]

    # While Alice is ACTIVE, Bob cannot claim directly from QUEUED.
    with pytest.raises(ValueError, match="not at the head"):
        claimHandoff(claimedOut.state, bobId, at(4))

    # Unknown ID is rejected.
    with pytest.raises(ValueError, match="cannot be claimed"):
        claimHandoff(claimedOut.state, "unknown-id", at(4))


# --- postStatusNote & clearStatusNote -----------------------------------------------------


def test_post_and_clear_status_notes_and_max_bound() -> None:
    state = emptyQueueState(at(0))
    for i in range(MAX_STATUS_NOTES + 3):
        out = postStatusNote(state, f"user{i}", f"msg {i}", at(i), ttlMinutes=60)
        state = out.state
        assert [e.eventType for e in out.events] == [ReservationEventType.STATUS_NOTE_POSTED]

    assert len(state.statusNotes) == MAX_STATUS_NOTES
    newestId = state.statusNotes[0].noteId

    cleared = clearStatusNote(state, newestId, at(20))
    assert len(cleared.state.statusNotes) == MAX_STATUS_NOTES - 1

    with pytest.raises(ValueError, match="status note not found"):
        clearStatusNote(cleared.state, newestId, at(21))


# --- evaluateTick -------------------------------------------------------------------------


def test_evaluate_tick_noop_when_nothing_changes() -> None:
    state = emptyQueueState(at(0))
    out = evaluateTick(state, [], pcAvailable=True, now=at(1))
    assert out.state is state
    assert out.events == ()


def test_evaluate_tick_correlates_ready_reservation_with_active_session() -> None:
    s1 = enqueueReservation(emptyQueueState(at(0)), "Alice", 60, at(1), pcAvailable=True).state
    # Disconnected session does not claim; Active session for another user does not claim.
    outNoMatch = evaluateTick(
        s1,
        [makeSession("alice", state=SessionState.DISCONNECTED), makeSession("other")],
        pcAvailable=False,
        now=at(2),
    )
    assert outNoMatch.state.readyReservation is not None

    # Active session for "alice" (case-insensitive) claims the READY reservation.
    outClaimed = evaluateTick(
        s1,
        [makeSession("  ALICE ")],
        pcAvailable=False,
        now=at(2),
    )
    assert outClaimed.state.activeReservation.userName == "Alice"
    assert outClaimed.state.handoff.status is HandoffStatus.CLAIMED
    assert [e.eventType for e in outClaimed.events] == [ReservationEventType.HANDOFF_CLAIMED]

    # When Alice disconnects and PC becomes Available, ACTIVE reservation completes.
    outCompleted = evaluateTick(
        outClaimed.state,
        [],
        pcAvailable=True,
        now=at(30),
    )
    assert outCompleted.state.activeReservation is None
    assert outCompleted.state.reservations[0].status is ReservationStatus.COMPLETED
    assert outCompleted.state.handoff.status is HandoffStatus.IDLE

    # Also test completing an ACTIVE reservation when handoff is already IDLE.
    idleHandoffActive = replace(outClaimed.state, handoff=outCompleted.state.handoff)
    outCompletedIdle = evaluateTick(idleHandoffActive, [], pcAvailable=True, now=at(31))
    assert outCompletedIdle.state.activeReservation is None


def test_evaluate_tick_direct_correlation_of_head_queued_reservation() -> None:
    s1 = enqueueReservation(emptyQueueState(at(0)), "alice", 60, at(1), pcAvailable=False).state
    s2 = enqueueReservation(s1, "bob", 60, at(2), pcAvailable=False).state

    # Bob connecting while Alice is ahead in QUEUED does not skip Alice.
    outBob = evaluateTick(s2, [makeSession("bob")], pcAvailable=False, now=at(3))
    assert outBob.state.activeReservation is None

    # Alice connecting while at head of QUEUED claims directly into ACTIVE.
    outAlice = evaluateTick(s2, [makeSession("alice")], pcAvailable=False, now=at(3))
    assert outAlice.state.activeReservation.userName == "alice"
    assert [e.eventType for e in outAlice.events] == [ReservationEventType.HANDOFF_CLAIMED]


def test_evaluate_tick_expires_ready_and_advances_next_queued() -> None:
    s1 = enqueueReservation(emptyQueueState(at(0)), "alice", 60, at(1), pcAvailable=True).state
    s2 = enqueueReservation(s1, "bob", 45, at(2), pcAvailable=True).state
    aliceId = s2.reservations[0].reservationId
    bobId = s2.reservations[1].reservationId

    # At t = 6 min (past 5-min grace window from t = 1 min), Alice expires and Bob becomes READY.
    out1 = evaluateTick(s2, [], pcAvailable=True, now=at(6))
    assert out1.state.findById(aliceId).status is ReservationStatus.EXPIRED
    assert out1.state.findById(bobId).status is ReservationStatus.READY
    assert out1.state.handoff.status is HandoffStatus.OFFERED
    assert out1.state.handoff.targetUserName == "bob"
    assert [e.eventType for e in out1.events] == [
        ReservationEventType.RESERVATION_EXPIRED,
        ReservationEventType.HANDOFF_INITIATED,
    ]

    # At t = 12 min, Bob also expires and no one else is queued -> handoff stays TIMED_OUT.
    out2 = evaluateTick(out1.state, [], pcAvailable=True, now=at(12))
    assert out2.state.findById(bobId).status is ReservationStatus.EXPIRED
    assert out2.state.handoff.status is HandoffStatus.TIMED_OUT
    assert [e.eventType for e in out2.events] == [ReservationEventType.RESERVATION_EXPIRED]


def test_evaluate_tick_prunes_expired_status_notes() -> None:
    s1 = postStatusNote(emptyQueueState(at(0)), "alice", "Short note", at(0), ttlMinutes=10).state
    assert len(s1.statusNotes) == 1

    out = evaluateTick(s1, [], pcAvailable=True, now=at(11))
    assert out.state.statusNotes == ()
    assert out.state.updatedAt == at(11)
