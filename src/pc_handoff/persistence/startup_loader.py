"""Startup loading: config, ``status.json`` and ``activity_log.json`` once, in order (TASK-008).

``loadStartup`` runs after the single-instance lock and app logging (ADR-002, ADR-004):

1. ``cleanupTempFiles``, then ``loadConfig``, ``StatusStore.loadAtStartup`` and
   ``ActivityLogStore.loadAtStartup``, in that order. Each store validates fully before it
   converts (G3/G5) and backs up a corrupt file with a notice (REQ AC-17, AC-18).
2. The open usage periods come from one source (requirement section 6):

   1. a usable ``activity_log.json``: every ``session_start`` without a ``session_end``. It
      wins over ``status.json`` when they disagree; ``status.json`` then only supplies P.
   2. otherwise a usable ``status.json``: every entry with a non-null ``usagePeriodId``.
      A **missing** log counts as not usable, like a corrupt one (pending requirement
      amendment 4), so a deleted log keeps AC-18's continuity.
   3. otherwise none. When a file was found corrupt, periods left open in its backup cannot
      be closed: an accepted limitation, logged at WARNING.

Nothing is reconciled here: ``reconcileStartup`` runs at the first successful poll (REQ
AC-27), and ``pendingDisplayState`` is what is shown until then.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pc_handoff.clock import Clock
from pc_handoff.detection.provider import OsSession
from pc_handoff.domain.models import PcStatus, StatusDocument, StatusSession, TrackedSession
from pc_handoff.domain.reconciliation import (
    OpenPeriodRecord,
    Reconciler,
    openPeriodsFromEvents,
    openPeriodsFromStatus,
    trackedFromStatusSession,
)
from pc_handoff.domain.tracker import PollOutcome, SessionTracker, TrackerState
from pc_handoff.persistence.activity_log_store import ActivityLogStore
from pc_handoff.persistence.atomic import cleanupTempFiles
from pc_handoff.persistence.config_store import Config, loadConfig
from pc_handoff.persistence.notices import StartupNotice
from pc_handoff.persistence.paths import ACTIVITY_LOG_FILE_NAME, STATUS_FILE_NAME, DataPaths
from pc_handoff.persistence.status_store import StatusStore

SOURCE_THREE_WARNING = "open usage periods in backups cannot be closed (accepted limitation)"


class OpenPeriodSource(StrEnum):
    """Where the open usage periods were rebuilt from (requirement section 6)."""

    ACTIVITY_LOG = ACTIVITY_LOG_FILE_NAME
    STATUS = STATUS_FILE_NAME
    NONE = "none"


@dataclass(frozen=True)
class StartupContext:
    """Everything loaded at startup, handed to the runtime (TASK-009).

    ``statusStore`` and ``activityStore`` are the instances that loaded the files; the
    activity store keeps its validated cache, so the runtime must use these instances.
    ``notices`` are in file order (config, status, activity log); ``configWarnings`` are the
    per-field config fallbacks (REQ AC-15). ``lastSuccessfulPollAt`` (P) and
    ``statusDocument`` come from a usable ``status.json`` only.
    """

    config: Config
    configWarnings: tuple[str, ...]
    notices: tuple[StartupNotice, ...]
    openPeriodSource: OpenPeriodSource
    openPeriods: tuple[OpenPeriodRecord, ...]
    lastSuccessfulPollAt: datetime | None
    statusDocument: StatusDocument | None
    statusUsable: bool
    activityLogUsable: bool
    statusStore: StatusStore
    activityStore: ActivityLogStore

    @property
    def statusSessions(self) -> tuple[StatusSession, ...]:
        return () if self.statusDocument is None else self.statusDocument.sessions


def loadStartup(paths: DataPaths, clock: Clock, logger: logging.Logger) -> StartupContext:
    """Load every data file once and choose the open-period source. Never raises for file
    problems; each store reports them through notices and the app log."""
    cleanupTempFiles(paths.dataDir, logger)
    configResult = loadConfig(paths, clock, logger)
    statusStore = StatusStore(paths, clock, logger)
    statusResult = statusStore.loadAtStartup()
    activityStore = ActivityLogStore(paths, clock, logger)
    activityResult = activityStore.loadAtStartup()

    notices = tuple(
        n
        for n in (configResult.notice, statusResult.notice, activityResult.notice)
        if n is not None
    )
    statusDocument = statusResult.document if statusResult.usable else None

    if activityResult.usable:
        source = OpenPeriodSource.ACTIVITY_LOG
        openPeriods = openPeriodsFromEvents(activityResult.events or [])
    elif statusDocument is not None:
        source = OpenPeriodSource.STATUS
        openPeriods = openPeriodsFromStatus(statusDocument.sessions)
    else:
        source = OpenPeriodSource.NONE
        openPeriods = []

    if source is OpenPeriodSource.NONE:
        if statusResult.notice is not None or activityResult.notice is not None:
            logger.warning(SOURCE_THREE_WARNING)
        logger.info("no open usage periods to rebuild")
    else:
        logger.info("open usage periods rebuilt from %s: %d", source.value, len(openPeriods))

    return StartupContext(
        config=configResult.config,
        configWarnings=tuple(configResult.warnings),
        notices=notices,
        openPeriodSource=source,
        openPeriods=tuple(openPeriods),
        lastSuccessfulPollAt=None
        if statusDocument is None
        else statusDocument.lastSuccessfulPollAt,
        statusDocument=statusDocument,
        statusUsable=statusResult.usable,
        activityLogUsable=activityResult.usable,
        statusStore=statusStore,
        activityStore=activityStore,
    )


def pendingDisplayState(ctx: StartupContext) -> TrackerState:
    """The state shown before the first successful poll (REQ AC-27).

    Unknown and unhealthy, with the last-known sessions and status from a usable
    ``status.json`` (for display only; TASK-005 S10), or nothing. A sessionId listed twice
    keeps its first entry. Nothing here is used for reconciliation.
    """
    doc = ctx.statusDocument
    sessions: dict[int, TrackedSession] = {}
    for entry in () if doc is None else doc.sessions:
        sessions.setdefault(entry.sessionId, trackedFromStatusSession(entry))
    return TrackerState(
        sessions={sid: sessions[sid] for sid in sorted(sessions)},
        lastSuccessfulPollAt=None if doc is None else doc.lastSuccessfulPollAt,
        lastKnownStatus=None if doc is None else doc.lastKnownStatus,
        lastKnownStatusAt=None if doc is None else doc.lastKnownStatusAt,
        status=PcStatus.UNKNOWN,
        detectionHealthy=False,
        outagePending=False,
    )


def reconcileStartup(
    ctx: StartupContext,
    tracker: SessionTracker,
    osSessions: list[OsSession],
    detectionTime: datetime,
) -> PollOutcome:
    """Run startup reconciliation for ``ctx`` at the first successful poll."""
    return Reconciler(tracker).reconcile(
        ctx.openPeriods,
        ctx.lastSuccessfulPollAt,
        ctx.statusSessions,
        osSessions,
        detectionTime,
    )
