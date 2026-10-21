"""SessionTracker failure and recovery (TASK-004 AC-8, AC-9; Critic TASK-003 G1)."""

from datetime import timedelta

import pytest

from pc_handoff.detection.provider import SessionEnumerationError
from pc_handoff.domain.models import EndCause, EventReason, EventType, PcStatus, SessionState
from tests.fakes.activity_checks import activityDocumentProblems
from tests.fakes.builders import osSession, trackedActive, trackerState

ERRORS = [
    SessionEnumerationError("timeout", "enumeration timed out"),
    SessionEnumerationError("access_denied", "denied", winerror=5),
    RuntimeError("unexpected"),
]


class TestAc08FailedPoll:
    @pytest.mark.parametrize("error", ERRORS, ids=["timeout", "access-denied", "other"])
    def test_failure_keeps_last_known_data(self, tracker, fakeClock, error, capturedAppLog):
        state = tracker.applySuccess(tracker.initialState(), [osSession(2, "alice")]).state
        p = fakeClock.now()
        fakeClock.advance(5)

        outcome = tracker.applyFailure(state, error)

        assert outcome.events == []
        assert outcome.state.status is PcStatus.UNKNOWN
        assert outcome.state.detectionHealthy is False
        assert outcome.state.outagePending is True
        assert outcome.state.lastKnownStatus is PcStatus.IN_USE
        assert outcome.state.lastKnownStatusAt == p
        assert outcome.state.lastSuccessfulPollAt == p
        assert outcome.state.sessions == state.sessions
        assert capturedAppLog() == []

    def test_repeated_failures_keep_last_known_data(self, tracker, fakeClock):
        state = tracker.applySuccess(tracker.initialState(), []).state
        for _ in range(3):
            fakeClock.advance(5)
            state = tracker.applyFailure(state, ERRORS[0], fakeClock.now()).state
        assert state.lastKnownStatus is PcStatus.AVAILABLE
        assert state.status is PcStatus.UNKNOWN

    def test_failure_on_initial_state_leaves_nulls(self, tracker):
        outcome = tracker.applyFailure(tracker.initialState(), ERRORS[0])
        assert outcome.events == []
        state = outcome.state
        assert state.lastKnownStatus is None
        assert state.lastKnownStatusAt is None
        assert state.lastSuccessfulPollAt is None
        assert state.sessions == {}
        assert state.status is PcStatus.UNKNOWN
        assert state.detectionHealthy is False

    def test_g1_last_known_status_is_never_unknown(self, tracker, fakeClock):
        """Critic TASK-003 G1, deterministic part (the random test covers sequences)."""
        state = tracker.initialState()
        script = [None, [osSession(2, "alice")], None, None, [], None, [osSession(3, "bob")]]
        for snapshot in script:
            fakeClock.advance(5)
            if snapshot is None:
                state = tracker.applyFailure(state, ERRORS[0]).state
            else:
                state = tracker.applySuccess(state, snapshot).state
            assert state.lastKnownStatus is not PcStatus.UNKNOWN


class TestAc09RecoveryAfterOutage:
    """Outage state with P; alice's period started at C1 with start-event timestamp S."""

    def outageState(self, fakeClock, *, sAfterP: bool):
        base = fakeClock.now()
        c1 = base - timedelta(minutes=30)
        p = base - timedelta(minutes=2)
        s = p + timedelta(seconds=30) if sAfterP else p - timedelta(minutes=10)
        alice = trackedActive(2, "alice", connectTime=c1, startEventTimestamp=s)
        return c1, p, s, alice, trackerState([alice], p, outagePending=True)

    def checkCommon(self, outcome, fakeClock, c1):
        for end in (e for e in outcome.events if e.eventType is EventType.SESSION_END):
            assert end.durationSeconds == int((end.periodEnd - c1).total_seconds())
            assert end.durationSeconds >= 0
        assert outcome.state.detectionHealthy is True
        assert outcome.state.outagePending is False
        assert outcome.state.lastSuccessfulPollAt == fakeClock.now()
        assert outcome.state.status is not PcStatus.UNKNOWN

    def test_a_still_active_same_connect_time(self, tracker, fakeClock):
        c1, _, _, alice, state = self.outageState(fakeClock, sAfterP=False)
        outcome = tracker.applySuccess(state, [osSession(2, "alice", connectTime=c1)])
        assert outcome.events == []
        assert outcome.state.sessions[2].openPeriod == alice.openPeriod
        assert outcome.state.status is PcStatus.IN_USE
        self.checkCommon(outcome, fakeClock, c1)

    @pytest.mark.parametrize("sAfterP", [False, True], ids=["P-later", "S-later"])
    def test_b_disconnected_without_os_time(self, tracker, fakeClock, sAfterP):
        c1, p, s, alice, state = self.outageState(fakeClock, sAfterP=sAfterP)
        outcome = tracker.applySuccess(
            state, [osSession(2, "alice", "Disconnected", connectTime=c1)]
        )
        (end,) = outcome.events
        assert end.usagePeriodId == alice.openPeriod.usagePeriodId
        assert end.endCause is EndCause.DISCONNECT
        assert end.reason is EventReason.DETECTED_AFTER_OUTAGE
        assert end.durationApproximate is True
        assert end.periodEnd == max(p, s)
        assert end.timestamp == fakeClock.now()
        tracked = outcome.state.sessions[2]
        assert tracked.state is SessionState.DISCONNECTED
        # R8: disconnectedSince uses the observed window and fallback, not the blind rule.
        assert tracked.disconnectedSince == fakeClock.now()
        self.checkCommon(outcome, fakeClock, c1)

    def test_b_disconnected_with_os_time_in_window(self, tracker, fakeClock):
        c1, _, _, _, state = self.outageState(fakeClock, sAfterP=False)
        d = fakeClock.now() - timedelta(seconds=40)
        outcome = tracker.applySuccess(
            state, [osSession(2, "alice", "Disconnected", connectTime=c1, disconnectTime=d)]
        )
        assert outcome.events[0].periodEnd == d
        assert outcome.state.sessions[2].disconnectedSince == d
        self.checkCommon(outcome, fakeClock, c1)

    @pytest.mark.parametrize("sAfterP", [False, True], ids=["P-later", "S-later"])
    def test_c_absent(self, tracker, fakeClock, sAfterP):
        c1, p, s, _, state = self.outageState(fakeClock, sAfterP=sAfterP)
        outcome = tracker.applySuccess(state, [])
        (end,) = outcome.events
        assert end.endCause is EndCause.LOGOFF
        assert end.reason is EventReason.DETECTED_AFTER_OUTAGE
        assert end.durationApproximate is True
        assert end.periodEnd == max(p, s)
        assert outcome.state.sessions == {}
        assert outcome.state.status is PcStatus.AVAILABLE
        self.checkCommon(outcome, fakeClock, c1)

    def test_d_reconnected_with_disconnect_time_in_window(self, tracker, fakeClock):
        c1, _, _, alice, state = self.outageState(fakeClock, sAfterP=False)
        c2 = fakeClock.now() - timedelta(seconds=20)
        d = c2 - timedelta(seconds=30)
        outcome = tracker.applySuccess(
            state, [osSession(2, "alice", connectTime=c2, disconnectTime=d)]
        )
        end, start = outcome.events
        assert end.eventType is EventType.SESSION_END
        assert end.endCause is EndCause.RECONNECT_DETECTED
        assert end.reason is EventReason.DETECTED_AFTER_OUTAGE
        assert end.durationApproximate is True
        assert end.periodEnd == d
        assert start.eventType is EventType.SESSION_START
        assert start.usagePeriodId != alice.openPeriod.usagePeriodId
        assert start.usagePeriodStart == c2
        assert start.reason is EventReason.DETECTED_AFTER_OUTAGE
        assert outcome.state.status is PcStatus.IN_USE
        self.checkCommon(outcome, fakeClock, c1)

    def test_new_session_after_outage_starts_with_outage_reason(self, tracker, fakeClock):
        _, _, _, _, state = self.outageState(fakeClock, sAfterP=False)
        outcome = tracker.applySuccess(state, [osSession(3, "bob")])
        assert [(e.eventType, e.sessionId, e.reason) for e in outcome.events] == [
            (EventType.SESSION_END, 2, EventReason.DETECTED_AFTER_OUTAGE),
            (EventType.SESSION_START, 3, EventReason.DETECTED_AFTER_OUTAGE),
        ]
        assert activityDocumentProblems(outcome.events) == []

    def test_second_success_after_recovery_is_observed_again(self, tracker, fakeClock):
        c1, _, _, _, state = self.outageState(fakeClock, sAfterP=False)
        state = tracker.applySuccess(state, [osSession(2, "alice", connectTime=c1)]).state
        fakeClock.advance(5)
        outcome = tracker.applySuccess(state, [])
        assert outcome.events[0].reason is EventReason.OBSERVED
        assert outcome.events[0].periodEnd == fakeClock.now()

    def test_failure_then_recovery_through_the_tracker(self, tracker, fakeClock):
        c1 = fakeClock.now() - timedelta(minutes=10)
        state = tracker.applySuccess(
            tracker.initialState(), [osSession(2, "alice", connectTime=c1)]
        ).state
        p = fakeClock.now()
        for _ in range(4):
            fakeClock.advance(5)
            state = tracker.applyFailure(state, ERRORS[0]).state
        fakeClock.advance(5)
        outcome = tracker.applySuccess(state, [])
        (end,) = outcome.events
        assert end.reason is EventReason.DETECTED_AFTER_OUTAGE
        assert end.periodEnd == p  # max(P, S) with P == S here
        assert end.durationSeconds == int((p - c1).total_seconds())


class TestM2OutageContextKeptWhileTransitional:
    """Critic TASK-004 M2: a session that is transitional on the recovery poll keeps the
    outage context until its next definite state (Active, Disconnected or gone)."""

    def outageThenTransitional(self, tracker, fakeClock, *, transitionalPolls=1):
        c1 = fakeClock.now() - timedelta(minutes=30)
        state = tracker.applySuccess(
            tracker.initialState(), [osSession(2, "alice", connectTime=c1)]
        ).state
        p = fakeClock.now()
        for _ in range(120):  # 10 minutes of failed polls
            fakeClock.advance(5)
            state = tracker.applyFailure(state, ERRORS[0]).state
        for _ in range(transitionalPolls):
            fakeClock.advance(5)
            outcome = tracker.applySuccess(
                state, [osSession(2, "alice", "ConnectQuery", connectTime=c1)]
            )
            assert outcome.events == []
            assert outcome.state.detectionHealthy is True
            assert outcome.state.outagePending is False
            assert outcome.state.sessions[2].state is SessionState.ACTIVE
            state = outcome.state
        fakeClock.advance(5)
        return c1, p, state

    def test_critic_probe_then_absent_is_blind_logoff(self, tracker, fakeClock):
        c1, p, state = self.outageThenTransitional(tracker, fakeClock)
        assert p.isoformat() == "2026-10-21T09:00:00+09:00"

        outcome = tracker.applySuccess(state, [])

        (end,) = outcome.events
        assert end.endCause is EndCause.LOGOFF
        assert end.reason is EventReason.DETECTED_AFTER_OUTAGE
        assert end.durationApproximate is True
        assert end.periodEnd == p
        assert end.durationSeconds == int((p - c1).total_seconds())
        assert end.timestamp == fakeClock.now()

    def test_several_transitional_polls_then_disconnected(self, tracker, fakeClock):
        _, p, state = self.outageThenTransitional(tracker, fakeClock, transitionalPolls=3)
        outcome = tracker.applySuccess(state, [osSession(2, "alice", "Disconnected")])
        (end,) = outcome.events
        assert end.endCause is EndCause.DISCONNECT
        assert end.reason is EventReason.DETECTED_AFTER_OUTAGE
        assert end.periodEnd == p
        assert outcome.state.sessions[2].disconnectedSince == fakeClock.now()

    def test_then_reconnected_ends_and_starts_after_outage(self, tracker, fakeClock):
        _, p, state = self.outageThenTransitional(tracker, fakeClock)
        c2 = fakeClock.now() - timedelta(seconds=2)
        outcome = tracker.applySuccess(state, [osSession(2, "alice", connectTime=c2)])
        end, start = outcome.events
        assert end.endCause is EndCause.RECONNECT_DETECTED
        assert end.reason is EventReason.DETECTED_AFTER_OUTAGE
        assert end.periodEnd == p
        assert start.reason is EventReason.DETECTED_AFTER_OUTAGE
        assert start.usagePeriodStart == c2

    def test_then_identity_replaced_uses_outage_context(self, tracker, fakeClock):
        _, p, state = self.outageThenTransitional(tracker, fakeClock)
        outcome = tracker.applySuccess(state, [osSession(2, "carol")])
        end, start = outcome.events
        assert (end.endCause, end.reason, end.periodEnd) == (
            EndCause.LOGOFF,
            EventReason.DETECTED_AFTER_OUTAGE,
            p,
        )
        assert start.reason is EventReason.DETECTED_AFTER_OUTAGE

    def test_then_same_connection_active_clears_the_context(self, tracker, fakeClock):
        c1, _, state = self.outageThenTransitional(tracker, fakeClock)
        state = tracker.applySuccess(state, [osSession(2, "alice", connectTime=c1)])
        assert state.events == []
        fakeClock.advance(5)
        outcome = tracker.applySuccess(state.state, [])
        (end,) = outcome.events
        assert end.reason is EventReason.OBSERVED
        assert end.periodEnd == fakeClock.now()
        assert end.durationApproximate is False

    def test_second_outage_keeps_the_original_last_poll(self, tracker, fakeClock):
        _, p, state = self.outageThenTransitional(tracker, fakeClock)
        for _ in range(3):
            fakeClock.advance(5)
            state = tracker.applyFailure(state, ERRORS[0]).state
        fakeClock.advance(5)
        state = tracker.applySuccess(state, [osSession(2, "alice", "Shadow")]).state
        fakeClock.advance(5)
        (end,) = tracker.applySuccess(state, []).events
        assert end.reason is EventReason.DETECTED_AFTER_OUTAGE
        assert end.periodEnd == p

    def test_disconnected_session_transitional_then_active_starts_after_outage(
        self, tracker, fakeClock
    ):
        state = tracker.applySuccess(
            tracker.initialState(), [osSession(2, "alice", "Disconnected")]
        ).state
        fakeClock.advance(5)
        state = tracker.applyFailure(state, ERRORS[0]).state
        fakeClock.advance(5)
        state = tracker.applySuccess(state, [osSession(2, "alice", "Init")]).state
        fakeClock.advance(5)
        c2 = fakeClock.now() - timedelta(seconds=1)
        (start,) = tracker.applySuccess(state, [osSession(2, "alice", connectTime=c2)]).events
        assert start.reason is EventReason.DETECTED_AFTER_OUTAGE
        assert start.usagePeriodStart == c2

    def test_other_sessions_on_the_next_poll_are_observed(self, tracker, fakeClock):
        _, _, state = self.outageThenTransitional(tracker, fakeClock)
        outcome = tracker.applySuccess(
            state, [osSession(2, "alice", "Shadow"), osSession(3, "bob")]
        )
        (start,) = outcome.events
        assert start.sessionId == 3
        assert start.reason is EventReason.OBSERVED


class TestN5DisconnectedSinceBoundsTheNextStart:
    """Critic TASK-004 N5: after an outage, a disconnect's periodEnd (blind, max(P, S)) is
    earlier than its disconnectedSince (the detection time). A connect time between the two
    belongs to a connection made during the outage, before the session was seen
    Disconnected, so it cannot describe the next connection."""

    def test_connect_time_made_during_the_outage_is_not_reported(
        self, tracker, fakeClock, capturedAppLog
    ):
        c1 = fakeClock.now() - timedelta(minutes=30)
        state = tracker.applySuccess(
            tracker.initialState(), [osSession(2, "alice", connectTime=c1)]
        ).state
        p = fakeClock.now()
        assert p.isoformat() == "2026-10-21T09:00:00+09:00"
        for _ in range(60):  # outage 09:00:05 to 09:05:00
            fakeClock.advance(5)
            state = tracker.applyFailure(state, ERRORS[0]).state
        fakeClock.advance(5)
        recovery = tracker.applySuccess(
            state, [osSession(2, "alice", "Disconnected", connectTime=c1)]
        )
        (end,) = recovery.events
        r = fakeClock.now()
        assert r.isoformat() == "2026-10-21T09:05:05+09:00"
        assert end.periodEnd == p
        assert recovery.state.sessions[2].disconnectedSince == r

        fakeClock.advance(5)
        duringOutage = p + timedelta(minutes=2)  # 09:02:00
        outcome = tracker.applySuccess(
            recovery.state, [osSession(2, "alice", connectTime=duringOutage)]
        )

        (start,) = outcome.events
        assert start.usagePeriodStart == fakeClock.now()
        assert start.durationApproximate is True
        assert start.connectTime is None
        assert outcome.state.sessions[2].openPeriod.connectTimeAtStart is None
        stale = [
            line["message"]
            for line in capturedAppLog()
            if line["level"] == "WARNING" and "treated as not reported (I4)" in line["message"]
        ]
        assert stale == [
            f"connect time {duringOutage.isoformat()} of session 2 is earlier than its "
            f"disconnectedSince {r.isoformat()}; treated as not reported (I4)"
        ]

    def test_connect_time_after_disconnected_since_is_used(self, tracker, fakeClock):
        state = tracker.applySuccess(tracker.initialState(), [osSession(2, "alice")]).state
        fakeClock.advance(5)
        state = tracker.applyFailure(state, ERRORS[0]).state
        fakeClock.advance(5)
        state = tracker.applySuccess(state, [osSession(2, "alice", "Disconnected")]).state
        fresh = state.sessions[2].disconnectedSince + timedelta(seconds=1)
        fakeClock.advance(5)
        (start,) = tracker.applySuccess(state, [osSession(2, "alice", connectTime=fresh)]).events
        assert start.usagePeriodStart == fresh
        assert start.durationApproximate is False
