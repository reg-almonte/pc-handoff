"""Session tracker: the pure snapshot-diff state machine of ADR-007 (TASK-004).

``SessionTracker`` turns each poll result into a new ``TrackerState`` and an ordered list of
``ActivityEvent`` objects. It does no I/O apart from app-log lines: persistence (TASK-006),
scheduling (TASK-009) and startup seeding (TASK-008) live elsewhere.

Per successful poll (ADR-007 R1-R11):

1. R1 filter: sessionId 0, ``Ignored`` and None/empty user names are dropped, i.e. treated as
   absent from the snapshot.
2. Each previously tracked session is classified by sessionId as gone (R5), identity
   replaced (R2), transitional (R3) or same identity (R4); unknown sessions are new (R6).
3. Every ``session_end`` is built by ``newEndEvent`` (R11 clamps), always from the session
   as it was **before** this poll's refresh, so the old period records the old client.
4. Events are ordered by ascending sessionId, end before start within a session.
"""

import logging
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime

from pc_handoff.clock import Clock
from pc_handoff.detection.provider import OsConnectState, OsSession
from pc_handoff.domain.end_time import (
    ObservedState,
    computeBlindEnd,
    computeDisconnectedSince,
    computeObservedEnd,
    deriveEndCause,
    reconnectWindowUpper,
)
from pc_handoff.domain.models import (
    ActivityEvent,
    EventReason,
    EventType,
    PcStatus,
    SessionState,
    TrackedSession,
    UsagePeriod,
    newEndEvent,
    newStartEvent,
)
from pc_handoff.domain.status import deriveStatus

TRANSITIONAL_WARN_POLLS = 12
SESSION_BOUND_NAME = "the end of the last period on that sessionId"

Step = tuple[TrackedSession | None, list[ActivityEvent]]


@dataclass(frozen=True)
class TrackerState:
    """In-memory tracker state between polls. ``sessions`` is never mutated after creation.

    ``lastKnownStatus`` is only ever set from a successful poll, so it is never ``Unknown``.

    ``lastPeriodEndBySessionId`` (memory only, never persisted) holds, per sessionId, the
    latest ``periodEnd`` of any period ended on it, whatever the identity. It bounds every new
    start on that sessionId (interpretation I4, Critic TASK-004 N1). It is empty before the
    first poll and after a restart, where startup reconciliation (TASK-008) may seed it.
    """

    sessions: dict[int, TrackedSession]
    lastSuccessfulPollAt: datetime | None
    lastKnownStatus: PcStatus | None
    lastKnownStatusAt: datetime | None
    status: PcStatus
    detectionHealthy: bool
    outagePending: bool
    lastPeriodEndBySessionId: dict[int, datetime] = field(default_factory=dict)


@dataclass(frozen=True)
class PollOutcome:
    """The state after one poll and the events it produced, in write order."""

    state: TrackerState
    events: list[ActivityEvent]


@dataclass(frozen=True)
class PollContext:
    detectionTime: datetime
    reason: EventReason
    lastSuccessfulPollAt: datetime | None


# --- R1 filter and R2 identity (public: reused by TASK-008 reconciliation) ----------------


def isUserSession(session: OsSession) -> bool:
    """R1: a user session has a non-zero id, is not ``Ignored`` and has a non-empty user."""
    return (
        session.sessionId != 0
        and session.rawState is not OsConnectState.IGNORED
        and bool(session.userName)
    )


def filterUserSessions(osSessions: Iterable[OsSession]) -> dict[int, OsSession]:
    """The user sessions of a snapshot by sessionId; everything else counts as absent."""
    return {s.sessionId: s for s in osSessions if isUserSession(s)}


def namesEqual(a: str, b: str) -> bool:
    return a.casefold() == b.casefold()


def sameIdentity(tracked: TrackedSession, session: OsSession) -> bool:
    """R2: same ``domain\\user`` (case-insensitive) and the same logon time when both known.

    The domain is compared only when both sides know it, so a transiently missing domain
    cannot turn into a false logoff; the logon time likewise (R10 fallback).
    """
    if session.userName is None or not namesEqual(tracked.userName, session.userName):
        return False
    if (
        tracked.domain is not None
        and session.domain is not None
        and not namesEqual(tracked.domain, session.domain)
    ):
        return False
    return not (
        tracked.logonTime is not None
        and session.logonTime is not None
        and tracked.logonTime != session.logonTime
    )


def lastKnownConnectTime(tracked: TrackedSession) -> datetime | None:
    """The last non-null connect time of ``tracked`` (Critic TASK-004 M3).

    Falls back to ``connectTime`` for a record built without the bookkeeping field.
    """
    if tracked.connectTime is not None:
        return tracked.connectTime
    return tracked.lastKnownConnectTime


def laterOf(a: datetime | None, b: datetime | None) -> datetime | None:
    """The later of two optional times (None when both are unknown)."""
    known = [t for t in (a, b) if t is not None]
    return max(known) if known else None


Bound = tuple[datetime | None, str]


class SessionTracker:
    """Pure poll-to-poll diffing of OS sessions into usage periods (ADR-007)."""

    def __init__(
        self,
        clock: Clock,
        logger: logging.Logger,
        transitionalWarnPolls: int = TRANSITIONAL_WARN_POLLS,
    ) -> None:
        self.clock = clock
        self.logger = logger
        self.transitionalWarnPolls = transitionalWarnPolls
        # (sessionId, firstDetectedAt) of identities already reported under R10. Log-only
        # rate limiting; it never affects states or events.
        self.loggedLogonFallbacks: set[tuple[int, datetime | None]] = set()

    # --- public API -----------------------------------------------------------------------

    def initialState(self) -> TrackerState:
        """The state before the first poll: no sessions, Unknown, unhealthy (REQ AC-27)."""
        return TrackerState(
            sessions={},
            lastSuccessfulPollAt=None,
            lastKnownStatus=None,
            lastKnownStatusAt=None,
            status=PcStatus.UNKNOWN,
            detectionHealthy=False,
            outagePending=False,
        )

    def stateFromReconciled(
        self,
        sessions: Mapping[int, TrackedSession],
        detectionTime: datetime,
        lastPeriodEndBySessionId: Mapping[int, datetime] | None = None,
    ) -> TrackerState:
        """A healthy state after a successful poll at ``detectionTime`` holding ``sessions``.

        Used by ``applySuccess`` and by startup reconciliation (TASK-008) to seed the tracker.
        ``lastPeriodEndBySessionId`` is copied; omitted, it starts empty.
        """
        ordered = {sid: sessions[sid] for sid in sorted(sessions)}
        status = deriveStatus(ordered.values())
        return TrackerState(
            sessions=ordered,
            lastSuccessfulPollAt=detectionTime,
            lastKnownStatus=status,
            lastKnownStatusAt=detectionTime,
            status=status,
            detectionHealthy=True,
            outagePending=False,
            lastPeriodEndBySessionId=dict(lastPeriodEndBySessionId or {}),
        )

    def applySuccess(
        self,
        state: TrackerState,
        osSessions: Iterable[OsSession],
        detectionTime: datetime | None = None,
    ) -> PollOutcome:
        """Apply a successful enumeration taken at ``detectionTime`` (default: the clock)."""
        now = self.clock.now() if detectionTime is None else detectionTime
        ctx = PollContext(
            detectionTime=now,
            reason=(
                EventReason.DETECTED_AFTER_OUTAGE if state.outagePending else EventReason.OBSERVED
            ),
            lastSuccessfulPollAt=state.lastSuccessfulPollAt,
        )
        snapshot = filterUserSessions(osSessions)
        sessions: dict[int, TrackedSession] = {}
        events: list[ActivityEvent] = []
        lastEnds = dict(state.lastPeriodEndBySessionId)
        for sessionId in sorted(set(state.sessions) | set(snapshot)):
            tracked, stepEvents = self.classify(
                state.sessions.get(sessionId), snapshot.get(sessionId), ctx, lastEnds.get(sessionId)
            )
            for event in stepEvents:
                if event.eventType is EventType.SESSION_END:
                    # The clamped periodEnd (newEndEvent); the value never moves back.
                    lastEnds[sessionId] = laterOf(lastEnds.get(sessionId), event.periodEnd)
            events.extend(stepEvents)
            if tracked is not None:
                sessions[sessionId] = tracked
        self.loggedLogonFallbacks &= {(s.sessionId, s.firstDetectedAt) for s in sessions.values()}
        return PollOutcome(self.stateFromReconciled(sessions, now, lastEnds), events)

    def applyFailure(
        self,
        state: TrackerState,
        error: BaseException,
        detectionTime: datetime | None = None,
    ) -> PollOutcome:
        """Apply a failed poll: Unknown, unhealthy, outage pending, everything else kept.

        No events and no log lines; the poller logs the failure with its traceId (TASK-009).
        """
        return PollOutcome(
            replace(state, status=PcStatus.UNKNOWN, detectionHealthy=False, outagePending=True),
            [],
        )

    # --- classification -------------------------------------------------------------------

    def classify(
        self,
        previous: TrackedSession | None,
        session: OsSession | None,
        ctx: PollContext,
        sessionLastEnd: datetime | None,
    ) -> Step:
        """Classify one sessionId. ``sessionLastEnd`` is the latest ``periodEnd`` on this
        sessionId from earlier polls (N1)."""
        if previous is None:
            # Unreachable by construction: applySuccess iterates over the union of the
            # tracked and the reported sessionIds, so at least one side is present.
            assert session is not None
            return self.newSession(session, ctx, sessionLastEnd)
        ctx = self.sessionContext(previous, ctx)
        if session is None:
            return None, self.endIfOpen(previous, ObservedState.GONE, None, ctx)
        if not sameIdentity(previous, session):
            ended = self.endIfOpen(previous, ObservedState.IDENTITY_REPLACED, None, ctx)
            if ended:
                # The new identity's start is also bounded by the end just emitted (N1).
                sessionLastEnd = laterOf(sessionLastEnd, ended[0].periodEnd)
            tracked, started = self.newSession(session, ctx, sessionLastEnd)
            return tracked, ended + started
        self.noteLogonFallback(previous, session)
        if session.rawState is OsConnectState.TRANSITIONAL:
            return self.keepTransitional(previous, session, ctx), []
        if session.rawState is OsConnectState.DISCONNECTED:
            return self.toDisconnected(previous, session, ctx)
        return self.toActive(previous, session, ctx, sessionLastEnd)

    @staticmethod
    def sessionContext(previous: TrackedSession, ctx: PollContext) -> PollContext:
        """Critic TASK-004 M2, generalized by Critic TASK-008 M1: a session that stayed
        transitional since a gap the tracker did not see (an outage, or the downtime before a
        restart) is still judged with that gap's reason and against its P: the last
        successful poll before the outage, or the persisted one before the restart."""
        if previous.blindReason is None:
            return ctx
        return replace(
            ctx,
            reason=previous.blindReason,
            lastSuccessfulPollAt=previous.outagePriorPollAt,
        )

    def newSession(
        self, session: OsSession, ctx: PollContext, sessionLastEnd: datetime | None
    ) -> Step:
        """R6: track an unknown Active or Disconnected session; ignore a transitional one.

        A connect time before the last period end on this sessionId (an earlier identity, or
        the same logon back after a "gone" blip) is stale: I4 extended by N1.
        """
        if session.rawState is OsConnectState.TRANSITIONAL:
            return None, []
        tracked = TrackedSession(
            sessionId=session.sessionId,
            userName=session.userName or "",
            domain=session.domain,
            state=SessionState.DISCONNECTED,
            connectionType=session.connectionType,
            clientName=session.clientName,
            clientAddress=session.clientAddress,
            logonTime=session.logonTime,
            firstDetectedAt=ctx.detectionTime,
            connectTime=session.connectTime,
            osDisconnectTime=session.disconnectTime,
            disconnectedSince=None,
            openPeriod=None,
            lastKnownConnectTime=session.connectTime,
        )
        if session.rawState is OsConnectState.DISCONNECTED:
            since = computeDisconnectedSince(
                session.disconnectTime, None, session.connectTime, ctx.detectionTime
            )
            return replace(tracked, disconnectedSince=since), []
        connectTime = self.plausibleConnectTime(
            session.sessionId, session.connectTime, [(sessionLastEnd, SESSION_BOUND_NAME)]
        )
        return self.startPeriod(tracked, connectTime, ctx)

    def keepTransitional(
        self, previous: TrackedSession, session: OsSession, ctx: PollContext
    ) -> TrackedSession:
        """R3: keep the previous record, count the poll, warn every N consecutive polls.

        In any context other than ``observed`` (after an outage, at startup, or still
        holding such a context) the session keeps that context, reason and P, until its next
        definite state (Critic TASK-004 M2, TASK-008 M1).
        """
        count = previous.consecutiveTransitionalPolls + 1
        if count % self.transitionalWarnPolls == 0:
            self.logger.warning(
                "session %d (%s) has reported transitional state %s for %d consecutive polls; "
                "keeping previous state %s",
                previous.sessionId,
                previous.userName,
                session.rawStateName,
                count,
                previous.state.value,
            )
        blind = ctx.reason is not EventReason.OBSERVED
        return replace(
            previous,
            consecutiveTransitionalPolls=count,
            outagePriorPollAt=ctx.lastSuccessfulPollAt if blind else None,
            blindReason=ctx.reason if blind else None,
        )

    def toDisconnected(
        self, previous: TrackedSession, session: OsSession, ctx: PollContext
    ) -> Step:
        """R4 Active -> Disconnected (end the period) or Disconnected -> Disconnected."""
        period = previous.openPeriod
        tracked = replace(
            self.refreshed(previous, session), state=SessionState.DISCONNECTED, openPeriod=None
        )
        if period is None:
            return tracked, []
        # R8 / I2: the observed window, clamped like periodEnd (R11) so that an observed
        # disconnect always has disconnectedSince == periodEnd.
        since = max(
            computeDisconnectedSince(
                session.disconnectTime, period, session.connectTime, ctx.detectionTime
            ),
            period.usagePeriodStart,
        )
        end = self.endEvent(
            previous,
            period,
            ObservedState.DISCONNECTED,
            session.disconnectTime,
            ctx.detectionTime,
            ctx,
        )
        return replace(tracked, disconnectedSince=since), [end]

    def toActive(
        self,
        previous: TrackedSession,
        session: OsSession,
        ctx: PollContext,
        sessionLastEnd: datetime | None,
    ) -> Step:
        """R4 Disconnected -> Active (start) or Active -> Active (continue or reconnect)."""
        refreshed = self.refreshed(previous, session)
        period = previous.openPeriod
        sessionBound: Bound = (sessionLastEnd, SESSION_BOUND_NAME)
        if period is None:
            # I4 / N5: the session was known to be Disconnected from disconnectedSince, so a
            # connect time before it belongs to an earlier connection; so does one before any
            # earlier period on this sessionId (N1). disconnectedSince is never earlier than
            # this identity's last periodEnd, which is also in the sessionId map.
            connectTime = self.plausibleConnectTime(
                previous.sessionId,
                session.connectTime,
                [sessionBound, (previous.disconnectedSince, "its disconnectedSince")],
            )
            return self.startPeriod(refreshed, connectTime, ctx)
        knownConnectTime = lastKnownConnectTime(previous)
        typeChanged = session.connectionType is not previous.connectionType
        connectChanged = (
            session.connectTime is not None
            and knownConnectTime is not None
            and session.connectTime != knownConnectTime
        )
        if not (typeChanged or connectChanged):
            return replace(refreshed, openPeriod=period), []
        # I1 (Critic TASK-003 S6): an unchanged or missing connect time does not describe the
        # new connection, so it is "not reported" for both the window and the new start.
        newConnectTime = session.connectTime if connectChanged else None
        if newConnectTime is not None:
            # I4: nor does a new connect time earlier than the end the old period would get
            # with that time as its window bound; the window then falls back to [C1, R].
            newConnectTime = self.plausibleConnectTime(
                previous.sessionId,
                newConnectTime,
                [
                    (
                        self.clampedBlindEnd(period, session.disconnectTime, newConnectTime, ctx),
                        "the end of its previous period",
                    ),
                    sessionBound,
                ],
            )
        end = self.endEvent(
            previous,
            period,
            ObservedState.ACTIVE_RECONNECTED,
            session.disconnectTime,
            reconnectWindowUpper(newConnectTime, ctx.detectionTime),
            ctx,
        )
        tracked, started = self.startPeriod(refreshed, newConnectTime, ctx)
        return tracked, [end, *started]

    def plausibleConnectTime(
        self, sessionId: int, connectTime: datetime | None, bounds: list[Bound]
    ) -> datetime | None:
        """I4 (Critic TASK-004 M1, N1): ``connectTime``, or None when it is earlier than the
        latest known bound in ``bounds`` (a list of (time, name); unknown times are skipped,
        and the first one wins a tie, which names it in the WARNING).

        Such a connect time belongs to an earlier connection; using it would overlap the
        previous period and count the time in between. It is treated as "not reported", so
        the new period starts at the detection time and is approximate (as under I1).
        """
        known = [(time, name) for time, name in bounds if time is not None]
        if connectTime is None or not known:
            return connectTime
        bound, boundName = max(known, key=lambda item: item[0])
        if connectTime >= bound:
            return connectTime
        self.logger.warning(
            "connect time %s of session %d is earlier than %s %s; treated as not reported (I4)",
            connectTime.isoformat(),
            sessionId,
            boundName,
            bound.isoformat(),
        )
        return None

    # --- periods and events ---------------------------------------------------------------

    def refreshed(self, previous: TrackedSession, session: OsSession) -> TrackedSession:
        """R4: the mutable fields from this poll; identity fields are kept.

        A definite state also ends any blind context (M2, M1), and a null connect time keeps
        the last known one as the comparison basis (M3).

        This is the single place where a policy such as "keep the previous connectionType
        for a Disconnected session" (Critic TASK-002 S2, undecided) would go.
        """
        return replace(
            previous,
            clientName=session.clientName,
            clientAddress=session.clientAddress,
            connectionType=session.connectionType,
            connectTime=session.connectTime,
            osDisconnectTime=session.disconnectTime,
            consecutiveTransitionalPolls=0,
            lastKnownConnectTime=(
                lastKnownConnectTime(previous)
                if session.connectTime is None
                else session.connectTime
            ),
            outagePriorPollAt=None,
            blindReason=None,
        )

    def startPeriod(
        self, session: TrackedSession, connectTime: datetime | None, ctx: PollContext
    ) -> Step:
        """Open a new period: start at ``connectTime`` if known, else the detection time.

        ``startEventTimestamp`` and the start event's ``timestamp`` are the same value (G4).
        """
        period = UsagePeriod(
            usagePeriodId=str(uuid.uuid4()),
            usagePeriodStart=ctx.detectionTime if connectTime is None else connectTime,
            durationApproximate=connectTime is None,
            startEventTimestamp=ctx.detectionTime,
            connectTimeAtStart=connectTime,
            connectionTypeAtStart=session.connectionType,
        )
        tracked = replace(
            session, state=SessionState.ACTIVE, openPeriod=period, disconnectedSince=None
        )
        return tracked, [newStartEvent(tracked, period, ctx.reason, ctx.detectionTime)]

    def endIfOpen(
        self,
        previous: TrackedSession,
        observed: ObservedState,
        osDisconnectTime: datetime | None,
        ctx: PollContext,
    ) -> list[ActivityEvent]:
        """End ``previous``'s open period, if any (a Disconnected session has none: R5)."""
        if previous.openPeriod is None:
            return []
        return [
            self.endEvent(
                previous,
                previous.openPeriod,
                observed,
                osDisconnectTime,
                ctx.detectionTime,
                ctx,
            )
        ]

    def endEvent(
        self,
        previous: TrackedSession,
        period: UsagePeriod,
        observed: ObservedState,
        osDisconnectTime: datetime | None,
        windowUpper: datetime,
        ctx: PollContext,
    ) -> ActivityEvent:
        """Build a ``session_end`` through ``newEndEvent`` (G2) from the pre-refresh session.

        A reconnect, and any end after an outage, uses the blind rule (R7) and is approximate;
        any other end is observed (R4/R5) and copies the period's ``durationApproximate``.
        """
        if ctx.reason is not EventReason.OBSERVED or observed is ObservedState.ACTIVE_RECONNECTED:
            periodEnd = self.blindEnd(period, osDisconnectTime, windowUpper, ctx)
            approximate = True
        else:
            periodEnd = computeObservedEnd(period, osDisconnectTime, ctx.detectionTime)
            approximate = period.durationApproximate
        return newEndEvent(
            previous,
            period,
            periodEnd,
            deriveEndCause(observed),
            ctx.reason,
            approximate,
            ctx.detectionTime,
            self.logger,
        )

    @staticmethod
    def blindEnd(
        period: UsagePeriod,
        osDisconnectTime: datetime | None,
        windowUpper: datetime,
        ctx: PollContext,
    ) -> datetime | None:
        """R7 for ``period`` in this poll's (or this session's outage) context."""
        return computeBlindEnd(
            period,
            osDisconnectTime,
            windowUpper,
            ctx.lastSuccessfulPollAt,
            period.startEventTimestamp,
        )

    def clampedBlindEnd(
        self,
        period: UsagePeriod,
        osDisconnectTime: datetime | None,
        windowUpper: datetime,
        ctx: PollContext,
    ) -> datetime | None:
        """The ``periodEnd`` that ``newEndEvent`` would record for ``blindEnd`` (R11 clamp)."""
        periodEnd = self.blindEnd(period, osDisconnectTime, windowUpper, ctx)
        return None if periodEnd is None else max(periodEnd, period.usagePeriodStart)

    def noteLogonFallback(self, previous: TrackedSession, session: OsSession) -> None:
        """R10: log once per tracked identity when it was matched without a logon time."""
        if previous.logonTime is not None and session.logonTime is not None:
            return
        key = (previous.sessionId, previous.firstDetectedAt)
        if key in self.loggedLogonFallbacks:
            return
        self.loggedLogonFallbacks.add(key)
        self.logger.info("identity matched without logonTime for session %d", previous.sessionId)
