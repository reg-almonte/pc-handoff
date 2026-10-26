"""Unit tests for ``ReservationManager`` (TASK-014, SD001 section 4)."""

import json
import logging
import threading
from datetime import timedelta
from pathlib import Path

import pytest

from pc_handoff.domain.models import EventReason, PcStatus
from pc_handoff.domain.reservations import (
    HandoffStatus,
    QueueState,
    ReservationEventType,
    ReservationStatus,
    emptyQueueState,
    enqueueReservation,
)
from pc_handoff.domain.tracker import TrackerState
from pc_handoff.persistence import reservation_store
from pc_handoff.persistence.activity_log_store import ActivityLogStore
from pc_handoff.persistence.atomic import PersistenceError
from pc_handoff.persistence.notices import StartupNotice
from pc_handoff.persistence.paths import DataPaths
from pc_handoff.persistence.reservation_store import ReservationStore
from pc_handoff.runtime.reservation_manager import ReservationManager
from tests.conftest import FAKE_CLOCK_START
from tests.fakes import FakeClock
from tests.fakes.builders import trackedActive, trackerState

T0 = FAKE_CLOCK_START


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(T0)


@pytest.fixture
def paths(tmp_path: Path) -> DataPaths:
    dataDir = tmp_path / "data"
    dataDir.mkdir()
    return DataPaths(dataDir)


def makeManager(
    paths: DataPaths,
    clock: FakeClock,
    *,
    initialState: QueueState | None = None,
    isPcAvailable=None,
    onStateChanged=None,
    claimGraceSeconds: int = 300,
) -> ReservationManager:
    logger = logging.getLogger("test.reservation_manager")
    resStore = ReservationStore(paths, clock, logger)
    actStore = ActivityLogStore(paths, clock, logger)
    actStore.loadAtStartup()
    return ReservationManager(
        reservationStore=resStore,
        activityStore=actStore,
        clock=clock,
        logger=logger,
        initialState=initialState,
        isPcAvailable=isPcAvailable,
        onStateChanged=onStateChanged,
        claimGraceSeconds=claimGraceSeconds,
    )


class TestReservationManagerMutations:
    def test_initial_state_defaults_to_empty_or_uses_provided_state(
        self, paths: DataPaths, clock: FakeClock
    ) -> None:
        mgrDefault = makeManager(paths, clock)
        assert mgrDefault.currentState() == emptyQueueState(T0)
        assert isinstance(mgrDefault.lock, type(threading.RLock()))

        seed = enqueueReservation(emptyQueueState(T0), "alice", 45, T0).state
        mgrSeeded = makeManager(paths, clock, initialState=seed)
        assert mgrSeeded.currentState() == seed

    def test_enqueue_claim_initiate_handoff_and_cancel_lifecycle(
        self, paths: DataPaths, clock: FakeClock
    ) -> None:
        changes: list[tuple[QueueState, StartupNotice | None]] = []
        mgr = makeManager(
            paths,
            clock,
            isPcAvailable=lambda: True,
            onStateChanged=lambda state, notice: changes.append((state, notice)),
        )

        # Enqueue alice while PC is available -> immediately READY
        rAlice = mgr.enqueueReservation("alice", 60, notes="GPU training")
        assert rAlice.status is ReservationStatus.READY
        assert mgr.currentState().handoff.status is HandoffStatus.OFFERED
        assert len(changes) == 1
        assert paths.reservations.is_file()

        # Enqueue bob while alice is READY -> QUEUED
        clock.advance(5)
        rBob = mgr.enqueueReservation("bob", 30, pcAvailable=False)
        assert rBob.status is ReservationStatus.QUEUED

        # Alice claims handoff -> ACTIVE
        clock.advance(5)
        claimedAlice = mgr.claimHandoff(rAlice.reservationId)
        assert claimedAlice.status is ReservationStatus.ACTIVE

        # Alice initiates handoff -> alice COMPLETED, bob promoted to READY
        clock.advance(10)
        stateAfterHandoff = mgr.initiateHandoff("alice")
        assert stateAfterHandoff.findById(rAlice.reservationId).status is (
            ReservationStatus.COMPLETED
        )
        assert stateAfterHandoff.findById(rBob.reservationId).status is ReservationStatus.READY
        assert stateAfterHandoff.handoff.offeredByUserName == "alice"

        # Bob cancels reservation -> CANCELLED
        clock.advance(5)
        cancelledBob = mgr.cancelReservation(rBob.reservationId)
        assert cancelledBob.status is ReservationStatus.CANCELLED
        assert mgr.currentState().handoff.status is HandoffStatus.IDLE

        # Verify persisted activity_log.json contains all reservation audit events
        rawLog = json.loads(paths.activityLog.read_text(encoding="utf-8"))
        eventTypes = [e["eventType"] for e in rawLog["events"]]
        assert eventTypes == [
            ReservationEventType.RESERVATION_QUEUED.value,
            ReservationEventType.HANDOFF_INITIATED.value,
            ReservationEventType.RESERVATION_QUEUED.value,
            ReservationEventType.HANDOFF_CLAIMED.value,
            ReservationEventType.HANDOFF_INITIATED.value,
            ReservationEventType.RESERVATION_CANCELLED.value,
        ]

    def test_status_note_post_and_clear(self, paths: DataPaths, clock: FakeClock) -> None:
        changes: list[tuple[QueueState, StartupNotice | None]] = []
        mgr = makeManager(
            paths,
            clock,
            onStateChanged=lambda state, notice: changes.append((state, notice)),
        )
        # Without isPcAvailable callback, _resolvePcAvailable falls back to False
        res = mgr.enqueueReservation("alice", 30)
        assert res.status is ReservationStatus.QUEUED

        note = mgr.postStatusNote("alice", "Stepping away for 15m", ttlMinutes=30)
        assert mgr.currentState().statusNotes == (note,)

        clock.advance(5)
        clearedState = mgr.clearStatusNote(note.noteId)
        assert clearedState.statusNotes == ()
        assert len(changes) == 3

    def test_write_persistence_error_logs_and_keeps_in_memory_state_authoritative(
        self, paths: DataPaths, clock: FakeClock, monkeypatch: pytest.MonkeyPatch, caplog
    ) -> None:
        mgr = makeManager(paths, clock)

        def failWrite(path, data):
            raise PersistenceError("disk full")

        monkeypatch.setattr(reservation_store, "atomicWriteBytes", failWrite)
        with caplog.at_level(logging.ERROR, logger="test.reservation_manager"):
            created = mgr.enqueueReservation("alice", 60, pcAvailable=True)

        assert created.status is ReservationStatus.READY
        assert mgr.currentState().readyReservation == created
        assert not paths.reservations.exists()
        assert any(
            "reservations.json could not be written; the in-memory state stays authoritative"
            in record.message
            for record in caplog.records
        )


class TestReservationManagerEvaluateTick:
    def test_evaluate_tick_correlates_session_and_expires_grace_window(
        self, paths: DataPaths, clock: FakeClock
    ) -> None:
        changes: list[tuple[QueueState, StartupNotice | None]] = []
        mgr = makeManager(
            paths,
            clock,
            claimGraceSeconds=60,
            onStateChanged=lambda state, notice: changes.append((state, notice)),
        )

        # No-op tick when queue is empty
        emptyTracker = trackerState([], clock.now())
        noOp = mgr.evaluateTick(emptyTracker, clock.now())
        assert noOp.events == ()
        assert not paths.reservations.exists()
        assert changes == []
        # writeCommittedTick when not dirty is a no-op
        mgr.writeCommittedTick()
        assert not paths.reservations.exists()

        # Enqueue alice (QUEUED) and bob (QUEUED)
        rAlice = mgr.enqueueReservation("alice", 60, pcAvailable=False)
        rBob = mgr.enqueueReservation("bob", 60, pcAvailable=False)
        changes.clear()

        # Tick when PC becomes Available -> alice promoted to READY
        clock.advance(5)
        availTracker = trackerState([], clock.now())
        tick1 = mgr.evaluateTick(availTracker, clock.now(), reconciling=True)
        assert len(tick1.events) == 1
        assert tick1.events[0].eventType is ReservationEventType.HANDOFF_INITIATED
        assert tick1.events[0].reason is EventReason.DETECTED_AT_STARTUP
        assert mgr.currentState().findById(rAlice.reservationId).status is ReservationStatus.READY
        assert len(changes) == 1

        # Tick during an enumeration outage (detectionHealthy=False) with stale active session:
        # must NOT correlate alice with the stale session, nor falsely complete anything.
        clock.advance(10)
        unhealthyTracker = TrackerState(
            sessions={1: trackedActive(1, "alice", connectTime=T0, startEventTimestamp=T0)},
            lastSuccessfulPollAt=T0,
            lastKnownStatus=PcStatus.IN_USE,
            lastKnownStatusAt=T0,
            status=PcStatus.UNKNOWN,
            detectionHealthy=False,
            outagePending=True,
        )
        tickUnhealthy = mgr.evaluateTick(unhealthyTracker, clock.now())
        assert tickUnhealthy.events == ()
        assert mgr.currentState().findById(rAlice.reservationId).status is ReservationStatus.READY

        # Advance past alice's 60s grace window -> alice EXPIRED, bob promoted to READY
        clock.advance(60)
        tickExpire = mgr.evaluateTick(trackerState([], clock.now()), clock.now())
        assert [e.eventType for e in tickExpire.events] == [
            ReservationEventType.RESERVATION_EXPIRED,
            ReservationEventType.HANDOFF_INITIATED,
        ]
        assert mgr.currentState().findById(rAlice.reservationId).status is (
            ReservationStatus.EXPIRED
        )
        assert mgr.currentState().findById(rBob.reservationId).status is ReservationStatus.READY

        # Bob connects via RDP -> correlated into ACTIVE
        clock.advance(5)
        bobActiveTracker = trackerState(
            [
                trackedActive(
                    2,
                    "bob",
                    connectTime=clock.now(),
                    startEventTimestamp=clock.now(),
                )
            ],
            clock.now(),
        )
        tickClaim = mgr.evaluateTick(bobActiveTracker, clock.now())
        assert [e.eventType for e in tickClaim.events] == [
            ReservationEventType.HANDOFF_CLAIMED,
        ]
        assert mgr.currentState().findById(rBob.reservationId).status is ReservationStatus.ACTIVE

    def test_evaluate_tick_prunes_expired_note_without_events(
        self, paths: DataPaths, clock: FakeClock
    ) -> None:
        changes: list[tuple[QueueState, StartupNotice | None]] = []
        mgr = makeManager(
            paths,
            clock,
            onStateChanged=lambda state, notice: changes.append((state, notice)),
        )
        mgr.postStatusNote("alice", "Quick note", ttlMinutes=5)
        changes.clear()

        clock.advance(5 * 60 + 1)
        outcome = mgr.evaluateTick(trackerState([], clock.now()), clock.now())
        assert outcome.events == ()
        assert mgr.currentState().statusNotes == ()
        assert len(changes) == 1
        assert changes[0][1] is None


class TestReservationManagerConcurrency:
    def test_concurrent_mutations_and_ticks_are_serialized_safely(
        self, paths: DataPaths, clock: FakeClock
    ) -> None:
        mgr = makeManager(paths, clock, isPcAvailable=lambda: False)
        errors: list[Exception] = []
        barrier = threading.Barrier(6)

        def enqueueAndCancelWorker(idx: int) -> None:
            try:
                barrier.wait(timeout=5)
                for step in range(5):
                    user = f"user_{idx}_{step}"
                    res = mgr.enqueueReservation(user, 30)
                    if step % 2 == 0:
                        mgr.cancelReservation(res.reservationId)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        def noteWorker() -> None:
            try:
                barrier.wait(timeout=5)
                for step in range(5):
                    note = mgr.postStatusNote("noter", f"msg {step}", ttlMinutes=15)
                    mgr.clearStatusNote(note.noteId)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        def tickWorker() -> None:
            try:
                barrier.wait(timeout=5)
                for step in range(10):
                    now = T0 + timedelta(seconds=step)
                    mgr.evaluateTick(trackerState([], now), now)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [
            *(threading.Thread(target=enqueueAndCancelWorker, args=(i,)) for i in range(4)),
            threading.Thread(target=noteWorker),
            threading.Thread(target=tickWorker),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == []
        # 4 workers * 5 steps: 2 active QUEUED per worker (steps 1, 3) = 8 QUEUED reservations
        state = mgr.currentState()
        assert len(state.queuedReservations) == 8
        assert state.statusNotes == ()
        loaded = mgr.reservationStore.loadAtStartup()
        assert loaded.usable is True
        assert loaded.state == state
