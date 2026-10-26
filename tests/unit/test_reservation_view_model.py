"""Unit tests for reservation, handoff, and status-note view models (TASK-015)."""

from dataclasses import replace
from datetime import timedelta

from pc_handoff.domain.reservations import (
    HandoffState,
    HandoffStatus,
    QueueState,
    emptyQueueState,
    enqueueReservation,
    evaluateTick,
    initiateHandoff,
    postStatusNote,
)
from pc_handoff.runtime.state_store import DashboardSnapshot
from pc_handoff.ui.view_model import (
    EMPTY_QUEUE_MESSAGE,
    RESERVATION_COLUMNS,
    RESERVATION_HEADING,
    STATUS_NOTES_HEADING,
    buildViewModel,
    formatTime,
    handoffBannerLine,
)
from tests.conftest import FAKE_CLOCK_START
from tests.fakes.builders import trackerState

T0 = FAKE_CLOCK_START


def snapshotWithQueue(queueState: QueueState) -> DashboardSnapshot:
    return DashboardSnapshot.fromState(
        trackerState([], T0),
        updatedAt=T0,
        configWarnings=(),
        notices=(),
        pollIntervalSeconds=5,
        queueState=queueState,
    )


class TestReservationViewModel:
    def test_empty_queue_state_view_model(self) -> None:
        vm = buildViewModel(snapshotWithQueue(emptyQueueState(T0)), T0)
        assert vm.reservationHeading == RESERVATION_HEADING
        assert vm.reservationRows == ()
        assert vm.emptyQueueMessage == EMPTY_QUEUE_MESSAGE
        assert vm.handoffBanner is None
        assert vm.statusNotesHeading == STATUS_NOTES_HEADING
        assert vm.statusNoteItems == ()

    def test_reservation_rows_countdown_claimability_and_sanitization(self) -> None:
        # 1. Head QUEUED when no ACTIVE or READY exists -> canClaim is True
        q1 = enqueueReservation(
            emptyQueueState(T0),
            "alice\x00",
            45,
            T0,
            notes="Model\x07 run",
            pcAvailable=False,
        ).state
        q2 = enqueueReservation(q1, "bob", 30, T0, notes=None, pcAvailable=False).state
        vmQueued = buildViewModel(snapshotWithQueue(q2), T0)
        assert vmQueued.emptyQueueMessage is None
        assert len(vmQueued.reservationRows) == 2
        row1, row2 = vmQueued.reservationRows
        assert row1.canClaim is True
        assert row1.canCancel is True
        assert row1.cells() == (
            "1",
            "alice",
            "QUEUED",
            "45 min",
            formatTime(T0),
            "-",
            "Model run",
        )
        assert len(row1.cells()) == len(RESERVATION_COLUMNS)
        assert row2.canClaim is False
        assert row2.notes == "-"

        # 2. Promote alice to READY via initiateHandoff -> countdown and handoffBanner shown
        qReady = initiateHandoff(q2, "dave", T0, claimGraceSeconds=300).state
        vmReady = buildViewModel(snapshotWithQueue(qReady), T0 + timedelta(seconds=60))
        readyRow, queuedRow = vmReady.reservationRows
        assert readyRow.status == "READY"
        assert readyRow.claimCountdown == "00:04:00"
        assert readyRow.canClaim is True
        assert queuedRow.canClaim is False
        assert vmReady.handoffBanner == "Handoff offered by dave to alice — claim within 00:04:00"

    def test_handoff_banner_variants(self) -> None:
        # Automatic offer (offeredByUserName is None)
        autoOffer = HandoffState(
            status=HandoffStatus.OFFERED,
            reservationId="r1",
            offeredByUserName=None,
            targetUserName="alice",
            offeredAt=T0,
            expiresAt=T0 + timedelta(seconds=120),
        )
        assert handoffBannerLine(autoOffer, T0) == "PC is ready for alice — claim within 00:02:00"

        # Offer without expiresAt (defensive fallback)
        noExpiryOffer = replace(autoOffer, expiresAt=None)
        assert (
            handoffBannerLine(noExpiryOffer, T0) == "PC is ready for alice — claim within Unknown"
        )

        # Timed out handoff
        q1 = enqueueReservation(emptyQueueState(T0), "alice", 30, T0, pcAvailable=True).state
        qExpired = evaluateTick(q1, (), False, T0 + timedelta(seconds=301)).state
        assert handoffBannerLine(qExpired.handoff, T0 + timedelta(seconds=301)) == (
            "Handoff to alice timed out"
        )

        # Idle handoff
        assert handoffBannerLine(HandoffState.idle(), T0) is None

    def test_status_note_items_filter_expired_and_sanitize(self) -> None:
        q1 = postStatusNote(
            emptyQueueState(T0),
            "alice\x00",
            "Step\x07ping out",
            T0,
            ttlMinutes=10,
        ).state
        q2 = postStatusNote(
            q1,
            "bob",
            "Longer note",
            T0,
            ttlMinutes=60,
        ).state

        # At T0 + 5 min, both notes are active
        vmActive = buildViewModel(snapshotWithQueue(q2), T0 + timedelta(minutes=5))
        assert len(vmActive.statusNoteItems) == 2
        assert vmActive.statusNoteItems[1].user == "alice"
        assert vmActive.statusNoteItems[1].message == "Stepping out"
        assert "[alice] Stepping out" in vmActive.statusNoteItems[1].line

        # At T0 + 15 min, alice's 10m note has expired; only bob's remains
        vmLater = buildViewModel(snapshotWithQueue(q2), T0 + timedelta(minutes=15))
        assert len(vmLater.statusNoteItems) == 1
        assert vmLater.statusNoteItems[0].user == "bob"
