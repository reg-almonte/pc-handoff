"""Integration tests for ``reservations.json`` startup loading and reconciliation (TASK-013)."""

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pc_handoff.domain.models import EventReason
from pc_handoff.domain.reservations import (
    HandoffStatus,
    ReservationEventType,
    ReservationStatus,
    claimHandoff,
    emptyQueueState,
    enqueueReservation,
    postStatusNote,
)
from pc_handoff.domain.tracker import SessionTracker
from pc_handoff.persistence.paths import DataPaths
from pc_handoff.persistence.reservation_store import ReservationStore
from pc_handoff.persistence.startup_loader import (
    loadStartup,
    reconcileReservationsAtStartup,
    reconcileStartup,
)
from tests.fakes import FakeClock
from tests.fakes.builders import osSession

JST = timezone(timedelta(hours=9))
T0 = datetime(2026, 10, 26, 10, 0, 0, tzinfo=JST)


def test_load_startup_with_valid_reservations_and_reconcile_connected_user(
    tmp_path: Path,
) -> None:
    dataDir = tmp_path / "data"
    dataDir.mkdir()
    paths = DataPaths(dataDir)
    logger = logging.getLogger("pc_handoff.test.startup_res")

    # Seed reservations.json before restart: alice is READY, bob is QUEUED, plus an expiring note.
    preRestartTime = T0 - timedelta(minutes=2)
    clock = FakeClock(preRestartTime)
    s0 = emptyQueueState(preRestartTime)
    s1 = enqueueReservation(s0, "alice", 60, preRestartTime, pcAvailable=True).state
    s2 = enqueueReservation(s1, "bob", 30, preRestartTime, pcAvailable=True).state
    s3 = postStatusNote(s2, "alice", "Connecting now", preRestartTime, ttlMinutes=5).state
    ReservationStore(paths, clock, logger).write(s3)

    # Advance clock to T0 (restart time) and run loadStartup.
    clock = FakeClock(T0)
    ctx = loadStartup(paths, clock, logger)
    assert ctx.reservationsUsable is True
    assert ctx.reservationStore is not None
    assert ctx.queueState is not None
    assert ctx.queueState.readyReservation is not None
    assert ctx.queueState.readyReservation.userName == "alice"

    # At first poll after startup, alice has an Active RDP session.
    tracker = SessionTracker(clock, logger)
    pollOutcome = reconcileStartup(
        ctx,
        tracker,
        [osSession(2, "alice", connectTime=T0 - timedelta(minutes=1))],
        T0,
    )
    resOutcome = reconcileReservationsAtStartup(ctx, pollOutcome.state, T0)

    assert resOutcome.state.activeReservation is not None
    assert resOutcome.state.activeReservation.userName == "alice"
    assert resOutcome.state.handoff.status is HandoffStatus.CLAIMED
    assert [e.eventType for e in resOutcome.events] == [ReservationEventType.HANDOFF_CLAIMED]
    assert resOutcome.events[0].reason is EventReason.DETECTED_AT_STARTUP

    # Persist reconciled queue state and events.
    ctx.reservationStore.write(resOutcome.state)
    appendRes = ctx.activityStore.appendBatch([*pollOutcome.events, *resOutcome.events])
    assert appendRes.written is True


def test_load_startup_reconciles_expired_ready_and_completed_active_after_downtime(
    tmp_path: Path,
) -> None:
    dataDir = tmp_path / "data"
    dataDir.mkdir()
    paths = DataPaths(dataDir)
    logger = logging.getLogger("pc_handoff.test.startup_res")

    # Case A: alice was READY before a 20-minute downtime; bob is QUEUED.
    preTime = T0 - timedelta(minutes=20)
    clock = FakeClock(preTime)
    s1 = enqueueReservation(emptyQueueState(preTime), "alice", 60, preTime, pcAvailable=True).state
    s2 = enqueueReservation(s1, "bob", 45, preTime, pcAvailable=True).state
    ReservationStore(paths, clock, logger).write(s2)

    clock = FakeClock(T0)
    ctx = loadStartup(paths, clock, logger)
    tracker = SessionTracker(clock, logger)
    pollOutcome = reconcileStartup(ctx, tracker, [], T0)
    resOutcome = reconcileReservationsAtStartup(ctx, pollOutcome.state, T0)

    # Alice's READY slot expired during downtime; Alice is REQUEUED and Bob is promoted to READY.
    aliceId = s2.reservations[0].reservationId
    bobId = s2.reservations[1].reservationId
    assert resOutcome.state.findById(aliceId).status is ReservationStatus.REQUEUED
    assert resOutcome.state.findById(bobId).status is ReservationStatus.READY
    assert [e.eventType for e in resOutcome.events] == [
        ReservationEventType.RESERVATION_REQUEUED,
        ReservationEventType.HANDOFF_INITIATED,
    ]
    assert all(e.reason is EventReason.DETECTED_AT_STARTUP for e in resOutcome.events)

    # Case B: Bob claims and is ACTIVE before another restart, then disconnects while PC is down.
    claimedState = claimHandoff(resOutcome.state, bobId, T0).state
    ReservationStore(paths, clock, logger).write(claimedState)

    laterTime = T0 + timedelta(minutes=30)
    clock2 = FakeClock(laterTime)
    ctx2 = loadStartup(paths, clock2, logger)
    pollOutcome2 = reconcileStartup(ctx2, SessionTracker(clock2, logger), [], laterTime)
    resOutcome2 = reconcileReservationsAtStartup(ctx2, pollOutcome2.state, laterTime)

    assert resOutcome2.state.findById(bobId).status is ReservationStatus.COMPLETED


def test_load_startup_with_corrupt_reservations_json_emits_startup_notice(
    tmp_path: Path,
) -> None:
    dataDir = tmp_path / "data"
    dataDir.mkdir()
    paths = DataPaths(dataDir)
    paths.reservations.write_bytes(b"corrupt-json{{{")

    clock = FakeClock(T0)
    logger = logging.getLogger("pc_handoff.test.startup_res")
    ctx = loadStartup(paths, clock, logger)

    assert ctx.reservationsUsable is False
    assert ctx.queueState == emptyQueueState(T0)
    assert any(
        n.fileName == "reservations.json" and "Reservation data was reset" in n.text
        for n in ctx.notices
    )
