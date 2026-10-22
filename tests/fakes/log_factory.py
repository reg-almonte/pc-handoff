"""Synthetic, valid ``activity_log.json`` content for store tests and the perf benchmark.

- ``makeEvents(n, openPeriods=k)``: ``n`` events forming a log that passes
  ``validateActivityDocument``: closed periods (``session_start`` then ``session_end``), ``k``
  open periods (a ``session_start`` only) and, when ``n - k`` is odd, one ``session_end`` whose
  start is not in the file (the AC-18 shape). Events carry every field with realistic values
  (about 550 bytes each serialized), so 10,000 events is about the ADR-003 size estimate.
- ``startEvent(...)`` / ``endEvent(...)``: single events with explicit control over the
  period id, timestamps and event id.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from pc_handoff.detection.provider import ConnectionType
from pc_handoff.domain.models import ActivityEvent, EndCause, EventReason, EventType

JST = timezone(timedelta(hours=9))
LOG_BASE_TIME = datetime(2026, 9, 1, 8, 0, 0, tzinfo=JST)
USERS = ("alice", "bob", "carol", "dave")


def newId() -> str:
    return str(uuid.uuid4())


def startEvent(
    usagePeriodId: str | None = None,
    timestamp: datetime = LOG_BASE_TIME,
    *,
    eventId: str | None = None,
    sessionId: int = 2,
    userName: str = "alice",
    usagePeriodStart: datetime | None = None,
    **overrides: Any,
) -> ActivityEvent:
    """A ``session_start`` whose period starts at ``usagePeriodStart`` (default: ``timestamp``)."""
    fields: dict[str, Any] = {
        "eventId": eventId or newId(),
        "eventType": EventType.SESSION_START,
        "usagePeriodId": usagePeriodId or newId(),
        "timestamp": timestamp,
        "sessionId": sessionId,
        "userName": userName,
        "domain": "CORP",
        "logonTime": timestamp - timedelta(hours=1),
        "clientName": "CLIENT-PC-0042",
        "clientAddress": "192.168.100.123",
        "connectionType": ConnectionType.RDP,
        "connectTime": usagePeriodStart or timestamp,
        "usagePeriodStart": usagePeriodStart or timestamp,
        "durationApproximate": False,
        "reason": EventReason.OBSERVED,
    }
    fields.update(overrides)
    return ActivityEvent(**fields)


def endEvent(
    usagePeriodId: str,
    timestamp: datetime,
    *,
    usagePeriodStart: datetime,
    eventId: str | None = None,
    sessionId: int = 2,
    userName: str = "alice",
    **overrides: Any,
) -> ActivityEvent:
    """A ``session_end`` with ``periodEnd`` = ``timestamp`` unless overridden."""
    periodEnd = overrides.pop("periodEnd", timestamp)
    duration = None if periodEnd is None else int((periodEnd - usagePeriodStart).total_seconds())
    fields: dict[str, Any] = {
        "eventId": eventId or newId(),
        "eventType": EventType.SESSION_END,
        "usagePeriodId": usagePeriodId,
        "timestamp": timestamp,
        "sessionId": sessionId,
        "userName": userName,
        "domain": "CORP",
        "logonTime": usagePeriodStart - timedelta(hours=1),
        "clientName": "CLIENT-PC-0042",
        "clientAddress": "192.168.100.123",
        "connectionType": ConnectionType.RDP,
        "connectTime": usagePeriodStart,
        "usagePeriodStart": usagePeriodStart,
        "durationApproximate": False,
        "reason": EventReason.OBSERVED,
        "periodEnd": periodEnd,
        "durationSeconds": duration,
        "endCause": EndCause.DISCONNECT,
    }
    fields.update(overrides)
    return ActivityEvent(**fields)


def makeEvents(n: int, openPeriods: int = 0) -> list[ActivityEvent]:
    """``n`` valid events, in timestamp order, of which ``openPeriods`` are unmatched starts."""
    if not 0 <= openPeriods <= n:
        raise ValueError("openPeriods must be between 0 and n")
    closedPairs, orphanEnd = divmod(n - openPeriods, 2)
    events: list[ActivityEvent] = []
    moment = LOG_BASE_TIME
    if orphanEnd:
        events.append(
            endEvent(newId(), moment, usagePeriodStart=moment - timedelta(hours=2), userName="erin")
        )
    for index in range(closedPairs):
        user = USERS[index % len(USERS)]
        sessionId = 2 + index % len(USERS)
        periodId = newId()
        start = moment + timedelta(minutes=1)
        end = start + timedelta(minutes=30)
        events.append(startEvent(periodId, start, sessionId=sessionId, userName=user))
        events.append(
            endEvent(periodId, end, usagePeriodStart=start, sessionId=sessionId, userName=user)
        )
        moment = end
    for index in range(openPeriods):
        moment += timedelta(minutes=1)
        events.append(startEvent(newId(), moment, sessionId=20 + index, userName=f"open{index}"))
    return events
