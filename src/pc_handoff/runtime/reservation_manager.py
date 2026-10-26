"""Thread-safe coordinator for reservations, ownership handoff, and status notes (SD001 section 4).

``ReservationManager`` owns an internal ``threading.RLock`` that serializes all reads and
mutations of ``QueueState``, ``ReservationStore`` writes, and coordination with
``ActivityLogStore`` across the poller thread and Streamlit UI threads (DEC-010).
"""

import logging
import threading
from collections.abc import Callable
from datetime import datetime

from pc_handoff.clock import Clock
from pc_handoff.domain.models import EventReason, PcStatus
from pc_handoff.domain.reservations import (
    CLAIM_GRACE_SECONDS,
    DEFAULT_NOTE_TTL_MINUTES,
    NoteCategory,
    QueueState,
    Reservation,
    ReservationAuditEvent,
    StatusNote,
    TransitionOutcome,
    emptyQueueState,
)
from pc_handoff.domain.reservations import (
    cancelReservation as domainCancelReservation,
)
from pc_handoff.domain.reservations import (
    claimHandoff as domainClaimHandoff,
)
from pc_handoff.domain.reservations import (
    clearStatusNote as domainClearStatusNote,
)
from pc_handoff.domain.reservations import (
    enqueueReservation as domainEnqueueReservation,
)
from pc_handoff.domain.reservations import (
    evaluateTick as domainEvaluateTick,
)
from pc_handoff.domain.reservations import (
    initiateHandoff as domainInitiateHandoff,
)
from pc_handoff.domain.reservations import (
    postStatusNote as domainPostStatusNote,
)
from pc_handoff.domain.reservations import (
    updateReservation as domainUpdateReservation,
)
from pc_handoff.domain.tracker import TrackerState
from pc_handoff.notifications.notifier import Notifier, NullNotifier
from pc_handoff.persistence.activity_log_store import ActivityLogStore
from pc_handoff.persistence.atomic import PersistenceError
from pc_handoff.persistence.notices import StartupNotice
from pc_handoff.persistence.paths import RESERVATIONS_FILE_NAME
from pc_handoff.persistence.reservation_store import ReservationStore


class ReservationManager:
    """Thread-safe authority for ``QueueState`` across the poller and UI threads."""

    def __init__(
        self,
        reservationStore: ReservationStore,
        activityStore: ActivityLogStore,
        clock: Clock,
        logger: logging.Logger,
        *,
        initialState: QueueState | None = None,
        isPcAvailable: Callable[[], bool] | None = None,
        onStateChanged: Callable[[QueueState, StartupNotice | None], None] | None = None,
        claimGraceSeconds: int = CLAIM_GRACE_SECONDS,
        notifier: Notifier | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self.reservationStore = reservationStore
        self.activityStore = activityStore
        self.clock = clock
        self.logger = logger
        self._state: QueueState = (
            emptyQueueState(clock.now()) if initialState is None else initialState
        )
        self._dirty = False
        self._pendingTickEvents: tuple[ReservationAuditEvent, ...] = ()
        self._isPcAvailable = isPcAvailable
        self._onStateChanged = onStateChanged
        self.claimGraceSeconds = claimGraceSeconds
        self.notifier: Notifier = NullNotifier() if notifier is None else notifier

    @property
    def lock(self) -> threading.RLock:
        """The manager's re-entrant lock, shared with ``AppRuntime`` for snapshot publishing."""
        return self._lock

    def currentState(self) -> QueueState:
        """Return the current immutable ``QueueState``."""
        with self._lock:
            return self._state

    def enqueueReservation(
        self,
        userName: str,
        requestedDurationMinutes: int,
        *,
        notes: str | None = None,
        pcAvailable: bool | None = None,
    ) -> Reservation:
        """Add a reservation for ``userName`` and immediately persist and publish."""
        with self._lock:
            now = self.clock.now()
            outcome = domainEnqueueReservation(
                self._state,
                userName,
                requestedDurationMinutes,
                now,
                notes=notes,
                pcAvailable=self._resolvePcAvailable(pcAvailable),
                claimGraceSeconds=self.claimGraceSeconds,
            )
            self._applyMutation(outcome)
            created = outcome.state.findByUser(userName)
            assert created is not None
            return created

    def updateReservation(
        self,
        reservationId: str,
        requestedDurationMinutes: int,
        *,
        notes: str | None = None,
    ) -> Reservation:
        """Update duration and notes on a ``QUEUED`` reservation and persist and publish."""
        with self._lock:
            now = self.clock.now()
            outcome = domainUpdateReservation(
                self._state,
                reservationId,
                requestedDurationMinutes,
                now,
                notes=notes,
            )
            self._applyMutation(outcome)
            updated = outcome.state.findById(reservationId)
            assert updated is not None
            return updated

    def cancelReservation(
        self,
        reservationId: str,
        *,
        pcAvailable: bool | None = None,
    ) -> Reservation:
        """Cancel a non-terminal reservation by ``reservationId`` and persist and publish."""
        with self._lock:
            now = self.clock.now()
            outcome = domainCancelReservation(
                self._state,
                reservationId,
                now,
                pcAvailable=self._resolvePcAvailable(pcAvailable),
                claimGraceSeconds=self.claimGraceSeconds,
            )
            self._applyMutation(outcome)
            cancelled = outcome.state.findById(reservationId)
            assert cancelled is not None
            return cancelled

    def initiateHandoff(self, offeredByUserName: str | None = None) -> QueueState:
        """Complete any ``ACTIVE`` reservation and offer handoff to the next queued user."""
        with self._lock:
            now = self.clock.now()
            outcome = domainInitiateHandoff(
                self._state,
                offeredByUserName,
                now,
                claimGraceSeconds=self.claimGraceSeconds,
            )
            self._applyMutation(outcome)
            return outcome.state

    def claimHandoff(self, reservationId: str) -> Reservation:
        """Claim a ``READY`` (or head ``QUEUED``) reservation into ``ACTIVE``."""
        with self._lock:
            now = self.clock.now()
            outcome = domainClaimHandoff(self._state, reservationId, now)
            self._applyMutation(outcome)
            claimed = outcome.state.findById(reservationId)
            assert claimed is not None
            return claimed

    def postStatusNote(
        self,
        userName: str,
        message: str,
        *,
        ttlMinutes: int = DEFAULT_NOTE_TTL_MINUTES,
        category: NoteCategory | str = NoteCategory.GENERAL,
    ) -> StatusNote:
        """Post a broadcast status note and immediately persist and publish."""
        with self._lock:
            now = self.clock.now()
            outcome = domainPostStatusNote(
                self._state,
                userName,
                message,
                now,
                ttlMinutes=ttlMinutes,
                category=category,
            )
            self._applyMutation(outcome)
            return outcome.state.statusNotes[0]

    def clearStatusNote(self, noteId: str) -> QueueState:
        """Remove an active status note by ``noteId`` and immediately persist and publish."""
        with self._lock:
            now = self.clock.now()
            outcome = domainClearStatusNote(self._state, noteId, now)
            self._applyMutation(outcome)
            return outcome.state

    def commitTick(
        self,
        trackerState: TrackerState,
        now: datetime,
        *,
        reconciling: bool = False,
    ) -> TransitionOutcome:
        """Evaluate session correlation, claim timeouts, and note TTLs at ``now`` without I/O.

        Used by ``AppRuntime.commit`` at the poll commit point before persisting the cycle's
        combined batch and writing ``reservations.json`` via ``writeCommittedTick``.
        """
        with self._lock:
            if trackerState.detectionHealthy:
                activeSessions = tuple(trackerState.sessions.values())
                pcAvailable = trackerState.status is PcStatus.AVAILABLE
            else:
                activeSessions = ()
                pcAvailable = False
            reason = EventReason.DETECTED_AT_STARTUP if reconciling else EventReason.OBSERVED
            outcome = domainEvaluateTick(
                self._state,
                activeSessions,
                pcAvailable,
                now,
                reason=reason,
                claimGraceSeconds=self.claimGraceSeconds,
            )
            if outcome.state != self._state:
                self._state = outcome.state
                self._dirty = True
            self._pendingTickEvents = outcome.events
            return outcome

    def writeCommittedTick(self, *, notify: bool = True) -> None:
        """Write ``reservations.json`` if the last ``commitTick`` changed ``QueueState``."""
        with self._lock:
            pendingEvents = self._pendingTickEvents
            self._pendingTickEvents = ()
            if self._dirty:
                self._dirty = False
                self._writeState(self._state)
            if notify and pendingEvents:
                self.notifier.notifyEvents(pendingEvents, self._state)

    def evaluateTick(
        self,
        trackerState: TrackerState,
        now: datetime,
        *,
        reconciling: bool = False,
    ) -> TransitionOutcome:
        """Evaluate a tick and immediately persist and notify when ``QueueState`` changes."""
        with self._lock:
            outcome = self.commitTick(trackerState, now, reconciling=reconciling)
            if self._dirty:
                self.writeCommittedTick(notify=False)
                self._persistEventsAndNotify(outcome.events)
            return outcome

    def _resolvePcAvailable(self, pcAvailable: bool | None) -> bool:
        if pcAvailable is not None:
            return pcAvailable
        if self._isPcAvailable is not None:
            return self._isPcAvailable()
        return False

    def _applyMutation(self, outcome: TransitionOutcome) -> None:
        self._state = outcome.state
        self._dirty = False
        self._writeState(self._state)
        self._persistEventsAndNotify(outcome.events)

    def _persistEventsAndNotify(self, events: tuple[ReservationAuditEvent, ...]) -> None:
        notice: StartupNotice | None = None
        if events:
            result = self.activityStore.appendBatch(events)
            notice = result.notice
            self.notifier.notifyEvents(events, self._state)
        if self._onStateChanged is not None:
            self._onStateChanged(self._state, notice)

    def _writeState(self, state: QueueState) -> None:
        try:
            self.reservationStore.write(state)
        except PersistenceError as exc:
            self.logger.error(
                "%s could not be written; the in-memory state stays authoritative",
                RESERVATIONS_FILE_NAME,
                extra={"detail": str(exc)},
            )
