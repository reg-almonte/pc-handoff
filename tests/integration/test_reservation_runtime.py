"""Integration tests for ``AppRuntime``, ``Poller``, and ``ReservationManager`` (TASK-014)."""

import json
from pathlib import Path
from typing import Any

import pytest

from pc_handoff.domain.models import PcStatus
from pc_handoff.domain.reservations import (
    CLAIM_GRACE_SECONDS,
    HandoffStatus,
    NoteCategory,
    ReservationEventType,
    ReservationStatus,
)
from pc_handoff.launcher import buildRuntime
from pc_handoff.notifications.notifier import HostNotification, formatEventNotification
from pc_handoff.notifications.windows_notifier import WindowsNotifier
from pc_handoff.runtime.app_runtime import POLL_OK, AppRuntime
from pc_handoff.runtime.poller import Poller
from pc_handoff.runtime.state_store import NoticeOrigin
from tests.conftest import FAKE_CLOCK_START
from tests.fakes import FakeClock, FakeSessionProvider
from tests.fakes.builders import osSession

T0 = FAKE_CLOCK_START


@pytest.fixture
def dataDir(tmp_path: Path) -> Path:
    directory = tmp_path / "data"
    directory.mkdir()
    return directory


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(T0)


def readEvents(dataDir: Path) -> list[dict[str, Any]]:
    raw = json.loads((dataDir / "activity_log.json").read_text(encoding="utf-8"))
    return raw["events"]


def readReservations(dataDir: Path) -> dict[str, Any]:
    return json.loads((dataDir / "reservations.json").read_text(encoding="utf-8"))


class TestAppRuntimeReservationLifecycle:
    def test_ui_mutations_immediately_publish_snapshot_and_surface_runtime_notices(
        self, dataDir: Path, clock: FakeClock
    ) -> None:
        provider = FakeSessionProvider([])
        runtime: AppRuntime = buildRuntime(dataDir, provider, clock)

        # Before first poll, detectionHealthy is False -> _isPcAvailable() is False
        rAlice = runtime.reservationManager.enqueueReservation("alice", 60, notes="First")
        assert rAlice.status is ReservationStatus.QUEUED
        assert runtime.stateStore.current().queueState.queuedReservations == (rAlice,)

        # First poll reconciles PC as Available -> alice advances to READY
        assert runtime.pollOnce("t-1") == POLL_OK
        snap = runtime.stateStore.current()
        assert snap.status is PcStatus.AVAILABLE
        assert snap.queueState.readyReservation is not None
        assert snap.queueState.readyReservation.reservationId == rAlice.reservationId

        # Delete activity_log.json while running, then trigger a UI mutation (postStatusNote)
        # so _onReservationStateChanged receives a runtime StartupNotice from ActivityLogStore
        (dataDir / "activity_log.json").unlink()
        clock.advance(5)
        note = runtime.reservationManager.postStatusNote("alice", "Connecting shortly")
        snapAfterNote = runtime.stateStore.current()
        assert snapAfterNote.queueState.statusNotes == (note,)
        assert any(
            n.origin is NoticeOrigin.RUNTIME
            and "activity_log.json was missing and has been recreated" in n.text
            for n in snapAfterNote.notices
        )

    def test_poller_correlates_rdp_session_expires_timeout_and_completes_on_logoff(
        self, dataDir: Path, clock: FakeClock
    ) -> None:
        provider = FakeSessionProvider([osSession(1, "alice", connectTime=T0)])
        runtime = buildRuntime(dataDir, provider, clock)
        assert runtime.pollOnce("t-init") == POLL_OK
        assert runtime.stateStore.current().status is PcStatus.IN_USE

        # Bob and Carol join the queue while Alice is using the PC
        clock.advance(5)
        rBob = runtime.reservationManager.enqueueReservation("bob", 45)
        rCarol = runtime.reservationManager.enqueueReservation("carol", 30)
        assert rBob.status is ReservationStatus.QUEUED
        assert rCarol.status is ReservationStatus.QUEUED

        # Carol updates her queued reservation duration and notes
        clock.advance(2)
        rCarol = runtime.reservationManager.updateReservation(
            rCarol.reservationId,
            60,
            notes="Extended test run",
        )
        assert rCarol.requestedDurationMinutes == 60
        assert rCarol.notes == "Extended test run"

        # Alice logs off -> poll cycle sees PC Available and promotes Bob to READY
        clock.advance(5)
        provider.setSessions([])
        assert runtime.pollOnce("t-alice-off") == POLL_OK
        qState = runtime.stateStore.current().queueState
        assert qState.findById(rBob.reservationId).status is ReservationStatus.READY
        assert qState.handoff.status is HandoffStatus.OFFERED
        assert qState.handoff.targetUserName == "bob"

        # Bob does not claim within CLAIM_GRACE_SECONDS -> poll cycle expires Bob and promotes Carol
        clock.advance(CLAIM_GRACE_SECONDS + 1)
        assert runtime.pollOnce("t-bob-timeout") == POLL_OK
        qState = runtime.stateStore.current().queueState
        assert qState.findById(rBob.reservationId).status is ReservationStatus.EXPIRED
        assert qState.findById(rCarol.reservationId).status is ReservationStatus.READY
        assert qState.handoff.targetUserName == "carol"

        # Carol connects via RDP -> poll cycle correlates Carol's session and marks Carol ACTIVE
        clock.advance(10)
        provider.setSessions([osSession(2, "CAROL", connectTime=clock.now())])
        assert runtime.pollOnce("t-carol-connect") == POLL_OK
        qState = runtime.stateStore.current().queueState
        assert qState.findById(rCarol.reservationId).status is ReservationStatus.ACTIVE
        assert qState.handoff.status is HandoffStatus.CLAIMED

        # Carol logs off -> poll cycle completes Carol's ACTIVE reservation
        clock.advance(600)
        provider.setSessions([])
        assert runtime.pollOnce("t-carol-off") == POLL_OK
        qState = runtime.stateStore.current().queueState
        assert qState.findById(rCarol.reservationId).status is ReservationStatus.COMPLETED

        # Verify persisted reservations.json and activity_log.json
        persistedRes = readReservations(dataDir)
        statusesById = {r["reservationId"]: r["status"] for r in persistedRes["reservations"]}
        assert statusesById == {
            rBob.reservationId: "EXPIRED",
            rCarol.reservationId: "COMPLETED",
        }
        eventTypes = [e["eventType"] for e in readEvents(dataDir)]
        assert eventTypes == [
            "session_start",
            ReservationEventType.RESERVATION_QUEUED.value,
            ReservationEventType.RESERVATION_QUEUED.value,
            ReservationEventType.RESERVATION_UPDATED.value,
            "session_end",
            ReservationEventType.HANDOFF_INITIATED.value,
            ReservationEventType.RESERVATION_EXPIRED.value,
            ReservationEventType.HANDOFF_INITIATED.value,
            "session_start",
            ReservationEventType.HANDOFF_CLAIMED.value,
            "session_end",
        ]

    def test_runtime_wires_notifier_for_ui_mutations_and_poller_ticks(
        self, dataDir: Path, clock: FakeClock
    ) -> None:
        notified: list[HostNotification] = []

        class SpyNotifier:
            def notify(self, notification: HostNotification) -> None:
                notified.append(notification)

            def notifyEvents(self, events, state=None) -> None:
                for ev in events:
                    n = formatEventNotification(ev, state)
                    if n is not None:
                        self.notify(n)

            def close(self) -> None:
                pass

        # Default buildRuntime creates a WindowsNotifier when notifications are enabled
        defaultRuntime = buildRuntime(dataDir, FakeSessionProvider([]), clock)
        assert isinstance(defaultRuntime.reservationManager.notifier, WindowsNotifier)
        defaultRuntime.reservationManager.notifier.close()

        # Custom notifier passed to buildRuntime receives queue join, handoff, and expire alerts
        provider = FakeSessionProvider([osSession(1, "alice", connectTime=T0)])
        runtime = buildRuntime(dataDir, provider, clock, notifier=SpyNotifier())
        assert runtime.pollOnce("t-1") == POLL_OK

        rBob = runtime.reservationManager.enqueueReservation("bob", 30)
        rCarol = runtime.reservationManager.enqueueReservation("carol", 30)
        runtime.reservationManager.postStatusNote(
            "alice",
            "Almost done",
            category=NoteCategory.URGENT,
        )
        assert [n.eventType for n in notified] == [
            ReservationEventType.RESERVATION_QUEUED,
            ReservationEventType.RESERVATION_QUEUED,
        ]

        # Alice disconnects -> Bob promoted to READY on poll tick -> HANDOFF_INITIATED notified
        clock.advance(10)
        provider.setSessions([])
        assert runtime.pollOnce("t-2") == POLL_OK
        assert [n.eventType for n in notified] == [
            ReservationEventType.RESERVATION_QUEUED,
            ReservationEventType.RESERVATION_QUEUED,
            ReservationEventType.HANDOFF_INITIATED,
        ]
        assert notified[-1].reservationId == rBob.reservationId

        # Bob expires -> RESERVATION_EXPIRED andCarol's HANDOFF_INITIATED notified
        clock.advance(CLAIM_GRACE_SECONDS + 1)
        assert runtime.pollOnce("t-3") == POLL_OK
        assert [n.eventType for n in notified] == [
            ReservationEventType.RESERVATION_QUEUED,
            ReservationEventType.RESERVATION_QUEUED,
            ReservationEventType.HANDOFF_INITIATED,
            ReservationEventType.RESERVATION_EXPIRED,
            ReservationEventType.HANDOFF_INITIATED,
        ]
        assert notified[-1].reservationId == rCarol.reservationId

    def test_poller_thread_drives_reservation_tick_evaluation(
        self, dataDir: Path, clock: FakeClock
    ) -> None:
        provider = FakeSessionProvider([])
        runtime = buildRuntime(dataDir, provider, clock)
        rAlice = runtime.reservationManager.enqueueReservation("alice", 30, pcAvailable=False)
        assert rAlice.status is ReservationStatus.QUEUED

        poller = Poller(
            runtime=runtime,
            source=runtime.source,
            intervalSeconds=1,
            clock=clock,
            logger=runtime.logger,
        )
        poller.start()
        try:
            # Wait for the immediate first poll on the poller thread
            for _ in range(50):
                snap = runtime.stateStore.current()
                if snap.queueState.readyReservation is not None:
                    break
                import time

                time.sleep(0.02)
            assert runtime.stateStore.current().queueState.readyReservation is not None
        finally:
            poller.stop(timeout=2)
