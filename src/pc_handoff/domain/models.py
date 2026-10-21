"""Domain types for REQ-001: tracked sessions, usage periods, activity events and the PC status.

Pure data and factories with no I/O. JSON keys follow requirement section 5 exactly
(camelCase). Timestamps are offset-aware datetimes, serialized with ``clock.toIso``.

Every ``session_end`` is built by ``newEndEvent``, which applies the ADR-007 R11 ordering
clamps so that an end never precedes its start, even after a backward wall-clock step.
"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, TypeVar

from pc_handoff.clock import parseIso, toIso
from pc_handoff.detection.provider import ConnectionType

SCHEMA_VERSION = 1

T = TypeVar("T")


class PcStatus(StrEnum):
    """Derived PC status (requirement section 4). Later REQs may add values."""

    AVAILABLE = "Available"
    IN_USE = "In Use"
    UNKNOWN = "Unknown"


class SessionState(StrEnum):
    """State of a tracked user session (transitional OS states never reach the model)."""

    ACTIVE = "Active"
    DISCONNECTED = "Disconnected"


class EndCause(StrEnum):
    """Why a usage period ended (requirement section 3)."""

    DISCONNECT = "disconnect"
    LOGOFF = "logoff"
    RECONNECT_DETECTED = "reconnect_detected"
    UNKNOWN = "unknown"


class EventReason(StrEnum):
    """How the change behind an activity event was seen."""

    OBSERVED = "observed"
    DETECTED_AFTER_OUTAGE = "detected_after_outage"
    DETECTED_AT_STARTUP = "detected_at_startup"


class EventType(StrEnum):
    """Activity event type (requirement section 5)."""

    SESSION_START = "session_start"
    SESSION_END = "session_end"


@dataclass(frozen=True)
class UsagePeriod:
    """One contiguous Active interval of a session over one connection.

    ``startEventTimestamp`` is the ``timestamp`` of this period's ``session_start`` (S), or
    None when it is unknown (a period rebuilt from ``status.json`` at startup).
    ``connectTimeAtStart`` / ``connectionTypeAtStart`` describe the connection the period
    belongs to; both of the period's events carry them.
    """

    usagePeriodId: str
    usagePeriodStart: datetime
    durationApproximate: bool
    startEventTimestamp: datetime | None
    connectTimeAtStart: datetime | None
    connectionTypeAtStart: ConnectionType


@dataclass(frozen=True)
class TrackedSession:
    """In-memory record of one user session, as held by the tracker between polls.

    The last two fields are tracker bookkeeping, never persisted (``StatusSession`` does
    not carry them):

    - ``lastKnownConnectTime``: the last non-null connect time reported for this identity,
      the basis for detecting a changed connect time (Critic TASK-004 M3).
    - ``outagePriorPollAt``: set while the session holds an outage context through
      transitional polls; the ``lastSuccessfulPollAt`` from before the outage (M2).
    """

    sessionId: int
    userName: str
    domain: str | None
    state: SessionState
    connectionType: ConnectionType
    clientName: str | None
    clientAddress: str | None
    logonTime: datetime | None
    firstDetectedAt: datetime | None
    connectTime: datetime | None
    osDisconnectTime: datetime | None
    disconnectedSince: datetime | None
    openPeriod: UsagePeriod | None
    consecutiveTransitionalPolls: int = 0
    lastKnownConnectTime: datetime | None = None
    outagePriorPollAt: datetime | None = None


# --- JSON helpers -------------------------------------------------------------------------


def isoOrNone(dt: datetime | None) -> str | None:
    return None if dt is None else toIso(dt)


def parseIsoOrNone(value: Any) -> datetime | None:
    return None if value is None else parseIsoStrict(value)


def parseIsoStrict(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"timestamp must be a string, got {value!r}")
    return parseIso(value)


def enumOrNone(enumType: Callable[[Any], T], value: Any) -> T | None:
    return None if value is None else enumType(value)


def convertJson(kind: str, build: Callable[[], T]) -> T:
    """Run ``build`` and report every malformed-input failure as ``ValueError``."""
    try:
        return build()
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"malformed {kind}: {exc!r}") from exc


# --- activity events ----------------------------------------------------------------------


@dataclass(frozen=True)
class ActivityEvent:
    """One ``activity_log.json`` event. The last three fields are set only on ``session_end``."""

    eventId: str
    eventType: EventType
    usagePeriodId: str
    timestamp: datetime
    sessionId: int
    userName: str
    domain: str | None
    logonTime: datetime | None
    clientName: str | None
    clientAddress: str | None
    connectionType: ConnectionType
    connectTime: datetime | None
    usagePeriodStart: datetime
    durationApproximate: bool
    reason: EventReason
    periodEnd: datetime | None = None
    durationSeconds: int | None = None
    endCause: EndCause | None = None

    def toJson(self) -> dict[str, Any]:
        """Return the event with exactly the requirement section 5 keys, in that order."""
        doc: dict[str, Any] = {
            "eventId": self.eventId,
            "eventType": self.eventType.value,
            "usagePeriodId": self.usagePeriodId,
            "timestamp": toIso(self.timestamp),
            "sessionId": self.sessionId,
            "userName": self.userName,
            "domain": self.domain,
            "logonTime": isoOrNone(self.logonTime),
            "clientName": self.clientName,
            "clientAddress": self.clientAddress,
            "connectionType": self.connectionType.value,
            "connectTime": isoOrNone(self.connectTime),
            "usagePeriodStart": toIso(self.usagePeriodStart),
            "durationApproximate": self.durationApproximate,
            "reason": self.reason.value,
        }
        if self.eventType is EventType.SESSION_END:
            doc["periodEnd"] = isoOrNone(self.periodEnd)
            doc["durationSeconds"] = self.durationSeconds
            doc["endCause"] = None if self.endCause is None else self.endCause.value
        return doc

    @classmethod
    def fromJson(cls, doc: dict[str, Any]) -> "ActivityEvent":
        """Inverse of ``toJson``. Unknown keys are ignored; malformed input raises ValueError."""

        def build() -> ActivityEvent:
            eventType = EventType(doc["eventType"])
            isEnd = eventType is EventType.SESSION_END
            return cls(
                eventId=doc["eventId"],
                eventType=eventType,
                usagePeriodId=doc["usagePeriodId"],
                timestamp=parseIsoStrict(doc["timestamp"]),
                sessionId=doc["sessionId"],
                userName=doc["userName"],
                domain=doc["domain"],
                logonTime=parseIsoOrNone(doc["logonTime"]),
                clientName=doc["clientName"],
                clientAddress=doc["clientAddress"],
                connectionType=ConnectionType(doc["connectionType"]),
                connectTime=parseIsoOrNone(doc["connectTime"]),
                usagePeriodStart=parseIsoStrict(doc["usagePeriodStart"]),
                durationApproximate=doc["durationApproximate"],
                reason=EventReason(doc["reason"]),
                periodEnd=parseIsoOrNone(doc["periodEnd"]) if isEnd else None,
                durationSeconds=doc["durationSeconds"] if isEnd else None,
                endCause=enumOrNone(EndCause, doc["endCause"]) if isEnd else None,
            )

        return convertJson("activity event", build)


def eventBase(
    session: TrackedSession,
    period: UsagePeriod,
    eventType: EventType,
    timestamp: datetime,
    reason: EventReason,
    durationApproximate: bool,
) -> dict[str, Any]:
    """Fields shared by start and end events.

    Identity and client fields come from ``session`` as passed by the caller; the connection
    fields come from ``period``, so both events of a period describe the same connection.
    """
    return {
        "eventId": str(uuid.uuid4()),
        "eventType": eventType,
        "usagePeriodId": period.usagePeriodId,
        "timestamp": timestamp,
        "sessionId": session.sessionId,
        "userName": session.userName,
        "domain": session.domain,
        "logonTime": session.logonTime,
        "clientName": session.clientName,
        "clientAddress": session.clientAddress,
        "connectionType": period.connectionTypeAtStart,
        "connectTime": period.connectTimeAtStart,
        "usagePeriodStart": period.usagePeriodStart,
        "durationApproximate": durationApproximate,
        "reason": reason,
    }


def newStartEvent(
    session: TrackedSession, period: UsagePeriod, reason: EventReason, now: datetime
) -> ActivityEvent:
    """Build the ``session_start`` of ``period``. Its timestamp is ``now`` (never clamped)."""
    return ActivityEvent(
        **eventBase(
            session, period, EventType.SESSION_START, now, reason, period.durationApproximate
        )
    )


def newEndEvent(
    session: TrackedSession,
    period: UsagePeriod,
    periodEnd: datetime | None,
    endCause: EndCause,
    reason: EventReason,
    durationApproximate: bool,
    now: datetime,
    logger: logging.Logger,
) -> ActivityEvent:
    """Build the ``session_end`` of ``period``. This is the only way a ``session_end`` is made.

    Applies the ADR-007 R11 ordering clamps:

    - ``timestamp = max(now, period.startEventTimestamp)`` (``now`` when S is unknown)
    - ``periodEnd = max(periodEnd, period.usagePeriodStart)`` when ``periodEnd`` is not None

    then computes ``durationSeconds`` from the clamped ``periodEnd``. When any clamp changes a
    value, exactly one WARNING is logged naming every clamped field and the largest correction.
    """
    # Imported here: end_time imports this module's types, so a top-level import would cycle.
    from pc_handoff.domain.end_time import durationSeconds

    clampedFields: list[str] = []
    correctionSeconds = 0
    timestamp = now
    start = period.startEventTimestamp
    if start is not None and start > now:
        timestamp = start
        clampedFields.append("timestamp")
        correctionSeconds = int((start - now).total_seconds())
    if periodEnd is not None and periodEnd < period.usagePeriodStart:
        clampedFields.append("periodEnd")
        correctionSeconds = max(
            correctionSeconds, int((period.usagePeriodStart - periodEnd).total_seconds())
        )
        periodEnd = period.usagePeriodStart
    if clampedFields:
        logger.warning(
            "wall clock moved backwards by %d s; end clamped (usagePeriodId %s, fields %s)",
            correctionSeconds,
            period.usagePeriodId,
            ", ".join(clampedFields),
        )

    return ActivityEvent(
        **eventBase(session, period, EventType.SESSION_END, timestamp, reason, durationApproximate),
        periodEnd=periodEnd,
        durationSeconds=durationSeconds(period, periodEnd),
        endCause=endCause,
    )


# --- status.json --------------------------------------------------------------------------


@dataclass(frozen=True)
class StatusSession:
    """One ``status.json`` ``sessions[]`` entry, exactly as persisted."""

    sessionId: int
    userName: str
    domain: str | None
    state: SessionState
    connectionType: ConnectionType
    clientName: str | None
    clientAddress: str | None
    logonTime: datetime | None
    firstDetectedAt: datetime | None
    connectTime: datetime | None
    usagePeriodId: str | None
    usagePeriodStart: datetime | None
    durationApproximate: bool
    disconnectedSince: datetime | None

    @classmethod
    def fromTracked(cls, session: TrackedSession) -> "StatusSession":
        """Project a tracked session onto the persisted entry.

        The period fields come from ``openPeriod`` for an Active session and are null (with
        ``durationApproximate`` false) for a Disconnected one; ``disconnectedSince`` is null
        for an Active session.

        Raises ``ValueError`` for an Active session with no open period: only a tracker bug
        produces one, and its null period fields would make ``status.json`` invalid (ADR-005).
        """
        isActive = session.state is SessionState.ACTIVE
        if isActive and session.openPeriod is None:
            raise ValueError(
                f"Active session has no open usage period (sessionId {session.sessionId})"
            )
        period = session.openPeriod if isActive else None
        return cls(
            sessionId=session.sessionId,
            userName=session.userName,
            domain=session.domain,
            state=session.state,
            connectionType=session.connectionType,
            clientName=session.clientName,
            clientAddress=session.clientAddress,
            logonTime=session.logonTime,
            firstDetectedAt=session.firstDetectedAt,
            connectTime=session.connectTime,
            usagePeriodId=None if period is None else period.usagePeriodId,
            usagePeriodStart=None if period is None else period.usagePeriodStart,
            durationApproximate=False if period is None else period.durationApproximate,
            disconnectedSince=None if isActive else session.disconnectedSince,
        )

    def toJson(self) -> dict[str, Any]:
        return {
            "sessionId": self.sessionId,
            "userName": self.userName,
            "domain": self.domain,
            "state": self.state.value,
            "connectionType": self.connectionType.value,
            "clientName": self.clientName,
            "clientAddress": self.clientAddress,
            "logonTime": isoOrNone(self.logonTime),
            "firstDetectedAt": isoOrNone(self.firstDetectedAt),
            "connectTime": isoOrNone(self.connectTime),
            "usagePeriodId": self.usagePeriodId,
            "usagePeriodStart": isoOrNone(self.usagePeriodStart),
            "durationApproximate": self.durationApproximate,
            "disconnectedSince": isoOrNone(self.disconnectedSince),
        }

    @classmethod
    def fromJson(cls, doc: dict[str, Any]) -> "StatusSession":
        return cls(
            sessionId=doc["sessionId"],
            userName=doc["userName"],
            domain=doc["domain"],
            state=SessionState(doc["state"]),
            connectionType=ConnectionType(doc["connectionType"]),
            clientName=doc["clientName"],
            clientAddress=doc["clientAddress"],
            logonTime=parseIsoOrNone(doc["logonTime"]),
            firstDetectedAt=parseIsoOrNone(doc["firstDetectedAt"]),
            connectTime=parseIsoOrNone(doc["connectTime"]),
            usagePeriodId=doc["usagePeriodId"],
            usagePeriodStart=parseIsoOrNone(doc["usagePeriodStart"]),
            durationApproximate=doc["durationApproximate"],
            disconnectedSince=parseIsoOrNone(doc["disconnectedSince"]),
        )


@dataclass(frozen=True)
class StatusDocument:
    """The ``status.json`` snapshot (requirement section 5)."""

    status: PcStatus
    lastKnownStatus: PcStatus | None
    lastKnownStatusAt: datetime | None
    lastSuccessfulPollAt: datetime | None
    updatedAt: datetime
    detectionHealthy: bool
    sessions: tuple[StatusSession, ...]
    schemaVersion: int = SCHEMA_VERSION

    def toJson(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schemaVersion,
            "status": self.status.value,
            "lastKnownStatus": None if self.lastKnownStatus is None else self.lastKnownStatus.value,
            "lastKnownStatusAt": isoOrNone(self.lastKnownStatusAt),
            "lastSuccessfulPollAt": isoOrNone(self.lastSuccessfulPollAt),
            "updatedAt": toIso(self.updatedAt),
            "detectionHealthy": self.detectionHealthy,
            "sessions": [s.toJson() for s in self.sessions],
        }

    @classmethod
    def fromJson(cls, doc: dict[str, Any]) -> "StatusDocument":
        """Inverse of ``toJson``. Unknown keys are ignored; malformed input raises ValueError."""

        def build() -> StatusDocument:
            return cls(
                schemaVersion=doc["schemaVersion"],
                status=PcStatus(doc["status"]),
                lastKnownStatus=enumOrNone(PcStatus, doc["lastKnownStatus"]),
                lastKnownStatusAt=parseIsoOrNone(doc["lastKnownStatusAt"]),
                lastSuccessfulPollAt=parseIsoOrNone(doc["lastSuccessfulPollAt"]),
                updatedAt=parseIsoStrict(doc["updatedAt"]),
                detectionHealthy=doc["detectionHealthy"],
                sessions=tuple(StatusSession.fromJson(s) for s in doc["sessions"]),
            )

        return convertJson("status document", build)
