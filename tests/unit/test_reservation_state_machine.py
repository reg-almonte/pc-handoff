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
    NoteCategory,
    Reservation,
    ReservationEventType,
    ReservationStatus,
    WaitEstimate,
    cancelReservation,
    claimHandoff,
    clearStatusNote,
    emptyQueueState,
    enqueueReservation,
    estimateQueueWait,
    estimateWaitMinutes,
    evaluateTick,
    initiateHandoff,
    postStatusNote,
    updateReservation,
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


# --- updateReservation & wait estimation --------------------------------------------------


def test_update_reservation_modifies_queued_slot_in_place_and_emits_audit_event() -> None:
    s0 = emptyQueueState(at(0))
    s1 = enqueueReservation(s0, "alice", 60, at(1), pcAvailable=True).state
    s2 = enqueueReservation(s1, "bob", 30, at(2), notes="Old note", pcAvailable=True).state
    s3 = enqueueReservation(s2, "carol", 45, at(3), pcAvailable=True).state
    aliceId = s3.reservations[0].reservationId
    bobId = s3.reservations[1].reservationId
    carolId = s3.reservations[2].reservationId

    out = updateReservation(s3, bobId, 90, at(4), notes="Updated note")
    assert [r.reservationId for r in out.state.queuedReservations] == [bobId, carolId]
    updatedBob = out.state.findById(bobId)
    assert updatedBob is not None
    assert updatedBob.requestedDurationMinutes == 90
    assert updatedBob.notes == "Updated note"
    assert [e.eventType for e in out.events] == [ReservationEventType.RESERVATION_UPDATED]
    assert out.events[0].reservationId == bobId
    assert out.events[0].requestedDurationMinutes == 90
    assert out.events[0].notes == "Updated note"

    # Non-existent or non-QUEUED (e.g. READY) reservation raises ValueError.
    with pytest.raises(ValueError, match="queued reservation not found"):
        updateReservation(out.state, "missing-id", 45, at(5))
    with pytest.raises(ValueError, match="queued reservation not found"):
        updateReservation(out.state, aliceId, 45, at(5))


def test_estimate_wait_minutes_and_queue_wait_across_states() -> None:
    s0 = emptyQueueState(at(0))
    assert estimateQueueWait(s0, at(0)) == WaitEstimate(waitMinutes=0, estimatedStartAt=at(0))

    # Active reservation (60m, activated at t=0), Ready reservation (30m), two Queued (45m, 20m).
    rActive = Reservation(
        reservationId=str(uuid.uuid4()),
        userName="alice",
        requestedDurationMinutes=60,
        queuedAt=at(0),
        status=ReservationStatus.ACTIVE,
        activatedAt=at(0),
    )
    rReady = Reservation(
        reservationId=str(uuid.uuid4()),
        userName="bob",
        requestedDurationMinutes=30,
        queuedAt=at(1),
        status=ReservationStatus.READY,
        readyAt=at(2),
        claimExpiresAt=at(7),
    )
    rQueued1 = Reservation(
        reservationId=str(uuid.uuid4()),
        userName="carol",
        requestedDurationMinutes=45,
        queuedAt=at(3),
        status=ReservationStatus.QUEUED,
    )
    rQueued2 = Reservation(
        reservationId=str(uuid.uuid4()),
        userName="dave",
        requestedDurationMinutes=20,
        queuedAt=at(4),
        status=ReservationStatus.QUEUED,
    )
    state = replace(s0, reservations=(rActive, rReady, rQueued1, rQueued2))

    # At t=15m: active has 45m remaining.
    # - Active/Ready slots have 0 wait.
    assert estimateWaitMinutes(state, at(15), reservationId=rActive.reservationId) == 0
    assert estimateWaitMinutes(state, at(15), reservationId=rReady.reservationId) == 0
    # - First queued (carol) waits for active (45m) + ready (30m) = 75m.
    assert estimateWaitMinutes(state, at(15), reservationId=rQueued1.reservationId) == 75
    # - Second queued (dave) waits 75m + carol (45m) = 120m.
    assert estimateWaitMinutes(state, at(15), reservationId=rQueued2.reservationId) == 120
    # - New user joining at tail waits 120m + dave (20m) = 140m.
    est = estimateQueueWait(state, at(15))
    assert est == WaitEstimate(waitMinutes=140, estimatedStartAt=at(155))

    # Past active duration (t=70m): active remaining clamps to 0.
    assert estimateWaitMinutes(state, at(70), reservationId=rQueued1.reservationId) == 30

    # Active with activatedAt=None uses full requestedDurationMinutes.
    stateNoActivatedAt = replace(state, reservations=(replace(rActive, activatedAt=None),))
    assert estimateWaitMinutes(stateNoActivatedAt, at(15)) == 60

    # Unknown or terminal reservationId raises ValueError.
    with pytest.raises(ValueError, match="not found"):
        estimateWaitMinutes(state, at(15), reservationId="unknown")


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
        out = postStatusNote(
            state,
            f"user{i}",
            f"msg {i}",
            at(i),
            ttlMinutes=60,
            category=NoteCategory.URGENT,
        )
        state = out.state
        assert [e.eventType for e in out.events] == [ReservationEventType.STATUS_NOTE_POSTED]
        assert out.events[0].category is NoteCategory.URGENT
        assert state.statusNotes[0].category is NoteCategory.URGENT

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

    # At t = 6 min (past 5-min grace window from t = 1 min), Alice re-queues to tail
    # and Bob becomes READY.
    out1 = evaluateTick(s2, [], pcAvailable=True, now=at(6))
    assert out1.state.findById(aliceId).status is ReservationStatus.REQUEUED
    assert out1.state.findById(aliceId).claimExpiresAt is None
    assert out1.state.findById(bobId).status is ReservationStatus.READY
    assert out1.state.handoff.status is HandoffStatus.OFFERED
    assert out1.state.handoff.targetUserName == "bob"
    assert [e.eventType for e in out1.events] == [
        ReservationEventType.RESERVATION_REQUEUED,
        ReservationEventType.HANDOFF_INITIATED,
    ]
    assert [r.userName for r in out1.state.nonTerminalReservations] == ["bob", "alice"]

    # At t = 12 min, Bob also expires -> Bob re-queues to tail, Alice becomes READY again!
    out2 = evaluateTick(out1.state, [], pcAvailable=True, now=at(12))
    assert out2.state.findById(bobId).status is ReservationStatus.REQUEUED
    assert out2.state.findById(bobId).claimExpiresAt is None
    assert out2.state.findById(aliceId).status is ReservationStatus.READY
    assert out2.state.handoff.status is HandoffStatus.OFFERED
    assert out2.state.handoff.targetUserName == "alice"
    assert [e.eventType for e in out2.events] == [
        ReservationEventType.RESERVATION_REQUEUED,
        ReservationEventType.HANDOFF_INITIATED,
    ]


def test_evaluate_tick_single_user_requeue_alone() -> None:
    # Single reservation in READY state
    s1 = enqueueReservation(
        emptyQueueState(at(0)),
        "alice",
        60,
        at(0),
        pcAvailable=True,
        claimGraceSeconds=300,
    ).state
    aliceId = s1.reservations[0].reservationId
    assert s1.reservations[0].status is ReservationStatus.READY

    # At t = 6 min (past 5 min), single user expires -> updates to REQUEUED, stays alone in queue
    out = evaluateTick(s1, [], pcAvailable=True, now=at(6), claimGraceSeconds=300)
    alice = out.state.findById(aliceId)
    assert alice.status is ReservationStatus.REQUEUED
    assert alice.claimExpiresAt is None
    assert out.state.handoff.status is HandoffStatus.TIMED_OUT
    assert out.state.queuedReservations[0].reservationId == aliceId
    assert [e.eventType for e in out.events] == [ReservationEventType.RESERVATION_REQUEUED]


def test_evaluate_tick_ip_autoclaim_for_ready_and_queued() -> None:
    # Alice enqueues with client IP 192.168.1.100 and is READY
    s1 = enqueueReservation(
        emptyQueueState(at(0)),
        "alice",
        60,
        at(0),
        clientAddress="192.168.1.100",
        pcAvailable=True,
    ).state
    aliceId = s1.reservations[0].reservationId
    assert s1.findById(aliceId).status is ReservationStatus.READY

    # Active session logs in with generic OS user 'operator' but matching client IP
    sess = replace(makeSession("operator"), clientAddress="192.168.1.100")
    out = evaluateTick(s1, [sess], pcAvailable=False, now=at(1))
    assert out.state.findById(aliceId).status is ReservationStatus.ACTIVE
    assert out.state.activeReservation.reservationId == aliceId
    assert out.state.handoff.status is HandoffStatus.CLAIMED
    assert [e.eventType for e in out.events] == [ReservationEventType.HANDOFF_CLAIMED]


def test_evaluate_tick_ip_autoclaim_head_queued() -> None:
    # Bob is queued with IP 192.168.1.101 while PC is busy
    s1 = enqueueReservation(
        emptyQueueState(at(0)),
        "bob",
        60,
        at(0),
        clientAddress="192.168.1.101",
        pcAvailable=False,
    ).state
    bobId = s1.reservations[0].reservationId
    assert s1.findById(bobId).status is ReservationStatus.QUEUED

    # Bob connects directly via RDP with generic user 'labuser'
    sess = replace(makeSession("labuser"), clientAddress="192.168.1.101")
    out = evaluateTick(s1, [sess], pcAvailable=False, now=at(1))
    assert out.state.findById(bobId).status is ReservationStatus.ACTIVE
    assert out.state.activeReservation.reservationId == bobId
    assert [e.eventType for e in out.events] == [ReservationEventType.HANDOFF_CLAIMED]


def test_evaluate_tick_prunes_expired_status_notes() -> None:
    s1 = postStatusNote(emptyQueueState(at(0)), "alice", "Short note", at(0), ttlMinutes=10).state
    assert len(s1.statusNotes) == 1

    out = evaluateTick(s1, [], pcAvailable=True, now=at(11))
    assert out.state.statusNotes == ()
    assert out.state.updatedAt == at(11)
