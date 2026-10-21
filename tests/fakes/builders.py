"""Builders for tracker-level tests, shared with later tasks (TASK-008, TASK-009, TASK-011).

- ``osSession(...)``: an ``OsSession`` whose ``state`` may also be a raw WTS name such as
  ``"Shadow"`` or ``"ConnectQuery"`` (mapped to ``TRANSITIONAL`` with ``rawStateName``
  ``"WTS<name>"``).
- ``usagePeriod(...)`` / ``trackedActive(...)`` / ``trackedDisconnected(...)``: in-memory
  tracked records with exact control over C (usagePeriodStart), S (startEventTimestamp) etc.
- ``trackerState(...)``: a ``TrackerState`` holding the given sessions.
"""

import uuid
from datetime import datetime
from typing import Any

from pc_handoff.detection.provider import ConnectionType, OsConnectState, OsSession
from pc_handoff.domain.models import (
    PcStatus,
    SessionState,
    TrackedSession,
    UsagePeriod,
)
from pc_handoff.domain.status import deriveStatus
from pc_handoff.domain.tracker import TrackerState
from tests.fakes.fake_session_provider import DEFAULT_LOGON_TIME, makeSession

TRANSITIONAL_RAW_NAMES = ("Connected", "ConnectQuery", "Shadow", "Idle", "Init", "Reset", "Down")


def osSession(
    sessionId: int,
    user: str | None,
    state: str | OsConnectState = "Active",
    connectionType: str | ConnectionType = "RDP",
    **overrides: Any,
) -> OsSession:
    """``makeSession`` that also accepts a raw transitional WTS state name for ``state``."""
    if isinstance(state, str) and state in TRANSITIONAL_RAW_NAMES:
        overrides.setdefault("rawStateName", f"WTS{state}")
        state = OsConnectState.TRANSITIONAL
    return makeSession(sessionId, user, state, connectionType, **overrides)


def usagePeriod(
    usagePeriodStart: datetime,
    startEventTimestamp: datetime | None,
    *,
    connectTimeAtStart: datetime | None = None,
    connectionType: ConnectionType = ConnectionType.RDP,
    durationApproximate: bool = False,
    usagePeriodId: str | None = None,
) -> UsagePeriod:
    return UsagePeriod(
        usagePeriodId=usagePeriodId or str(uuid.uuid4()),
        usagePeriodStart=usagePeriodStart,
        durationApproximate=durationApproximate,
        startEventTimestamp=startEventTimestamp,
        connectTimeAtStart=connectTimeAtStart,
        connectionTypeAtStart=connectionType,
    )


def trackedActive(
    sessionId: int,
    user: str,
    *,
    connectTime: datetime | None,
    startEventTimestamp: datetime | None,
    usagePeriodStart: datetime | None = None,
    connectionType: ConnectionType = ConnectionType.RDP,
    logonTime: datetime | None = DEFAULT_LOGON_TIME,
    clientName: str | None = "CLIENT-PC",
    clientAddress: str | None = "192.168.1.20",
    domain: str | None = "CORP",
    durationApproximate: bool | None = None,
) -> TrackedSession:
    """An Active tracked session with an open period.

    ``usagePeriodStart`` defaults to ``connectTime`` (or S when the connect time is None, in
    which case the period is approximate).
    """
    start = usagePeriodStart or connectTime or startEventTimestamp
    assert start is not None, "trackedActive needs a usagePeriodStart"
    approx = (connectTime is None) if durationApproximate is None else durationApproximate
    period = usagePeriod(
        start,
        startEventTimestamp,
        connectTimeAtStart=connectTime,
        connectionType=connectionType,
        durationApproximate=approx,
    )
    return TrackedSession(
        sessionId=sessionId,
        userName=user,
        domain=domain,
        state=SessionState.ACTIVE,
        connectionType=connectionType,
        clientName=clientName,
        clientAddress=clientAddress,
        logonTime=logonTime,
        firstDetectedAt=startEventTimestamp,
        connectTime=connectTime,
        osDisconnectTime=None,
        disconnectedSince=None,
        openPeriod=period,
    )


def trackedDisconnected(
    sessionId: int,
    user: str,
    *,
    disconnectedSince: datetime,
    connectTime: datetime | None = DEFAULT_LOGON_TIME,
    connectionType: ConnectionType = ConnectionType.RDP,
    logonTime: datetime | None = DEFAULT_LOGON_TIME,
    domain: str | None = "CORP",
) -> TrackedSession:
    return TrackedSession(
        sessionId=sessionId,
        userName=user,
        domain=domain,
        state=SessionState.DISCONNECTED,
        connectionType=connectionType,
        clientName=None,
        clientAddress=None,
        logonTime=logonTime,
        firstDetectedAt=disconnectedSince,
        connectTime=connectTime,
        osDisconnectTime=disconnectedSince,
        disconnectedSince=disconnectedSince,
        openPeriod=None,
    )


def trackerState(
    sessions: list[TrackedSession],
    lastSuccessfulPollAt: datetime | None,
    *,
    outagePending: bool = False,
) -> TrackerState:
    """A state as left by a successful poll at P (or by a failure after it, with an outage)."""
    status = deriveStatus(sessions)
    return TrackerState(
        sessions={s.sessionId: s for s in sessions},
        lastSuccessfulPollAt=lastSuccessfulPollAt,
        lastKnownStatus=status if lastSuccessfulPollAt is not None else None,
        lastKnownStatusAt=lastSuccessfulPollAt,
        status=PcStatus.UNKNOWN if outagePending else status,
        detectionHealthy=not outagePending,
        outagePending=outagePending,
    )
