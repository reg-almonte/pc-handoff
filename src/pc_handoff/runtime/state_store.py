"""In-memory dashboard state: an immutable snapshot behind a reference swap (ADR-002).

The poller builds a complete ``DashboardSnapshot`` and then ``publish``es it; the dashboard
calls ``current()``, a plain reference read. The publish lock is held only for the
assignment, and readers never take it, so rendering can never wait on a poll (REQ AC-21)
and never sees a half-updated state.
"""

import threading
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from pc_handoff.domain.models import PcStatus, TrackedSession
from pc_handoff.domain.reservations import QueueState, emptyQueueState
from pc_handoff.domain.tracker import TrackerState
from pc_handoff.persistence.notices import StartupNotice

# Notices retained by the runtime and shown on the page: the most recent ones only, so a
# repeating runtime notice cannot grow memory or the page without bound (TASK-010, Critic
# TASK-009 S7). Notices are rare (a damaged or missing data file), so 20 is ample.
MAX_NOTICES = 20


class NoticeOrigin(StrEnum):
    """Whether a notice was raised while loading the files or while running."""

    STARTUP = "startup"
    RUNTIME = "runtime"


@dataclass(frozen=True)
class Notice:
    """A one-time notice for the dashboard (REQ AC-17, AC-18; ADR-003 runtime resets).

    ``noticeId`` is unique per notice, so a viewer can dismiss one notice without hiding a
    later one with the same text (ADR-008).
    """

    noticeId: str
    origin: NoticeOrigin
    fileName: str
    backupName: str | None
    text: str

    @classmethod
    def fromStoreNotice(cls, notice: StartupNotice, origin: NoticeOrigin) -> "Notice":
        return cls(uuid.uuid4().hex, origin, notice.fileName, notice.backupName, notice.text)


@dataclass(frozen=True)
class DashboardSnapshot:
    """Everything the dashboard renders, as of one publish. Never mutated."""

    status: PcStatus
    lastKnownStatus: PcStatus | None
    lastKnownStatusAt: datetime | None
    lastSuccessfulPollAt: datetime | None
    detectionHealthy: bool
    sessions: tuple[TrackedSession, ...]
    updatedAt: datetime
    configWarnings: tuple[str, ...]
    notices: tuple[Notice, ...]
    pollIntervalSeconds: int
    queueState: QueueState
    knownUsers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def fromState(
        cls,
        state: TrackerState,
        *,
        updatedAt: datetime,
        configWarnings: Iterable[str],
        notices: Iterable[Notice],
        pollIntervalSeconds: int,
        queueState: QueueState | None = None,
        knownUsers: dict[str, str] | None = None,
    ) -> "DashboardSnapshot":
        """The snapshot of ``state``, sessions in sessionId order and only the most recent
        ``MAX_NOTICES`` notices."""
        return cls(
            status=state.status,
            lastKnownStatus=state.lastKnownStatus,
            lastKnownStatusAt=state.lastKnownStatusAt,
            lastSuccessfulPollAt=state.lastSuccessfulPollAt,
            detectionHealthy=state.detectionHealthy,
            sessions=tuple(state.sessions[sid] for sid in sorted(state.sessions)),
            updatedAt=updatedAt,
            configWarnings=tuple(configWarnings),
            notices=tuple(notices)[-MAX_NOTICES:],
            pollIntervalSeconds=pollIntervalSeconds,
            queueState=emptyQueueState(updatedAt) if queueState is None else queueState,
            knownUsers={} if knownUsers is None else dict(knownUsers),
        )


class StateStore:
    """Holds the latest ``DashboardSnapshot``; safe to read from any thread."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.snapshot: DashboardSnapshot | None = None

    def publish(self, snapshot: DashboardSnapshot) -> None:
        """Replace the current snapshot. The lock covers only the assignment."""
        if not isinstance(snapshot, DashboardSnapshot):
            raise TypeError(f"expected a DashboardSnapshot, got {type(snapshot).__name__}")
        with self.lock:
            self.snapshot = snapshot

    def current(self) -> DashboardSnapshot | None:
        """The last published snapshot (None before the first publish). Never blocks."""
        return self.snapshot
