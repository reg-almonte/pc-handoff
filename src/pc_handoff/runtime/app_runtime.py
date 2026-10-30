"""One poll cycle: enumerate -> tracker or reconcile -> activity log -> status.json -> publish.

``AppRuntime.pollOnce`` is called only from the poller thread (ADR-002), which makes it the
only caller of the timed source and of both stores (TASK-002 F2, TASK-006 S8).

**Commit point** (ADR-002, Critic MAJOR-3). As soon as ``applySuccess``, ``applyFailure`` or
``reconcileStartup`` returns, and before any I/O, the returned state becomes
``trackerState`` (and the reconciliation flag is set). From then on the poll's events live
only in its batch, so no later exception can make the next poll re-derive them.

**Hand-over** (ADR-003). ``handedOver`` is set immediately before ``appendBatch``: the
store's first statement queues the batch. An exception after the commit point but before
that call re-queues the batch with ``enqueuePending``; an exception from inside the call or
later never does, because the store already holds the events and skips any it already wrote.

**Failure containment.** Every ``Exception`` in a cycle is logged with the poll's traceId,
an Unknown snapshot is published and nothing propagates (requirement Resilience). A poll
that raised before the commit point counts as a failed poll for the tracker: its
``applyFailure`` result (no events) is committed, so the next successful poll applies the
outage rule to what it finds (A7).
"""

import logging
import sys
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime

from pc_handoff.app_logging import newTraceId, traceScope
from pc_handoff.clock import Clock
from pc_handoff.detection.provider import SessionEnumerationError
from pc_handoff.detection.timed_source import TimedSessionSource
from pc_handoff.domain.models import PcStatus
from pc_handoff.domain.reservations import QueueState
from pc_handoff.domain.tracker import PollOutcome, SessionTracker, TrackerState
from pc_handoff.notifications.notifier import Notifier
from pc_handoff.persistence.activity_log_store import ActivityLogStore, AuditEvent
from pc_handoff.persistence.atomic import PersistenceError
from pc_handoff.persistence.config_store import Config
from pc_handoff.persistence.notices import StartupNotice
from pc_handoff.persistence.paths import STATUS_FILE_NAME
from pc_handoff.persistence.startup_loader import (
    StartupContext,
    pendingDisplayState,
    reconcileStartup,
)
from pc_handoff.persistence.status_store import StatusStore
from pc_handoff.runtime.reservation_manager import ReservationManager
from pc_handoff.runtime.state_store import (
    MAX_NOTICES,
    DashboardSnapshot,
    Notice,
    NoticeOrigin,
    StateStore,
)

POLL_OK = "ok"
POLL_ENUMERATION_FAILED = "enumeration_failed"
POLL_ERROR = "error"


@dataclass
class PollCycle:
    """Progress of one ``pollOnce``, read by its exception handler."""

    committed: bool = False
    handedOver: bool = False
    batch: list[AuditEvent] = field(default_factory=list)


class AppRuntime:
    """The application's in-memory core: committed tracker state, stores and snapshot.

    ``config`` is the startup configuration, fixed for the process lifetime (REQ AC-14).
    The constructor publishes the pre-poll snapshot (``pendingDisplayState``, REQ AC-27).
    """

    def __init__(
        self,
        config: Config,
        configWarnings: tuple[str, ...],
        startupContext: StartupContext,
        source: TimedSessionSource,
        tracker: SessionTracker,
        statusStore: StatusStore,
        activityStore: ActivityLogStore,
        stateStore: StateStore,
        clock: Clock,
        logger: logging.Logger,
        *,
        notifier: Notifier | None = None,
    ) -> None:
        self.config = config
        self.configWarnings = tuple(configWarnings)
        self.startupContext = startupContext
        self.source = source
        self.tracker = tracker
        self.statusStore = statusStore
        self.activityStore = activityStore
        self.stateStore = stateStore
        self.clock = clock
        self.logger = logger
        # Before the first successful poll only this state (and applyFailure results derived
        # from it) is shown or persisted; reconciliation replaces it (TASK-008 S6).
        self.trackerState: TrackerState = pendingDisplayState(startupContext)
        self.reconciliationDone = False
        # Only the most recent notices are retained (TASK-010, Critic TASK-009 S7).
        # Thread confinement: this deque is mutated (persistEvents) and iterated
        # (publishState -> DashboardSnapshot.fromState) only on the poller thread, apart from
        # here in __init__, before the runtime is registered and the poller starts. Readers
        # (the Streamlit script threads) only ever see the snapshot's immutable tuple, so
        # tuple(deque) can never race an append (Critic TASK-010 N5).
        self.notices: deque[Notice] = deque(
            (Notice.fromStoreNotice(n, NoticeOrigin.STARTUP) for n in startupContext.notices),
            maxlen=MAX_NOTICES,
        )
        assert startupContext.reservationStore is not None
        claimGraceSeconds = startupContext.config.claimGraceMinutes * 60
        self.knownUsersStore = startupContext.knownUsersStore
        self.reservationManager = ReservationManager(
            reservationStore=startupContext.reservationStore,
            activityStore=activityStore,
            clock=clock,
            logger=logger,
            initialState=startupContext.queueState,
            isPcAvailable=self._isPcAvailable,
            onStateChanged=self._onReservationStateChanged,
            claimGraceSeconds=claimGraceSeconds,
            notifier=notifier,
            knownUsersStore=self.knownUsersStore,
        )
        self.publishState(self.trackerState)

    def _isPcAvailable(self) -> bool:
        return self.trackerState.detectionHealthy and self.trackerState.status is PcStatus.AVAILABLE

    def _onReservationStateChanged(
        self,
        queueState: QueueState,
        notice: StartupNotice | None,
    ) -> None:
        if notice is not None:
            self.notices.append(Notice.fromStoreNotice(notice, NoticeOrigin.RUNTIME))
        self.publishState(self.trackerState)

    # --- the poll cycle -------------------------------------------------------------------

    def pollOnce(self, traceId: str | None = None) -> str:
        """Run one poll under ``traceId`` (fresh when omitted); never raises ``Exception``.

        Returns ``POLL_OK``, ``POLL_ENUMERATION_FAILED`` or ``POLL_ERROR``.
        """
        with traceScope(traceId or newTraceId()):
            started = self.clock.monotonic()
            cycle = PollCycle()
            outcome = POLL_ERROR
            try:
                outcome = self.runCycle(cycle)
            except Exception as exc:
                self.recoverFromError(cycle, exc)
            finally:
                self.logPollCompleted(started, outcome, cycle)
        return outcome

    def runCycle(self, cycle: PollCycle) -> str:
        try:
            sessions = self.source.enumerate()
            failure: Exception | None = None
        except Exception as exc:  # any enumerate() failure fails the poll (TASK-002 F2/S7)
            sessions, failure = [], exc
        detectionTime = self.clock.now()

        reconciling = failure is None and not self.reconciliationDone
        if failure is not None:
            self.logEnumerationFailure(failure)
            result = self.tracker.applyFailure(self.trackerState, failure, detectionTime)
        elif reconciling:
            result = reconcileStartup(self.startupContext, self.tracker, sessions, detectionTime)
        else:
            result = self.tracker.applySuccess(self.trackerState, sessions, detectionTime)
        self.commit(cycle, result, reconciling, detectionTime)

        self._beforeAppend()
        self.persistEvents(cycle)
        self.writeStatus(self.trackerState)
        self.reservationManager.writeCommittedTick()
        self.publishState(self.trackerState)
        return POLL_OK if failure is None else POLL_ENUMERATION_FAILED

    def commit(
        self,
        cycle: PollCycle,
        result: PollOutcome,
        reconciled: bool,
        detectionTime: datetime,
    ) -> None:
        """The commit point: plain assignments, no I/O."""
        self.trackerState = result.state
        if reconciled:
            self.reconciliationDone = True
        reservationOutcome = self.reservationManager.commitTick(
            self.trackerState,
            detectionTime,
            reconciling=reconciled,
        )
        cycle.batch = [*result.events, *reservationOutcome.events]
        cycle.committed = True

    def _beforeAppend(self) -> None:
        """Test hook between the commit point and ``appendBatch`` (AC-10 case c)."""

    def persistEvents(self, cycle: PollCycle) -> None:
        """Write the batch after any still-pending events; a no-op when there are none."""
        if not cycle.batch and self.activityStore.pendingCount() == 0:
            return
        cycle.handedOver = True  # immediately before the call: the store queues at entry
        result = self.activityStore.appendBatch(cycle.batch)
        # Every notice is passed through, whatever ``written`` says: a backup happened even if
        # the write after it failed. The store emits each notice once (Critic TASK-009 B1).
        if result.notice is not None:
            with self.reservationManager.lock:
                self.notices.append(Notice.fromStoreNotice(result.notice, NoticeOrigin.RUNTIME))

    def writeStatus(self, state: TrackerState) -> None:
        """Write ``status.json``; a ``PersistenceError`` is a failed write, not a crash."""
        try:
            self.statusStore.write(state)
        except PersistenceError as exc:
            self.logger.error(
                "%s could not be written; the in-memory state stays authoritative",
                STATUS_FILE_NAME,
                extra={"detail": str(exc)},
            )

    def publishState(self, state: TrackerState) -> None:
        with self.reservationManager.lock:
            self.stateStore.publish(
                DashboardSnapshot.fromState(
                    state,
                    updatedAt=self.clock.now(),
                    configWarnings=self.configWarnings,
                    notices=self.notices,
                    pollIntervalSeconds=self.config.sessionPollIntervalSeconds,
                    queueState=self.reservationManager.currentState(),
                )
            )

    # --- failures -------------------------------------------------------------------------

    def logEnumerationFailure(self, failure: Exception) -> None:
        if isinstance(failure, SessionEnumerationError):
            detail = {
                "kind": failure.kind,
                "message": failure.message,
                "winerror": failure.winerror,
            }
        else:
            message = f"{type(failure).__name__}: {failure}"
            detail = {"kind": "error", "message": message, "winerror": None}
        self.logger.warning("session enumeration failed", extra={"detail": detail})

    def recoverFromError(self, cycle: PollCycle, exc: Exception) -> None:
        """Contain an unexpected exception from a cycle. Never raises ``Exception``."""
        self.logger.error(
            "poll failed with an unexpected error",
            exc_info=(type(exc), exc, exc.__traceback__),
            extra={
                "detail": {
                    "error": f"{type(exc).__name__}: {exc}",
                    "committed": cycle.committed,
                    "handedOver": cycle.handedOver,
                    "eventsInBatch": len(cycle.batch),
                }
            },
        )
        try:
            if cycle.committed:
                if not cycle.handedOver and cycle.batch:
                    # Never reached the store: queue it, so the next poll writes it.
                    self.activityStore.enqueuePending(cycle.batch)
                    self.logger.info("%d event(s) queued for the next poll", len(cycle.batch))
                # The committed state stays authoritative; only the display is degraded.
                display = replace(
                    self.trackerState, status=PcStatus.UNKNOWN, detectionHealthy=False
                )
            else:
                # Nothing was committed or emitted: the poll failed as a whole.
                self.trackerState = self.tracker.applyFailure(
                    self.trackerState, exc, self.clock.now()
                ).state
                display = self.trackerState
                self.writeStatus(display)
            self.publishState(display)
        except Exception:
            self.logger.error("poll error recovery failed", exc_info=sys.exc_info())

    def logPollCompleted(self, started: float, outcome: str, cycle: PollCycle) -> None:
        durationMs = round((self.clock.monotonic() - started) * 1000)
        self.logger.info(
            "poll completed",
            extra={
                "detail": {
                    "durationMs": int(durationMs),
                    "outcome": outcome,
                    "eventsInBatch": len(cycle.batch),
                    "pendingEvents": self.activityStore.pendingCount(),
                }
            },
        )
