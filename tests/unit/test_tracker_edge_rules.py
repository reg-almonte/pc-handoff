"""SessionTracker edge rules (TASK-004 ACs 11, 14-17, 19-21; ADR-007 R1-R4, R10, R11)."""

import logging
from dataclasses import replace
from datetime import timedelta

import pytest

from pc_handoff.detection.provider import ConnectionType
from pc_handoff.domain.models import EndCause, EventReason, EventType, PcStatus, SessionState
from pc_handoff.domain.tracker import SessionTracker, filterUserSessions, sameIdentity
from tests.fakes.activity_checks import activityDocumentProblems
from tests.fakes.builders import (
    osSession,
    trackedActive,
    trackedDisconnected,
    trackerState,
)

POLL = 5


def poll(tracker, fakeClock, state, sessions):
    fakeClock.advance(POLL)
    return tracker.applySuccess(state, sessions)


def warnings(lines):
    return [line for line in lines if line["level"] == "WARNING"]


class ActiveAlice:
    """alice Active on sessionId 2: period start C1, start-event timestamp S, previous poll P."""

    def __init__(self, fakeClock, *, connectionType=ConnectionType.RDP, outage=False, logon=None):
        now = fakeClock.now()
        self.c1 = now - timedelta(minutes=30)
        self.s = now - timedelta(minutes=20)
        self.p = now - timedelta(seconds=POLL)
        kwargs = {} if logon is None else {"logonTime": logon}
        self.tracked = trackedActive(
            2,
            "alice",
            connectTime=self.c1,
            startEventTimestamp=self.s,
            connectionType=connectionType,
            **kwargs,
        )
        self.state = trackerState([self.tracked], self.p, outagePending=outage)
        self.periodId = self.tracked.openPeriod.usagePeriodId


class TestAc11FastReconnect:
    def test_disconnect_time_outside_window_falls_back_to_p_or_s(self, tracker, fakeClock):
        alice = ActiveAlice(fakeClock)
        c2 = fakeClock.now() - timedelta(seconds=2)
        outside = alice.c1 - timedelta(minutes=5)
        outcome = tracker.applySuccess(
            alice.state, [osSession(2, "alice", connectTime=c2, disconnectTime=outside)]
        )
        end, start = outcome.events
        assert end.eventType is EventType.SESSION_END
        assert end.usagePeriodId == alice.periodId
        assert end.endCause is EndCause.RECONNECT_DETECTED
        assert end.reason is EventReason.OBSERVED
        assert end.durationApproximate is True
        assert end.periodEnd == max(alice.p, alice.s)
        assert start.eventType is EventType.SESSION_START
        assert start.usagePeriodId != alice.periodId
        assert start.usagePeriodStart == c2
        assert start.durationApproximate is False
        assert start.reason is EventReason.OBSERVED
        assert outcome.state.status is PcStatus.IN_USE
        assert outcome.state.sessions[2].openPeriod.usagePeriodId == start.usagePeriodId

    def test_disconnect_time_after_new_connect_time_is_outside(self, tracker, fakeClock):
        alice = ActiveAlice(fakeClock)
        c2 = fakeClock.now() - timedelta(seconds=3)  # after P (I4)
        outcome = tracker.applySuccess(
            alice.state,
            [osSession(2, "alice", connectTime=c2, disconnectTime=c2 + timedelta(seconds=2))],
        )
        assert outcome.events[0].periodEnd == max(alice.p, alice.s)

    def test_disconnect_time_inside_window_is_used(self, tracker, fakeClock):
        alice = ActiveAlice(fakeClock)
        c2 = fakeClock.now() - timedelta(seconds=2)
        d = c2 - timedelta(minutes=1)
        outcome = tracker.applySuccess(
            alice.state, [osSession(2, "alice", connectTime=c2, disconnectTime=d)]
        )
        assert outcome.events[0].periodEnd == d
        assert outcome.events[0].durationSeconds == int((d - alice.c1).total_seconds())


class TestAc14TypeChangeReconnect:
    @pytest.mark.parametrize("reported", ["unchanged", "null"])
    def test_type_change_with_unchanged_or_null_connect_time(self, tracker, fakeClock, reported):
        alice = ActiveAlice(fakeClock)
        r = fakeClock.now()
        connect = alice.c1 if reported == "unchanged" else None
        outcome = tracker.applySuccess(
            alice.state, [osSession(2, "alice", "Active", "Console", connectTime=connect)]
        )
        end, start = outcome.events
        assert end.endCause is EndCause.RECONNECT_DETECTED
        assert end.reason is EventReason.OBSERVED
        assert end.durationApproximate is True
        assert end.periodEnd == max(alice.p, alice.s)
        assert end.connectionType is ConnectionType.RDP
        assert start.usagePeriodId != alice.periodId
        assert start.connectionType is ConnectionType.CONSOLE
        assert start.usagePeriodStart == r
        assert start.durationApproximate is True
        assert outcome.state.sessions[2].connectionType is ConnectionType.CONSOLE
        # Interpretation #3 (Critic TASK-004 S2): the I1 period records no connect time.
        assert start.connectTime is None
        assert outcome.state.sessions[2].openPeriod.connectTimeAtStart is None
        assert outcome.state.sessions[2].connectTime == connect

    @pytest.mark.parametrize("reported", ["unchanged", "null"])
    def test_s6_window_upper_is_detection_time_not_old_connect_time(
        self, tracker, fakeClock, reported
    ):
        """Critic TASK-003 S6: with the I1 window [C1, R], a time after C1 is accepted.

        Had C1 been passed as the new connect time, the window would be [C1, C1] and D
        would be rejected.
        """
        alice = ActiveAlice(fakeClock)
        d = alice.c1 + timedelta(minutes=5)
        connect = alice.c1 if reported == "unchanged" else None
        outcome = tracker.applySuccess(
            alice.state,
            [osSession(2, "alice", "Active", "Console", connectTime=connect, disconnectTime=d)],
        )
        assert outcome.events[0].periodEnd == d

    def test_disconnect_time_after_detection_is_rejected(self, tracker, fakeClock):
        alice = ActiveAlice(fakeClock)
        d = fakeClock.now() + timedelta(seconds=1)
        outcome = tracker.applySuccess(
            alice.state,
            [osSession(2, "alice", "Active", "Console", connectTime=None, disconnectTime=d)],
        )
        assert outcome.events[0].periodEnd == max(alice.p, alice.s)

    def test_type_change_with_new_connect_time_uses_it(self, tracker, fakeClock):
        alice = ActiveAlice(fakeClock)
        c2 = fakeClock.now() - timedelta(seconds=3)
        d = c2 - timedelta(seconds=10)
        outcome = tracker.applySuccess(
            alice.state,
            [osSession(2, "alice", "Active", "Console", connectTime=c2, disconnectTime=d)],
        )
        end, start = outcome.events
        assert end.periodEnd == d
        assert start.usagePeriodStart == c2
        assert start.durationApproximate is False
        assert start.connectionType is ConnectionType.CONSOLE

    def test_type_change_window_upper_is_new_connect_time(self, tracker, fakeClock):
        alice = ActiveAlice(fakeClock)
        c2 = fakeClock.now() - timedelta(seconds=3)  # after P (I4)
        outcome = tracker.applySuccess(
            alice.state,
            [
                osSession(
                    2,
                    "alice",
                    "Active",
                    "Console",
                    connectTime=c2,
                    disconnectTime=c2 + timedelta(seconds=2),
                )
            ],
        )
        assert outcome.events[0].periodEnd == max(alice.p, alice.s)

    def test_console_to_rdp_is_also_a_reconnect(self, tracker, fakeClock):
        alice = ActiveAlice(fakeClock, connectionType=ConnectionType.CONSOLE)
        outcome = tracker.applySuccess(alice.state, [osSession(2, "alice", connectTime=alice.c1)])
        end, start = outcome.events
        assert end.connectionType is ConnectionType.CONSOLE
        assert start.connectionType is ConnectionType.RDP

    def test_the_new_period_continues_on_the_next_poll(self, tracker, fakeClock):
        alice = ActiveAlice(fakeClock)
        console = osSession(2, "alice", "Active", "Console", connectTime=alice.c1)
        state = tracker.applySuccess(alice.state, [console]).state
        outcome = poll(tracker, fakeClock, state, [console])
        assert outcome.events == []


class TestAc15IdentityReplaced:
    @pytest.mark.parametrize("newIdentity", ["other-user", "other-logon"])
    def test_old_end_as_logoff_then_new_start(self, tracker, fakeClock, newIdentity):
        l1 = fakeClock.now() - timedelta(hours=5)
        alice = ActiveAlice(fakeClock, logon=l1)
        t = fakeClock.now()
        inWindow = t - timedelta(minutes=1)
        if newIdentity == "other-user":
            replacement = osSession(2, "carol", disconnectTime=inWindow, logonTime=l1)
        else:
            replacement = osSession(
                2, "alice", disconnectTime=inWindow, logonTime=l1 + timedelta(minutes=1)
            )
        outcome = tracker.applySuccess(alice.state, [replacement])

        end, start = outcome.events
        assert end.eventType is EventType.SESSION_END
        assert end.usagePeriodId == alice.periodId
        assert end.userName == "alice"
        assert end.logonTime == l1
        assert end.endCause is EndCause.LOGOFF
        assert end.reason is EventReason.OBSERVED
        assert end.periodEnd == t  # the new logon's disconnect time is never used
        assert end.durationApproximate is False
        assert start.eventType is EventType.SESSION_START
        assert start.usagePeriodId != alice.periodId
        assert start.userName == replacement.userName
        assert start.logonTime == replacement.logonTime
        tracked = outcome.state.sessions[2]
        assert tracked.userName == replacement.userName
        assert tracked.logonTime == replacement.logonTime
        assert tracked.firstDetectedAt == t
        assert activityDocumentProblems(outcome.events) == []

    def test_after_outage_uses_blind_rule_without_disconnect_time(self, tracker, fakeClock):
        alice = ActiveAlice(fakeClock, outage=True)
        d = fakeClock.now() - timedelta(minutes=1)
        outcome = tracker.applySuccess(alice.state, [osSession(2, "carol", disconnectTime=d)])
        end, start = outcome.events
        assert end.endCause is EndCause.LOGOFF
        assert end.reason is EventReason.DETECTED_AFTER_OUTAGE
        assert end.durationApproximate is True
        assert end.periodEnd == max(alice.p, alice.s)
        assert start.reason is EventReason.DETECTED_AFTER_OUTAGE

    def test_replacement_disconnected_is_tracked_without_start(self, tracker, fakeClock):
        alice = ActiveAlice(fakeClock)
        outcome = tracker.applySuccess(alice.state, [osSession(2, "carol", "Disconnected")])
        assert [(e.eventType, e.userName) for e in outcome.events] == [
            (EventType.SESSION_END, "alice")
        ]
        assert outcome.state.sessions[2].userName == "carol"
        assert outcome.state.sessions[2].state is SessionState.DISCONNECTED
        assert outcome.state.status is PcStatus.AVAILABLE

    def test_replacement_transitional_is_not_tracked(self, tracker, fakeClock):
        alice = ActiveAlice(fakeClock)
        outcome = tracker.applySuccess(alice.state, [osSession(2, "carol", "Init")])
        assert [e.endCause for e in outcome.events] == [EndCause.LOGOFF]
        assert outcome.state.sessions == {}

    def test_replacing_a_disconnected_session_emits_only_the_start(self, tracker, fakeClock):
        t = fakeClock.now()
        state = trackerState([trackedDisconnected(2, "alice", disconnectedSince=t)], t)
        outcome = poll(tracker, fakeClock, state, [osSession(2, "carol")])
        assert [(e.eventType, e.userName) for e in outcome.events] == [
            (EventType.SESSION_START, "carol")
        ]


class TestSameIdentity:
    def tracked(self, **kwargs):
        start = osSession(2, "x").logonTime
        return trackedActive(
            2, "alice", connectTime=None, startEventTimestamp=None, usagePeriodStart=start, **kwargs
        )

    @pytest.mark.parametrize(
        ("trackedKw", "osKw", "expected"),
        [
            ({}, {}, True),
            ({}, {"userName": "ALICE"}, True),
            ({}, {"userName": "bob"}, False),
            ({}, {"domain": "corp"}, True),
            ({}, {"domain": "OTHER"}, False),
            ({}, {"domain": None}, True),
            ({"domain": None}, {"domain": "OTHER"}, True),
            ({}, {"logonTime": None}, True),
            ({"logonTime": None}, {}, True),
        ],
        ids=[
            "equal",
            "user-case",
            "other-user",
            "domain-case",
            "other-domain",
            "os-domain-unknown",
            "tracked-domain-unknown",
            "os-logon-unknown",
            "tracked-logon-unknown",
        ],
    )
    def test_identity_rules(self, trackedKw, osKw, expected):
        os = osSession(2, osKw.pop("userName", "alice"), **osKw)
        assert sameIdentity(self.tracked(**trackedKw), os) is expected

    def test_filter_user_sessions_keeps_only_user_sessions(self):
        kept = osSession(2, "alice", "Shadow")
        result = filterUserSessions(
            [osSession(0, "x"), osSession(65536, "y", "Ignored"), osSession(3, ""), kept]
        )
        assert result == {2: kept}


class TestAc16Transitional:
    def test_long_transitional_keeps_state_and_warns_twice(
        self, tracker, fakeClock, capturedAppLog
    ):
        c1 = fakeClock.now() - timedelta(minutes=10)
        state = tracker.applySuccess(
            tracker.initialState(), [osSession(2, "alice", connectTime=c1)]
        ).state
        period = state.sessions[2].openPeriod

        for count in range(1, 26):
            outcome = poll(
                tracker, fakeClock, state, [osSession(2, "alice", "Shadow", connectTime=c1)]
            )
            assert outcome.events == []
            assert outcome.state.status is PcStatus.IN_USE
            assert outcome.state.sessions[2].openPeriod == period
            assert outcome.state.sessions[2].consecutiveTransitionalPolls == count
            state = outcome.state

        outcome = poll(tracker, fakeClock, state, [osSession(2, "alice", connectTime=c1)])
        assert outcome.events == []
        assert outcome.state.sessions[2].openPeriod == period
        assert outcome.state.sessions[2].consecutiveTransitionalPolls == 0
        assert outcome.state.status is PcStatus.IN_USE

        lines = warnings(capturedAppLog())
        assert [line["message"] for line in lines] == [
            "session 2 (alice) has reported transitional state WTSShadow for 12 consecutive "
            "polls; keeping previous state Active",
            "session 2 (alice) has reported transitional state WTSShadow for 24 consecutive "
            "polls; keeping previous state Active",
        ]

    def test_transitional_keeps_previous_record_unchanged(self, tracker, fakeClock):
        alice = ActiveAlice(fakeClock)
        outcome = tracker.applySuccess(
            alice.state,
            [osSession(2, "alice", "Connected", "Console", connectTime=None, clientName="X")],
        )
        tracked = outcome.state.sessions[2]
        assert tracked == replace(alice.tracked, consecutiveTransitionalPolls=1)

    def test_disconnected_session_kept_disconnected(self, tracker, fakeClock, capturedAppLog):
        t = fakeClock.now()
        state = trackerState([trackedDisconnected(2, "alice", disconnectedSince=t)], t)
        small = SessionTracker(
            fakeClock, logging.getLogger("pc_handoff.domain.tracker"), transitionalWarnPolls=2
        )
        for _ in range(2):
            outcome = poll(small, fakeClock, state, [osSession(2, "alice", "Idle")])
            assert outcome.events == []
            assert outcome.state.sessions[2].state is SessionState.DISCONNECTED
            state = outcome.state
        assert outcome.state.status is PcStatus.AVAILABLE
        (line,) = warnings(capturedAppLog())
        assert line["message"].endswith(
            "WTSIdle for 2 consecutive polls; keeping previous state Disconnected"
        )

    def test_unknown_transitional_session_is_ignored(self, tracker, fakeClock):
        outcome = tracker.applySuccess(tracker.initialState(), [osSession(4, "dave", "Init")])
        assert outcome.events == []
        assert outcome.state.sessions == {}
        assert outcome.state.status is PcStatus.AVAILABLE

    def test_session_gone_while_transitional_is_logoff(self, tracker, fakeClock):
        alice = ActiveAlice(fakeClock)
        state = tracker.applySuccess(alice.state, [osSession(2, "alice", "Reset")]).state
        outcome = poll(tracker, fakeClock, state, [])
        assert [e.endCause for e in outcome.events] == [EndCause.LOGOFF]
        assert outcome.events[0].reason is EventReason.OBSERVED


class TestAc17IdentityWithoutLogonTime:
    def infoLines(self, capturedAppLog):
        return [
            line["message"]
            for line in capturedAppLog()
            if line["level"] == "INFO" and "without logonTime" in line["message"]
        ]

    def test_match_without_logon_time_is_logged_once(self, tracker, fakeClock, capturedAppLog):
        alice = ActiveAlice(fakeClock, logon=None)
        state = trackerState(
            [
                trackedActive(
                    2, "alice", connectTime=alice.c1, startEventTimestamp=alice.s, logonTime=None
                )
            ],
            alice.p,
        )
        session = osSession(2, "alice", connectTime=alice.c1, logonTime=None)
        outcome = tracker.applySuccess(state, [session])
        assert outcome.events == []
        assert self.infoLines(capturedAppLog) == [
            "identity matched without logonTime for session 2"
        ]

        outcome = poll(tracker, fakeClock, outcome.state, [session])
        assert outcome.events == []
        assert len(self.infoLines(capturedAppLog)) == 1

    def test_logged_again_for_a_new_identity_on_the_same_session_id(
        self, tracker, fakeClock, capturedAppLog
    ):
        session = osSession(2, "alice", logonTime=None)
        state = tracker.applySuccess(tracker.initialState(), [session]).state
        state = poll(tracker, fakeClock, state, [session]).state
        state = poll(tracker, fakeClock, state, []).state
        state = poll(tracker, fakeClock, state, [session]).state
        poll(tracker, fakeClock, state, [session])
        assert len(self.infoLines(capturedAppLog)) == 2

    def test_one_side_unknown_is_logged(self, tracker, fakeClock, capturedAppLog):
        alice = ActiveAlice(fakeClock)
        tracker.applySuccess(
            alice.state, [osSession(2, "alice", connectTime=alice.c1, logonTime=None)]
        )
        assert len(self.infoLines(capturedAppLog)) == 1

    def test_both_known_is_not_logged(self, tracker, fakeClock, capturedAppLog):
        alice = ActiveAlice(fakeClock)
        tracker.applySuccess(alice.state, [osSession(2, "alice", connectTime=alice.c1)])
        assert self.infoLines(capturedAppLog) == []


class TestAc19EmptyUserName:
    @pytest.mark.parametrize("state", ["Init", "Active"])
    @pytest.mark.parametrize("userName", ["", None])
    def test_known_session_with_empty_user_is_gone(self, tracker, fakeClock, state, userName):
        alice = ActiveAlice(fakeClock)
        t = fakeClock.now()
        outcome = tracker.applySuccess(alice.state, [osSession(2, userName, state)])
        (end,) = outcome.events
        assert end.usagePeriodId == alice.periodId
        assert end.endCause is EndCause.LOGOFF
        assert end.reason is EventReason.OBSERVED
        assert end.periodEnd == t
        assert outcome.state.sessions == {}
        assert outcome.state.status is PcStatus.AVAILABLE

        again = poll(tracker, fakeClock, outcome.state, [osSession(2, "alice")])
        (start,) = again.events
        assert start.eventType is EventType.SESSION_START
        assert start.usagePeriodId != alice.periodId
        assert again.state.status is PcStatus.IN_USE
        assert activityDocumentProblems(outcome.events + again.events) == []

    def test_status_follows_remaining_user_sessions(self, tracker, fakeClock):
        alice = ActiveAlice(fakeClock)
        bob = trackedActive(3, "bob", connectTime=alice.c1, startEventTimestamp=alice.s)
        state = trackerState([alice.tracked, bob], alice.p)
        outcome = tracker.applySuccess(
            state, [osSession(2, "", "Active"), osSession(3, "bob", connectTime=alice.c1)]
        )
        assert [e.sessionId for e in outcome.events] == [2]
        assert outcome.state.status is PcStatus.IN_USE

    def test_after_outage_uses_blind_rule(self, tracker, fakeClock):
        alice = ActiveAlice(fakeClock, outage=True)
        outcome = tracker.applySuccess(alice.state, [osSession(2, "", "Active")])
        (end,) = outcome.events
        assert end.reason is EventReason.DETECTED_AFTER_OUTAGE
        assert end.durationApproximate is True
        assert end.periodEnd == max(alice.p, alice.s)
        assert end.endCause is EndCause.LOGOFF

    def test_disconnected_session_removed_without_event(self, tracker, fakeClock):
        t = fakeClock.now()
        state = trackerState([trackedDisconnected(2, "alice", disconnectedSince=t)], t)
        outcome = poll(tracker, fakeClock, state, [osSession(2, "", "Disconnected")])
        assert outcome.events == []
        assert outcome.state.sessions == {}

    @pytest.mark.parametrize("state", ["Active", "Disconnected", "Shadow"])
    def test_unknown_session_with_empty_user_is_ignored(self, tracker, state):
        outcome = tracker.applySuccess(tracker.initialState(), [osSession(5, "", state)])
        assert outcome.events == []
        assert outcome.state.sessions == {}
        assert outcome.state.status is PcStatus.AVAILABLE


class TestAc20BackwardClockStep:
    def setUp(self, tracker, fakeClock, connectOffset):
        s = fakeClock.now()
        c = s - timedelta(seconds=connectOffset)
        state = tracker.applySuccess(tracker.initialState(), [osSession(2, "alice", connectTime=c)])
        fakeClock.set(s - timedelta(seconds=30))
        return s, c, state

    @pytest.mark.parametrize("case", ["disconnected", "absent"])
    def test_end_is_clamped_and_logged_once(self, tracker, fakeClock, capturedAppLog, case):
        s, c, first = self.setUp(tracker, fakeClock, connectOffset=10)
        disconnected = [osSession(2, "alice", "Disconnected", connectTime=c)]
        snapshot = disconnected if case == "disconnected" else []

        outcome = tracker.applySuccess(first.state, snapshot)

        (end,) = outcome.events
        assert end.timestamp >= s
        assert end.timestamp == s
        assert end.periodEnd >= end.usagePeriodStart
        assert end.periodEnd == c
        assert end.durationSeconds == 0
        lines = capturedAppLog()
        clamp = [line for line in lines if "wall clock moved backwards" in line["message"]]
        assert len(clamp) == 1
        assert clamp[0]["level"] == "WARNING"
        assert "fields timestamp, periodEnd" in clamp[0]["message"]
        assert not [line for line in lines if line["level"] == "ERROR"]
        assert activityDocumentProblems(first.events + outcome.events) == []
        if case == "disconnected":
            assert outcome.state.sessions[2].disconnectedSince == end.periodEnd

    def test_only_timestamp_clamped_when_period_start_is_earlier(
        self, tracker, fakeClock, capturedAppLog
    ):
        s, _, first = self.setUp(tracker, fakeClock, connectOffset=600)
        outcome = tracker.applySuccess(first.state, [])
        (end,) = outcome.events
        assert end.timestamp == s
        assert end.periodEnd == fakeClock.now()
        clamp = [line for line in capturedAppLog() if "wall clock" in line["message"]]
        assert len(clamp) == 1
        assert clamp[0]["message"].endswith("fields timestamp)")

    def test_start_timestamp_and_last_poll_are_not_clamped(self, tracker, fakeClock):
        s, _, first = self.setUp(tracker, fakeClock, connectOffset=10)
        outcome = tracker.applySuccess(first.state, [osSession(3, "bob")])
        start = next(e for e in outcome.events if e.eventType is EventType.SESSION_START)
        assert start.timestamp == s - timedelta(seconds=30)
        assert outcome.state.lastSuccessfulPollAt == s - timedelta(seconds=30)


class TestAc21DisconnectedSinceAfterTypeChange:
    @pytest.mark.parametrize("case", ["stale", "in-window"])
    def test_disconnected_since_equals_period_end(self, tracker, fakeClock, case):
        c1 = fakeClock.now() - timedelta(minutes=30)
        state = tracker.applySuccess(
            tracker.initialState(), [osSession(2, "alice", connectTime=c1)]
        ).state
        fakeClock.advance(60)
        r1 = fakeClock.now()
        switched = tracker.applySuccess(
            state, [osSession(2, "alice", "Active", "Console", connectTime=c1)]
        )
        u2 = switched.state.sessions[2].openPeriod
        assert (u2.usagePeriodStart, u2.durationApproximate) == (r1, True)

        fakeClock.advance(60)
        r2 = fakeClock.now()
        d = c1 + timedelta(minutes=10) if case == "stale" else r1 + timedelta(seconds=20)
        outcome = tracker.applySuccess(
            switched.state,
            [osSession(2, "alice", "Disconnected", "Console", connectTime=c1, disconnectTime=d)],
        )
        (end,) = outcome.events
        assert end.usagePeriodId == u2.usagePeriodId
        assert end.periodEnd == (r2 if case == "stale" else d)
        assert outcome.state.sessions[2].disconnectedSince == end.periodEnd


def staleWarnings(lines):
    return [
        line
        for line in lines
        if line["level"] == "WARNING" and "treated as not reported (I4)" in line["message"]
    ]


class TestI4StaleConnectTime:
    """Critic TASK-004 M1 / interpretation I4: a connect time earlier than the end of the
    session's previous period does not describe the new connection; it is "not reported"."""

    def disconnectedAlice(self, tracker, fakeClock, *, c1):
        state = tracker.applySuccess(
            tracker.initialState(), [osSession(2, "alice", connectTime=c1)]
        ).state
        outcome = poll(
            tracker, fakeClock, state, [osSession(2, "alice", "Disconnected", connectTime=c1)]
        )
        (end,) = outcome.events
        return outcome.state, end

    def assertNotReported(self, start, outcome, now):
        assert start.eventType is EventType.SESSION_START
        assert start.usagePeriodStart == now
        assert start.durationApproximate is True
        assert start.connectTime is None
        assert outcome.state.sessions[2].openPeriod.connectTimeAtStart is None

    def test_critic_probe_console_resume_with_stale_connect_time(
        self, tracker, fakeClock, capturedAppLog
    ):
        c1 = fakeClock.now() - timedelta(minutes=30)
        state, end = self.disconnectedAlice(tracker, fakeClock, c1=c1)
        fakeClock.advance(3600)
        r = fakeClock.now()

        outcome = tracker.applySuccess(
            state, [osSession(2, "alice", "Active", "Console", connectTime=c1)]
        )

        (start,) = outcome.events
        self.assertNotReported(start, outcome, r)
        assert start.connectionType is ConnectionType.CONSOLE
        assert start.usagePeriodStart >= end.periodEnd
        (line,) = staleWarnings(capturedAppLog())
        assert line["message"] == (
            f"connect time {c1.isoformat()} of session 2 is earlier than the end of the last "
            f"period on that sessionId {end.periodEnd.isoformat()}; treated as not reported (I4)"
        )

    def test_connect_time_after_c1_but_before_previous_end(self, tracker, fakeClock):
        c1 = fakeClock.now() - timedelta(minutes=30)
        state, end = self.disconnectedAlice(tracker, fakeClock, c1=c1)
        fakeClock.advance(600)
        outcome = tracker.applySuccess(
            state, [osSession(2, "alice", connectTime=end.periodEnd - timedelta(seconds=1))]
        )
        self.assertNotReported(outcome.events[0], outcome, fakeClock.now())

    @pytest.mark.parametrize("offset", [0, 30])
    def test_connect_time_at_or_after_previous_end_is_used(
        self, tracker, fakeClock, capturedAppLog, offset
    ):
        c1 = fakeClock.now() - timedelta(minutes=30)
        state, end = self.disconnectedAlice(tracker, fakeClock, c1=c1)
        fakeClock.advance(600)
        c2 = end.periodEnd + timedelta(seconds=offset)
        outcome = tracker.applySuccess(state, [osSession(2, "alice", connectTime=c2)])
        (start,) = outcome.events
        assert start.usagePeriodStart == c2
        assert start.durationApproximate is False
        assert start.connectTime == c2
        assert staleWarnings(capturedAppLog()) == []

    def test_previous_end_survives_disconnected_and_transitional_polls(self, tracker, fakeClock):
        c1 = fakeClock.now() - timedelta(minutes=30)
        state, _ = self.disconnectedAlice(tracker, fakeClock, c1=c1)
        state = poll(
            tracker, fakeClock, state, [osSession(2, "alice", "Disconnected", connectTime=c1)]
        ).state
        state = poll(tracker, fakeClock, state, [osSession(2, "alice", "ConnectQuery")]).state
        outcome = poll(tracker, fakeClock, state, [osSession(2, "alice", connectTime=c1)])
        self.assertNotReported(outcome.events[0], outcome, fakeClock.now())

    def test_first_seen_disconnected_uses_disconnected_since_as_bound(self, tracker, fakeClock):
        t = fakeClock.now()
        c0 = t - timedelta(hours=2)
        d = t - timedelta(minutes=10)
        state = tracker.applySuccess(
            tracker.initialState(),
            [osSession(2, "alice", "Disconnected", connectTime=c0, disconnectTime=d)],
        ).state
        assert state.sessions[2].disconnectedSince == d
        outcome = poll(tracker, fakeClock, state, [osSession(2, "alice", connectTime=c0)])
        self.assertNotReported(outcome.events[0], outcome, fakeClock.now())

    def test_first_seen_disconnected_with_later_connect_time_uses_it(self, tracker, fakeClock):
        t = fakeClock.now()
        state = tracker.applySuccess(
            tracker.initialState(),
            [osSession(2, "alice", "Disconnected", disconnectTime=t - timedelta(minutes=10))],
        ).state
        c2 = t + timedelta(seconds=2)
        outcome = poll(tracker, fakeClock, state, [osSession(2, "alice", connectTime=c2)])
        assert outcome.events[0].usagePeriodStart == c2

    def test_connect_time_moving_backwards(self, tracker, fakeClock, capturedAppLog):
        alice = ActiveAlice(fakeClock)
        r = fakeClock.now()
        outcome = tracker.applySuccess(
            alice.state, [osSession(2, "alice", connectTime=alice.c1 - timedelta(hours=2))]
        )
        end, start = outcome.events
        assert end.endCause is EndCause.RECONNECT_DETECTED
        assert end.periodEnd == max(alice.p, alice.s)
        self.assertNotReported(start, outcome, r)
        assert start.usagePeriodStart >= end.periodEnd
        assert len(staleWarnings(capturedAppLog())) == 1

    def test_new_connect_time_before_fallback_end_uses_detection_window(
        self, tracker, fakeClock, capturedAppLog
    ):
        """C2 lies in [C1, max(P, S)): it cannot be the new connection, so the end window
        is [C1, R] as for an unreported connect time, and a D after C2 is accepted."""
        alice = ActiveAlice(fakeClock)
        r = fakeClock.now()
        c2 = alice.s - timedelta(minutes=1)
        d = alice.p + timedelta(seconds=1)
        outcome = tracker.applySuccess(
            alice.state, [osSession(2, "alice", connectTime=c2, disconnectTime=d)]
        )
        end, start = outcome.events
        assert end.periodEnd == d
        self.assertNotReported(start, outcome, r)
        assert len(staleWarnings(capturedAppLog())) == 1

    def test_ac11_fast_reconnect_is_unaffected(self, tracker, fakeClock, capturedAppLog):
        alice = ActiveAlice(fakeClock)
        c2 = fakeClock.now() - timedelta(seconds=2)
        outcome = tracker.applySuccess(alice.state, [osSession(2, "alice", connectTime=c2)])
        assert outcome.events[1].usagePeriodStart == c2
        assert staleWarnings(capturedAppLog()) == []


class TestM3LastKnownConnectTime:
    """Critic TASK-004 M3: a null connect time does not erase the comparison basis."""

    def test_c1_none_c2_is_a_reconnect(self, tracker, fakeClock):
        alice = ActiveAlice(fakeClock)
        state = tracker.applySuccess(alice.state, [osSession(2, "alice", connectTime=None)]).state
        assert state.sessions[2].connectTime is None
        c2 = fakeClock.now() + timedelta(seconds=2)
        outcome = poll(tracker, fakeClock, state, [osSession(2, "alice", connectTime=c2)])
        end, start = outcome.events
        assert end.endCause is EndCause.RECONNECT_DETECTED
        assert end.usagePeriodId == alice.periodId
        assert start.usagePeriodStart == c2
        assert start.durationApproximate is False

    def test_c1_none_c1_is_not_a_reconnect(self, tracker, fakeClock):
        alice = ActiveAlice(fakeClock)
        state = tracker.applySuccess(alice.state, [osSession(2, "alice", connectTime=None)]).state
        outcome = poll(tracker, fakeClock, state, [osSession(2, "alice", connectTime=alice.c1)])
        assert outcome.events == []

    def test_type_change_after_null_with_old_connect_time_is_still_i1(self, tracker, fakeClock):
        alice = ActiveAlice(fakeClock)
        state = tracker.applySuccess(alice.state, [osSession(2, "alice", connectTime=None)]).state
        outcome = poll(
            tracker,
            fakeClock,
            state,
            [osSession(2, "alice", "Active", "Console", connectTime=alice.c1)],
        )
        _, start = outcome.events
        assert start.usagePeriodStart == fakeClock.now()
        assert start.connectTime is None

    def test_null_to_value_without_known_value_is_not_a_reconnect(self, tracker, fakeClock):
        state = tracker.applySuccess(
            tracker.initialState(), [osSession(4, "dave", connectTime=None)]
        ).state
        state = poll(tracker, fakeClock, state, [osSession(4, "dave", connectTime=None)]).state
        outcome = poll(
            tracker, fakeClock, state, [osSession(4, "dave", connectTime=fakeClock.now())]
        )
        assert outcome.events == []


SESSION_BOUND = "the end of the last period on that sessionId"


class TestN1SessionIdBound:
    """Critic TASK-004 N1: the I4 bound also covers earlier identities on the same sessionId,
    so a period never starts before the last period on that sessionId ended."""

    def aliceEnded(self, tracker, fakeClock, endingSnapshot):
        c1 = fakeClock.now() - timedelta(minutes=30)
        logon = fakeClock.now() - timedelta(hours=1)
        alice = osSession(2, "alice", connectTime=c1, logonTime=logon)
        state = tracker.applySuccess(tracker.initialState(), [alice]).state
        outcome = poll(tracker, fakeClock, state, endingSnapshot)
        (end,) = outcome.events
        assert end.endCause is EndCause.LOGOFF
        assert outcome.state.sessions == {}
        assert outcome.state.lastPeriodEndBySessionId == {2: end.periodEnd}
        return alice, end, outcome.state

    def assertNotReported(self, start, outcome, now):
        assert start.eventType is EventType.SESSION_START
        assert start.usagePeriodStart == now
        assert start.durationApproximate is True
        assert start.connectTime is None
        assert outcome.state.sessions[start.sessionId].openPeriod.connectTimeAtStart is None

    def test_ac19_blip_same_logon_returns_with_old_connect_time(
        self, tracker, fakeClock, capturedAppLog
    ):
        alice, end, state = self.aliceEnded(tracker, fakeClock, [osSession(2, "", "Active")])

        outcome = poll(tracker, fakeClock, state, [alice])

        (start,) = outcome.events
        self.assertNotReported(start, outcome, fakeClock.now())
        assert start.usagePeriodStart >= end.periodEnd  # nothing counted twice
        (line,) = staleWarnings(capturedAppLog())
        assert line["message"] == (
            f"connect time {alice.connectTime.isoformat()} of session 2 is earlier than "
            f"{SESSION_BOUND} {end.periodEnd.isoformat()}; treated as not reported (I4)"
        )

    def test_return_after_several_polls_absent(self, tracker, fakeClock):
        alice, end, state = self.aliceEnded(tracker, fakeClock, [])
        for _ in range(4):
            state = poll(tracker, fakeClock, state, []).state
        assert state.lastPeriodEndBySessionId == {2: end.periodEnd}
        outcome = poll(tracker, fakeClock, state, [alice])
        self.assertNotReported(outcome.events[0], outcome, fakeClock.now())

    def test_genuine_replacement_with_fresh_connect_time_is_used(
        self, tracker, fakeClock, capturedAppLog
    ):
        _, end, state = self.aliceEnded(tracker, fakeClock, [])
        fakeClock.advance(60)
        fresh = end.periodEnd + timedelta(seconds=30)
        outcome = tracker.applySuccess(state, [osSession(2, "carol", connectTime=fresh)])
        (start,) = outcome.events
        assert start.usagePeriodStart == fresh
        assert start.durationApproximate is False
        assert staleWarnings(capturedAppLog()) == []

    def test_connect_time_equal_to_last_end_is_used(self, tracker, fakeClock):
        _, end, state = self.aliceEnded(tracker, fakeClock, [])
        outcome = poll(
            tracker, fakeClock, state, [osSession(2, "carol", connectTime=end.periodEnd)]
        )
        assert outcome.events[0].usagePeriodStart == end.periodEnd

    def test_same_poll_replacement_starts_at_detection_time(
        self, tracker, fakeClock, capturedAppLog
    ):
        """Accepted under-count (A7): the old end is the detection time R, so the new
        logon's genuine connect time (< R) cannot be told from a stale one."""
        alice = ActiveAlice(fakeClock)
        r = fakeClock.now()
        carol = osSession(2, "carol", connectTime=r - timedelta(seconds=2))
        outcome = tracker.applySuccess(alice.state, [carol])
        end, start = outcome.events
        assert end.periodEnd == r
        self.assertNotReported(start, outcome, r)
        assert len(staleWarnings(capturedAppLog())) == 1
        assert outcome.state.lastPeriodEndBySessionId == {2: r}

    def test_disconnected_to_active_uses_the_later_bound(self, tracker, fakeClock):
        """bob was first seen Disconnected (disconnectedSince D) after alice's period on the
        same sessionId ended later than D; a connect time in between is stale."""
        _, end, state = self.aliceEnded(tracker, fakeClock, [])
        d = end.periodEnd - timedelta(minutes=5)
        state = poll(
            tracker, fakeClock, state, [osSession(2, "bob", "Disconnected", disconnectTime=d)]
        ).state
        assert state.sessions[2].disconnectedSince == d
        between = end.periodEnd - timedelta(seconds=1)
        outcome = poll(tracker, fakeClock, state, [osSession(2, "bob", connectTime=between)])
        self.assertNotReported(outcome.events[0], outcome, fakeClock.now())

    def test_map_is_per_session_id_and_never_moves_back(self, tracker, fakeClock):
        t = fakeClock.now()
        state = trackerState(
            [
                trackedActive(
                    2, "alice", connectTime=t - timedelta(minutes=9), startEventTimestamp=t
                ),
                trackedActive(
                    3, "bob", connectTime=t - timedelta(minutes=9), startEventTimestamp=t
                ),
            ],
            t,
        )
        state = replace(state, lastPeriodEndBySessionId={2: t + timedelta(hours=1), 7: t})
        outcome = poll(tracker, fakeClock, state, [])
        assert outcome.state.lastPeriodEndBySessionId == {
            2: t + timedelta(hours=1),
            3: fakeClock.now(),
            7: t,
        }

    def test_reconnect_is_bounded_by_an_earlier_end_on_the_session_id(
        self, tracker, fakeClock, capturedAppLog
    ):
        """After a backward clock step the open period started (at a detection time) before
        the last end E on this sessionId; a reconnect C2 between its own clamped end and E
        would still overlap that earlier period, so it is not reported."""
        t = fakeClock.now()
        e = t - timedelta(seconds=10)
        start = t - timedelta(seconds=60)
        alice = trackedActive(2, "alice", connectTime=start, startEventTimestamp=start)
        state = replace(
            trackerState([alice], t - timedelta(seconds=50)), lastPeriodEndBySessionId={2: e}
        )
        c2 = t - timedelta(seconds=30)

        outcome = tracker.applySuccess(state, [osSession(2, "alice", connectTime=c2)])

        end, newStart = outcome.events
        assert end.periodEnd == t - timedelta(seconds=50)  # max(P, S) < c2 < e
        self.assertNotReported(newStart, outcome, t)
        (line,) = staleWarnings(capturedAppLog())
        assert SESSION_BOUND in line["message"]

    def test_initial_failure_and_reconciled_states_carry_the_map(self, tracker, fakeClock):
        assert tracker.initialState().lastPeriodEndBySessionId == {}
        seeded = {4: fakeClock.now()}
        state = tracker.stateFromReconciled({}, fakeClock.now(), seeded)
        assert state.lastPeriodEndBySessionId == seeded
        assert state.lastPeriodEndBySessionId is not seeded
        assert tracker.stateFromReconciled({}, fakeClock.now()).lastPeriodEndBySessionId == {}
        failed = tracker.applyFailure(state, RuntimeError("x")).state
        assert failed.lastPeriodEndBySessionId == seeded


class TestN2ReconnectBoundIsClamped:
    """Critic TASK-004 N2: the I4 bound on a reconnect is the R11-clamped end. After a
    backward clock step the blind fallback max(P, S) can lie before usagePeriodStart C; a
    C2 between the two is still before the old period's (clamped) end C."""

    def test_c2_between_fallback_and_period_start_is_not_reported(
        self, tracker, fakeClock, capturedAppLog
    ):
        t = fakeClock.now()
        c = t - timedelta(seconds=60)
        s = t - timedelta(seconds=120)
        p = t - timedelta(seconds=100)
        alice = trackedActive(2, "alice", connectTime=c, startEventTimestamp=s)
        state = trackerState([alice], p)
        c2 = t - timedelta(seconds=80)

        outcome = tracker.applySuccess(state, [osSession(2, "alice", connectTime=c2)])

        end, start = outcome.events
        assert end.periodEnd == c  # clamped from max(P, S) by R11
        assert start.usagePeriodStart == t
        assert start.usagePeriodStart >= end.periodEnd
        assert start.durationApproximate is True
        assert len(staleWarnings(capturedAppLog())) == 1
