"""Crafted ``DashboardSnapshot`` fixtures for the dashboard tests (TASK-010).

Built on the TASK-004 builders (``trackedActive``, ``trackedDisconnected``, ``trackerState``),
so a snapshot looks exactly like one the runtime publishes after a poll.
"""

from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime

from pc_handoff.domain.models import PcStatus, TrackedSession
from pc_handoff.persistence.notices import StartupNotice
from pc_handoff.runtime.state_store import DashboardSnapshot, Notice, NoticeOrigin
from tests.fakes.builders import trackerState


def healthySnapshot(
    sessions: Iterable[TrackedSession],
    polledAt: datetime,
    *,
    configWarnings: Iterable[str] = (),
    notices: Iterable[Notice] = (),
    updatedAt: datetime | None = None,
) -> DashboardSnapshot:
    """The snapshot published right after a successful poll at ``polledAt``."""
    state = trackerState(list(sessions), polledAt)
    return DashboardSnapshot.fromState(
        state,
        updatedAt=updatedAt or polledAt,
        configWarnings=configWarnings,
        notices=notices,
        pollIntervalSeconds=5,
    )


def unknownSnapshot(
    sessions: Iterable[TrackedSession],
    lastSuccessfulPollAt: datetime | None,
    *,
    lastKnownStatus: PcStatus | None = None,
    failedAt: datetime | None = None,
    configWarnings: Iterable[str] = (),
    notices: Iterable[Notice] = (),
) -> DashboardSnapshot:
    """The snapshot published after a failed poll; P = ``lastSuccessfulPollAt``.

    ``lastKnownStatus`` defaults to the status derived from ``sessions`` when P is known, and
    is None on a fresh install (P None), as the tracker leaves it (REQ AC-27).
    """
    state = trackerState(list(sessions), lastSuccessfulPollAt, outagePending=True)
    if lastKnownStatus is not None:
        state = replace(state, lastKnownStatus=lastKnownStatus)
    return DashboardSnapshot.fromState(
        state,
        updatedAt=failedAt or lastSuccessfulPollAt or datetime.now().astimezone(),
        configWarnings=configWarnings,
        notices=notices,
        pollIntervalSeconds=5,
    )


def notice(text: str, fileName: str = "status.json", backupName: str | None = None) -> Notice:
    """A startup notice as the runtime publishes it (unique ``noticeId``)."""
    return Notice.fromStoreNotice(StartupNotice(fileName, backupName, text), NoticeOrigin.STARTUP)
