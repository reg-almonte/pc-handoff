"""End-time, duration and end-cause rules for usage periods (ADR-007 R4, R7, R8, R11).

Pure functions with no I/O. Window bounds are inclusive on both sides.

- ``computeObservedEnd``: a transition seen directly (requirement section 2).
- ``computeBlindEnd``: a transition not seen directly (requirement section 3, A7).
- ``computeDisconnectedSince``: interpretation I2, sharing ``computeObservedEnd``'s window so
  that ``disconnectedSince == periodEnd`` for every observed disconnect (REQ AC-5).
"""

import logging
from datetime import datetime
from enum import Enum

from pc_handoff.domain.models import EndCause, UsagePeriod

logger = logging.getLogger(__name__)


class ObservedState(Enum):
    """What the current poll shows for the identity whose period is ending."""

    DISCONNECTED = "disconnected"
    GONE = "gone"
    IDENTITY_REPLACED = "identity_replaced"
    ACTIVE_RECONNECTED = "active_reconnected"


END_CAUSE_BY_OBSERVED_STATE = {
    ObservedState.DISCONNECTED: EndCause.DISCONNECT,
    ObservedState.GONE: EndCause.LOGOFF,
    ObservedState.IDENTITY_REPLACED: EndCause.LOGOFF,
    ObservedState.ACTIVE_RECONNECTED: EndCause.RECONNECT_DETECTED,
}


def acceptDisconnectTime(
    osDisconnectTime: datetime | None, lower: datetime, upper: datetime
) -> datetime | None:
    """Return ``osDisconnectTime`` if it is known and ``lower <= t <= upper``, else None."""
    if osDisconnectTime is not None and lower <= osDisconnectTime <= upper:
        return osDisconnectTime
    return None


def computeObservedEnd(
    period: UsagePeriod, osDisconnectTime: datetime | None, detectionTime: datetime
) -> datetime:
    """End of a directly seen Active -> Disconnected (or logoff) transition.

    The OS disconnect time if accepted in [usagePeriodStart, detectionTime], otherwise the
    detection time. For a logoff the caller passes ``osDisconnectTime=None``.
    """
    accepted = acceptDisconnectTime(osDisconnectTime, period.usagePeriodStart, detectionTime)
    return detectionTime if accepted is None else accepted


def computeBlindEnd(
    period: UsagePeriod,
    osDisconnectTime: datetime | None,
    windowUpper: datetime,
    lastSuccessfulPollAt: datetime | None,
    startEventTimestamp: datetime | None,
) -> datetime | None:
    """End of a transition not seen directly (ADR-007 R7).

    1. The OS disconnect time if accepted in [usagePeriodStart, windowUpper].
    2. Otherwise the later of the known values among P (``lastSuccessfulPollAt``) and S
       (``startEventTimestamp``).
    3. Otherwise None. Unreachable through the runtime paths; kept as the schema allows it.
    """
    accepted = acceptDisconnectTime(osDisconnectTime, period.usagePeriodStart, windowUpper)
    if accepted is not None:
        return accepted
    known = [v for v in (lastSuccessfulPollAt, startEventTimestamp) if v is not None]
    return max(known) if known else None


def reconnectWindowUpper(newConnectTime: datetime | None, detectionTime: datetime) -> datetime:
    """Upper bound of a reconnect's acceptance window: the new connect time if reported."""
    return detectionTime if newConnectTime is None else newConnectTime


def durationSeconds(period: UsagePeriod, periodEnd: datetime | None) -> int | None:
    """Whole seconds from ``usagePeriodStart`` to ``periodEnd``, or None when it is unknown.

    ``newEndEvent`` clamps ``periodEnd`` first, so the result is 0 or more by construction.
    The guard below only fires on a call that bypassed ``newEndEvent``; it logs an ERROR
    because that is a bug, and returns 0.
    """
    if periodEnd is None:
        return None
    seconds = int((periodEnd - period.usagePeriodStart).total_seconds())
    if seconds < 0:
        logger.error(
            "negative duration %d s guarded to 0 (usagePeriodId %s, usagePeriodStart %s, "
            "periodEnd %s); periodEnd was not clamped by newEndEvent",
            seconds,
            period.usagePeriodId,
            period.usagePeriodStart.isoformat(),
            periodEnd.isoformat(),
        )
        return 0
    return seconds


def deriveEndCause(observed: ObservedState | None) -> EndCause:
    """Map the state observed now to the period's ``endCause`` (requirement section 3)."""
    if observed is None:
        return EndCause.UNKNOWN
    return END_CAUSE_BY_OBSERVED_STATE[observed]


def computeDisconnectedSince(
    osDisconnectTime: datetime | None,
    openPeriod: UsagePeriod | None,
    reportedConnectTime: datetime | None,
    detectionTime: datetime,
) -> datetime:
    """``disconnectedSince`` of a session seen Disconnected (ADR-007 R8, interpretation I2).

    - With an open period (Active -> Disconnected): exactly ``computeObservedEnd``, so the
      value equals that period's ``periodEnd`` for every observed disconnect.
    - Without one (first seen Disconnected): the OS disconnect time if it is not later than
      the detection time and not earlier than the reported connect time (when known);
      otherwise the detection time.
    """
    if openPeriod is not None:
        return computeObservedEnd(openPeriod, osDisconnectTime, detectionTime)
    if osDisconnectTime is None or osDisconnectTime > detectionTime:
        return detectionTime
    if reportedConnectTime is not None and osDisconnectTime < reportedConnectTime:
        return detectionTime
    return osDisconnectTime
