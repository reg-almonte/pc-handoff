"""Startup reconciliation of open usage periods (requirement section 6, TASK-008).

Pure logic with no I/O apart from app-log lines. The persistence side (which files are
usable, which source wins) lives in ``persistence/startup_loader.py``.

At the first successful poll after a start, every open period rebuilt from disk
(``OpenPeriodRecord``) is turned back into the tracked session it belonged to before the
restart, and that session is classified against the snapshot by the same ``SessionTracker``
rules that run on every poll (ADR-007 R1-R11, I1-I4, N1), in a ``detected_at_startup``
context with P = the persisted ``lastSuccessfulPollAt``. This gives requirement section 6
directly:

- **Matched** (the R4 "continue" row): same sessionId, same identity (R2: user name
  case-insensitive, domain and logon time when both known), Active, the same connection type
  (interpretation I3) and the same connect time when both are known. An approximate period
  (connect time null) cannot compare connect times, so it is matched by identity and
  connection type alone (REQ AC-19). The period continues and no event is written.
- **Unmatched** periods are closed through ``newEndEvent`` (G2, R11 clamps) under the blind
  rule (R7) with ``endCause`` from the state observed now: Disconnected ``disconnect``, gone,
  no user name (R1) or another identity ``logoff``, Active with another connect time or
  connection type ``reconnect_detected`` followed by a new start (I1, I4).
- **Active sessions with no matched period** start a period (R6), bounded by any end just
  written on the same sessionId (I4/N1).
- **Transitional** sessions with the identity of an open period keep it open and are seeded
  Active (R3), holding the blind context (P, ``detected_at_startup``) until their next
  definite state: then a gone, Disconnected, reconnected or replaced session is ended under
  the blind rule with that reason, and an Active one with the same connection just clears it
  (Critic TASK-008 M1, the startup counterpart of TASK-004 M2). Other transitional sessions
  are ignored.

Several open periods on one sessionId (an end lost in a crash, or an inconsistent
``status.json``, TASK-005 S10) are handled before that: all but the latest are closed first,
see ``Reconciler.closeSuperseded``.
"""

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from pc_handoff.detection.provider import ConnectionType, OsSession
from pc_handoff.domain.end_time import ObservedState
from pc_handoff.domain.models import (
    ActivityEvent,
    EventReason,
    EventType,
    SessionState,
    StatusSession,
    TrackedSession,
    UsagePeriod,
)
from pc_handoff.domain.tracker import (
    PollContext,
    PollOutcome,
    SessionTracker,
    filterUserSessions,
    namesEqual,
)


@dataclass(frozen=True)
class OpenPeriodRecord:
    """An open usage period rebuilt from disk at startup.

    - ``connectTime``: the connect time the period is matched on, the period's own: source 1
      the start event's ``connectTime``; source 2 ``usagePeriodStart`` for an exact period
      and null for an approximate one (Critic TASK-008 S1).
    - ``connectTimeAtStart``: the connect time the period's events carry (null for an
      approximate period). Equal to ``connectTime`` for both sources.
    - ``startEventTimestamp``: S, the logged ``session_start`` timestamp (source 1), or None
      when the start is in a backup or lost (source 2).
    """

    usagePeriodId: str
    sessionId: int
    userName: str
    domain: str | None
    logonTime: datetime | None
    connectTime: datetime | None
    connectionType: ConnectionType
    usagePeriodStart: datetime
    durationApproximate: bool
    startEventTimestamp: datetime | None
    clientName: str | None
    clientAddress: str | None
    connectTimeAtStart: datetime | None

    def toUsagePeriod(self) -> UsagePeriod:
        return UsagePeriod(
            usagePeriodId=self.usagePeriodId,
            usagePeriodStart=self.usagePeriodStart,
            durationApproximate=self.durationApproximate,
            startEventTimestamp=self.startEventTimestamp,
            connectTimeAtStart=self.connectTimeAtStart,
            connectionTypeAtStart=self.connectionType,
        )


# --- building records ---------------------------------------------------------------------


def recordFromStartEvent(event: ActivityEvent) -> OpenPeriodRecord:
    """Source 1: the period of a ``session_start`` with no ``session_end`` in the log.

    S is the event's own ``timestamp`` (G4), so the R11 clamp keeps the end after it.
    """
    return OpenPeriodRecord(
        usagePeriodId=event.usagePeriodId,
        sessionId=event.sessionId,
        userName=event.userName,
        domain=event.domain,
        logonTime=event.logonTime,
        connectTime=event.connectTime,
        connectionType=event.connectionType,
        usagePeriodStart=event.usagePeriodStart,
        durationApproximate=event.durationApproximate,
        startEventTimestamp=event.timestamp,
        clientName=event.clientName,
        clientAddress=event.clientAddress,
        connectTimeAtStart=event.connectTime,
    )


def openPeriodsFromEvents(events: Iterable[ActivityEvent]) -> list[OpenPeriodRecord]:
    """Source 1: every ``session_start`` whose ``usagePeriodId`` has no ``session_end``.

    The events come from a log that passed full validation, so each id has at most one start
    and one end. An end without a start (its start is in a backup) opens nothing.
    """
    eventList = list(events)
    ended = {e.usagePeriodId for e in eventList if e.eventType is EventType.SESSION_END}
    return [
        recordFromStartEvent(e)
        for e in eventList
        if e.eventType is EventType.SESSION_START and e.usagePeriodId not in ended
    ]


def recordFromStatusSession(entry: StatusSession) -> OpenPeriodRecord:
    """Source 2: the open period of an Active ``status.json`` entry.

    S is unknown (null). The period's connect time is not persisted in ``status.json``; it
    is rebuilt the way the tracker sets it: an exact period starts at its connect time, an
    approximate one has none. It is both what the events carry and what the period is
    matched on (Critic TASK-008 S1), exactly as for a source-1 record. The entry's own
    ``connectTime`` is the session's latest OS value: it can be null after a poll that got
    none (M3), or an older connection's time for an I1/I4 period, so it is not used.
    """
    if entry.usagePeriodId is None or entry.usagePeriodStart is None:
        raise ValueError(f"status entry has no open usage period (sessionId {entry.sessionId})")
    periodConnectTime = None if entry.durationApproximate else entry.usagePeriodStart
    return OpenPeriodRecord(
        usagePeriodId=entry.usagePeriodId,
        sessionId=entry.sessionId,
        userName=entry.userName,
        domain=entry.domain,
        logonTime=entry.logonTime,
        connectTime=periodConnectTime,
        connectionType=entry.connectionType,
        usagePeriodStart=entry.usagePeriodStart,
        durationApproximate=entry.durationApproximate,
        startEventTimestamp=None,
        clientName=entry.clientName,
        clientAddress=entry.clientAddress,
        connectTimeAtStart=periodConnectTime,
    )


def openPeriodsFromStatus(sessions: Iterable[StatusSession]) -> list[OpenPeriodRecord]:
    """Source 2: every entry with a non-null ``usagePeriodId`` (the first one per id)."""
    records: list[OpenPeriodRecord] = []
    seen: set[str] = set()
    for entry in sessions:
        if entry.usagePeriodId is None or entry.usagePeriodId in seen:
            continue
        seen.add(entry.usagePeriodId)
        records.append(recordFromStatusSession(entry))
    return records


def trackedFromStatusSession(entry: StatusSession) -> TrackedSession:
    """A ``status.json`` entry as a tracked session, for display before the first poll."""
    isActive = entry.state is SessionState.ACTIVE
    period = recordFromStatusSession(entry).toUsagePeriod() if isActive else None
    return TrackedSession(
        sessionId=entry.sessionId,
        userName=entry.userName,
        domain=entry.domain,
        state=entry.state,
        connectionType=entry.connectionType,
        clientName=entry.clientName,
        clientAddress=entry.clientAddress,
        logonTime=entry.logonTime,
        firstDetectedAt=entry.firstDetectedAt,
        connectTime=entry.connectTime,
        osDisconnectTime=None,
        disconnectedSince=None if isActive else entry.disconnectedSince,
        openPeriod=period,
        lastKnownConnectTime=entry.connectTime,
    )


# --- identity helpers -------------------------------------------------------------------


def identityMatches(
    userName: str,
    domain: str | None,
    logonTime: datetime | None,
    other: OpenPeriodRecord | StatusSession,
) -> bool:
    """R2 on persisted data: same user (case-insensitive), domain and logon time when known."""
    if not namesEqual(userName, other.userName):
        return False
    if domain is not None and other.domain is not None and not namesEqual(domain, other.domain):
        return False
    return logonTime is None or other.logonTime is None or logonTime == other.logonTime


def persistedFirstDetectedAt(
    statusSessions: Sequence[StatusSession],
    sessionId: int,
    userName: str,
    domain: str | None,
    logonTime: datetime | None,
) -> datetime | None:
    """``firstDetectedAt`` of the same identity in a usable ``status.json`` (R10), if any."""
    for entry in statusSessions:
        if (
            entry.sessionId == sessionId
            and entry.firstDetectedAt is not None
            and identityMatches(userName, domain, logonTime, entry)
        ):
            return entry.firstDetectedAt
    return None


def periodOrder(record: OpenPeriodRecord) -> tuple[datetime, datetime, str]:
    return (
        record.usagePeriodStart,
        record.startEventTimestamp or record.usagePeriodStart,
        record.usagePeriodId,
    )


def noteEnds(lastEnds: dict[int, datetime], sessionId: int, events: list[ActivityEvent]) -> None:
    """N1: keep the latest clamped ``periodEnd`` of the ends on ``sessionId``. An end whose
    ``periodEnd`` is unknown (nothing known, R7 step 3) bounds nothing and is not recorded."""
    for event in events:
        if event.eventType is not EventType.SESSION_END or event.periodEnd is None:
            continue
        current = lastEnds.get(sessionId)
        lastEnds[sessionId] = event.periodEnd if current is None else max(current, event.periodEnd)


# --- the reconciler -----------------------------------------------------------------------


class Reconciler:
    """Runs startup reconciliation once, at the first successful poll (REQ AC-27).

    Uses the tracker's logger, its I4 bound and its classification, so startup and runtime
    apply one rule set. The tracker is not otherwise modified.
    """

    def __init__(self, tracker: SessionTracker) -> None:
        self.tracker = tracker
        self.logger: logging.Logger = tracker.logger

    def reconcile(
        self,
        openPeriods: Iterable[OpenPeriodRecord],
        lastSuccessfulPollAt: datetime | None,
        statusSessions: Sequence[StatusSession],
        osSessions: Iterable[OsSession],
        detectionTime: datetime,
    ) -> PollOutcome:
        """Match ``openPeriods`` against the first successful snapshot taken at
        ``detectionTime``; return the seeded tracker state and the events in write order.

        ``lastSuccessfulPollAt`` is P from a usable ``status.json`` (None otherwise), and
        ``statusSessions`` its entries (empty otherwise), used for ``firstDetectedAt`` only.
        ``lastPeriodEndBySessionId`` is seeded with the clamped ``periodEnd`` of every period
        closed here (N1).
        """
        ctx = PollContext(
            detectionTime=detectionTime,
            reason=EventReason.DETECTED_AT_STARTUP,
            lastSuccessfulPollAt=lastSuccessfulPollAt,
        )
        bySession: dict[int, list[OpenPeriodRecord]] = {}
        seen: set[str] = set()
        for record in openPeriods:
            if record.usagePeriodId in seen:
                continue
            seen.add(record.usagePeriodId)
            bySession.setdefault(record.sessionId, []).append(record)

        snapshot = filterUserSessions(osSessions)
        sessions: dict[int, TrackedSession] = {}
        events: list[ActivityEvent] = []
        lastEnds: dict[int, datetime] = {}
        for sessionId in sorted(set(bySession) | set(snapshot)):
            records = sorted(bySession.get(sessionId, []), key=periodOrder)
            stepEvents = self.closeSuperseded(records, statusSessions, ctx)
            previous = self.preRestartSession(records[-1], statusSessions, ctx) if records else None
            noteEnds(lastEnds, sessionId, stepEvents)
            tracked, classified = self.tracker.classify(
                previous, snapshot.get(sessionId), ctx, lastEnds.get(sessionId)
            )
            noteEnds(lastEnds, sessionId, classified)
            events.extend(stepEvents + classified)
            if tracked is not None:
                sessions[sessionId] = self.withPersistedFirstDetectedAt(tracked, statusSessions)

        self.logSummary(seen, sessions, events)
        return PollOutcome(
            self.tracker.stateFromReconciled(sessions, detectionTime, lastEnds), events
        )

    def preRestartSession(
        self,
        record: OpenPeriodRecord,
        statusSessions: Sequence[StatusSession],
        ctx: PollContext,
    ) -> TrackedSession:
        """The Active tracked session the period belonged to before the restart.

        ``lastKnownConnectTime`` is seeded with the period's connect time, so a later change
        is still detected after a poll that reports none (M3). No blind context is seeded
        here: a session in a definite state at the first poll gets the blind rule for the
        downtime at startup, once. Only one reported transitional keeps it, through
        ``SessionTracker.keepTransitional`` with this poll's ``detected_at_startup`` context,
        so its eventual end still uses P and that reason (Critic TASK-008 M1).
        """
        firstDetectedAt = persistedFirstDetectedAt(
            statusSessions, record.sessionId, record.userName, record.domain, record.logonTime
        )
        return TrackedSession(
            sessionId=record.sessionId,
            userName=record.userName,
            domain=record.domain,
            state=SessionState.ACTIVE,
            connectionType=record.connectionType,
            clientName=record.clientName,
            clientAddress=record.clientAddress,
            logonTime=record.logonTime,
            firstDetectedAt=firstDetectedAt or ctx.detectionTime,
            connectTime=record.connectTime,
            osDisconnectTime=None,
            disconnectedSince=None,
            openPeriod=record.toUsagePeriod(),
            lastKnownConnectTime=record.connectTime,
        )

    def closeSuperseded(
        self,
        records: list[OpenPeriodRecord],
        statusSessions: Sequence[StatusSession],
        ctx: PollContext,
    ) -> list[ActivityEvent]:
        """Close every period of one sessionId except the latest, oldest first.

        Each was followed by a later period on the same sessionId, so it ended before that
        one began; what happened is known from its successor, not from the snapshot:

        - ``endCause``: ``reconnect_detected`` when the successor has the same identity,
          otherwise ``logoff`` (the sessionId was reused).
        - The window's upper bound is the successor's ``usagePeriodStart``. No OS disconnect
          time is used: it belongs to the session's latest disconnect.
        - P counts only when it is before the successor's start event (or that is unknown):
          a P after it is not a moment this period was known to be Active.
        """
        ended: list[ActivityEvent] = []
        for record, successor in zip(records, records[1:], strict=False):
            p = ctx.lastSuccessfulPollAt
            successorS = successor.startEventTimestamp
            if p is not None and successorS is not None and p >= successorS:
                p = None
            observed = (
                ObservedState.ACTIVE_RECONNECTED
                if identityMatches(record.userName, record.domain, record.logonTime, successor)
                else ObservedState.IDENTITY_REPLACED
            )
            ended.append(
                self.tracker.endEvent(
                    self.preRestartSession(record, statusSessions, ctx),
                    record.toUsagePeriod(),
                    observed,
                    None,
                    successor.usagePeriodStart,
                    replace(ctx, lastSuccessfulPollAt=p),
                )
            )
        return ended

    @staticmethod
    def withPersistedFirstDetectedAt(
        tracked: TrackedSession, statusSessions: Sequence[StatusSession]
    ) -> TrackedSession:
        """Carry ``firstDetectedAt`` from ``status.json`` for a known identity (R10)."""
        persisted = persistedFirstDetectedAt(
            statusSessions, tracked.sessionId, tracked.userName, tracked.domain, tracked.logonTime
        )
        if persisted is None or persisted == tracked.firstDetectedAt:
            return tracked
        return replace(tracked, firstDetectedAt=persisted)

    def logSummary(
        self,
        periodIds: set[str],
        sessions: dict[int, TrackedSession],
        events: list[ActivityEvent],
    ) -> None:
        continued = sum(
            1
            for s in sessions.values()
            if s.openPeriod is not None and s.openPeriod.usagePeriodId in periodIds
        )
        closed = sum(1 for e in events if e.eventType is EventType.SESSION_END)
        started = len(events) - closed
        self.logger.info(
            "startup reconciliation: %d open period(s): %d continued, %d closed; "
            "%d session_start(s)",
            len(periodIds),
            continued,
            closed,
            started,
        )
