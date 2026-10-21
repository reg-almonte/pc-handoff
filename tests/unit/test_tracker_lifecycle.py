"""SessionTracker lifecycle: logon, logoff, disconnect, reconnect, multiple sessions (TASK-004).

Task ACs 1-7, 10, 12, 13, plus the real dev-PC session shape (Critic TASK-002 F1) and the
pre-refresh session passed to ``newEndEvent`` (Critic TASK-003 S5).
"""

import uuid
from datetime import timedelta

import pytest

from pc_handoff.detection.provider import ConnectionType
from pc_handoff.domain.models import (
    EndCause,
    EventReason,
    EventType,
    PcStatus,
    SessionState,
)
from pc_handoff.domain.tracker import TRANSITIONAL_WARN_POLLS, PollOutcome, SessionTracker
from tests.fakes.activity_checks import activityDocumentProblems
from tests.fakes.builders import osSession, trackedActive, trackedDisconnected, trackerState

POLL = 5


def isUuid4(value: str) -> bool:
    return uuid.UUID(value).version == 4 and str(uuid.UUID(value)) == value


def poll(tracker: SessionTracker, fakeClock, state, sessions) -> PollOutcome:
    fakeClock.advance(POLL)
    return tracker.applySuccess(state, sessions)


def starts(outcome: PollOutcome):
    return [e for e in outcome.events if e.eventType is EventType.SESSION_START]


def ends(outcome: PollOutcome):
    return [e for e in outcome.events if e.eventType is EventType.SESSION_END]


class TestInitialState:
    def test_initial_state_is_unknown_and_empty(self, tracker):
        state = tracker.initialState()
        assert state.sessions == {}
        assert state.status is PcStatus.UNKNOWN
        assert state.detectionHealthy is False
        assert state.outagePending is False
        assert state.lastKnownStatus is None
        assert state.lastKnownStatusAt is None
        assert state.lastSuccessfulPollAt is None

    def test_warn_poll_default_is_twelve(self):
        assert TRANSITIONAL_WARN_POLLS == 12

    def test_detection_time_defaults_to_clock_and_can_be_passed(self, tracker, fakeClock):
        state = tracker.applySuccess(tracker.initialState(), []).state
        assert state.lastSuccessfulPollAt == fakeClock.now()
        explicit = fakeClock.now() + timedelta(seconds=42)
        state = tracker.applySuccess(state, [], explicit).state
        assert state.lastSuccessfulPollAt == explicit


class TestAc01NewActiveRdpSession:
    def test_starts_one_observed_period_with_full_fields(self, tracker, fakeClock):
        t = fakeClock.now()
        connect = t - timedelta(minutes=10)
        logon = t - timedelta(hours=1)
        bob = osSession(
            3,
            "bob",
            clientName="BOB-PC",
            clientAddress="192.168.1.40",
            connectTime=connect,
            logonTime=logon,
        )

        outcome = tracker.applySuccess(tracker.initialState(), [bob])

        assert len(outcome.events) == 1
        start = outcome.events[0]
        assert start.eventType is EventType.SESSION_START
        assert isUuid4(start.usagePeriodId)
        assert start.userName == "bob"
        assert start.domain == "CORP"
        assert start.sessionId == 3
        assert start.logonTime == logon
        assert start.clientName == "BOB-PC"
        assert start.clientAddress == "192.168.1.40"
        assert start.connectionType is ConnectionType.RDP
        assert start.connectTime == connect
        assert start.usagePeriodStart == connect
        assert start.durationApproximate is False
        assert start.reason is EventReason.OBSERVED
        assert start.timestamp == t
        assert outcome.state.status is PcStatus.IN_USE

        tracked = outcome.state.sessions[3]
        assert tracked.state is SessionState.ACTIVE
        assert tracked.firstDetectedAt == t
        assert tracked.openPeriod.usagePeriodId == start.usagePeriodId
        assert tracked.openPeriod.startEventTimestamp == start.timestamp

        state = outcome.state
        for _ in range(3):
            outcome = poll(tracker, fakeClock, state, [bob])
            assert outcome.events == []
            assert outcome.state.status is PcStatus.IN_USE
            assert outcome.state.sessions[3].openPeriod == tracked.openPeriod
            state = outcome.state


class TestAc02NonUserSessionsIgnored:
    def test_services_listener_and_empty_user_are_ignored(self, tracker):
        sessions = [
            osSession(0, None, "Disconnected", "Console", winStationName="Services"),
            osSession(65536, None, "Ignored", "Console", winStationName="RDP-Tcp"),
            osSession(1, "", "Active", "Console"),
        ]
        outcome = tracker.applySuccess(tracker.initialState(), sessions)
        assert outcome.state.status is PcStatus.AVAILABLE
        assert outcome.state.sessions == {}
        assert outcome.events == []

    @pytest.mark.parametrize(
        "session",
        [
            osSession(0, "SYSTEM", "Active", "Console"),
            osSession(65536, "alice", "Ignored"),
            osSession(4, None, "Active"),
            osSession(4, "", "Disconnected"),
        ],
        ids=["session-0", "listener", "none-user", "empty-user"],
    )
    def test_each_filter_rule_alone_drops_the_session(self, tracker, session):
        outcome = tracker.applySuccess(tracker.initialState(), [session])
        assert outcome.state.sessions == {}
        assert outcome.events == []
        assert outcome.state.status is PcStatus.AVAILABLE


class TestRealDevPcSessionShape:
    """Critic TASK-002 F1: the real enumeration seen on PC545 (decisions.log, TASK-002)."""

    def realSnapshot(self, connect):
        return [
            osSession(
                0,
                None,
                "Disconnected",
                "Console",
                winStationName="Services",
                domain=None,
                logonTime=None,
                connectTime=None,
            ),
            osSession(
                1,
                "prnsoft",
                "Active",
                "Console",
                domain="PC545",
                connectTime=connect,
                disconnectTime=None,
            ),
            osSession(
                65536,
                None,
                "Ignored",
                "Console",
                winStationName="RDP-Tcp",
                domain=None,
                protocolType=0,
                logonTime=None,
                connectTime=None,
            ),
        ]

    def test_exactly_one_user_session_and_one_start_then_nothing(self, tracker, fakeClock):
        connect = fakeClock.now() - timedelta(hours=2)
        snapshot = self.realSnapshot(connect)

        first = tracker.applySuccess(tracker.initialState(), snapshot)

        assert list(first.state.sessions) == [1]
        tracked = first.state.sessions[1]
        assert (tracked.userName, tracked.domain) == ("prnsoft", "PC545")
        assert tracked.connectionType is ConnectionType.CONSOLE
        assert tracked.state is SessionState.ACTIVE
        assert len(first.events) == 1
        assert first.events[0].eventType is EventType.SESSION_START
        assert first.events[0].sessionId == 1
        assert first.events[0].connectionType is ConnectionType.CONSOLE
        assert first.state.status is PcStatus.IN_USE

        state = first.state
        for _ in range(3):
            again = poll(tracker, fakeClock, state, snapshot)
            assert again.events == []
            assert list(again.state.sessions) == [1]
            state = again.state


class TestAc03Logoff:
    def test_logoff_ends_period_at_detection_time(self, tracker, fakeClock):
        connect = fakeClock.now() - timedelta(minutes=30)
        bob = osSession(3, "bob", connectTime=connect)
        state = tracker.applySuccess(tracker.initialState(), [bob]).state
        periodId = state.sessions[3].openPeriod.usagePeriodId

        outcome = poll(tracker, fakeClock, state, [])
        t2 = fakeClock.now()

        assert len(outcome.events) == 1
        end = outcome.events[0]
        assert end.eventType is EventType.SESSION_END
        assert end.usagePeriodId == periodId
        assert end.endCause is EndCause.LOGOFF
        assert end.reason is EventReason.OBSERVED
        assert end.durationApproximate is False
        assert end.periodEnd == t2
        assert end.timestamp == t2
        assert end.durationSeconds == int((t2 - connect).total_seconds())
        assert outcome.state.status is PcStatus.AVAILABLE
        assert outcome.state.sessions == {}


class TestAc04Disconnect:
    def setUp(self, tracker, fakeClock):
        c1 = fakeClock.now() - timedelta(minutes=20)
        alice = osSession(2, "alice", connectTime=c1)
        return c1, tracker.applySuccess(tracker.initialState(), [alice]).state

    def test_disconnect_with_os_time_in_window(self, tracker, fakeClock):
        c1, state = self.setUp(tracker, fakeClock)
        periodId = state.sessions[2].openPeriod.usagePeriodId
        d = fakeClock.now() + timedelta(seconds=2)

        outcome = poll(
            tracker,
            fakeClock,
            state,
            [osSession(2, "alice", "Disconnected", connectTime=c1, disconnectTime=d)],
        )

        assert len(outcome.events) == 1
        end = outcome.events[0]
        assert end.usagePeriodId == periodId
        assert end.endCause is EndCause.DISCONNECT
        assert end.reason is EventReason.OBSERVED
        assert end.durationApproximate is False
        assert end.periodEnd == d
        assert end.durationSeconds == int((d - c1).total_seconds())
        tracked = outcome.state.sessions[2]
        assert tracked.state is SessionState.DISCONNECTED
        assert tracked.openPeriod is None
        assert tracked.disconnectedSince == d
        assert tracked.osDisconnectTime == d
        assert outcome.state.status is PcStatus.AVAILABLE

    def test_disconnect_without_os_time_uses_detection_time(self, tracker, fakeClock):
        c1, state = self.setUp(tracker, fakeClock)
        outcome = poll(
            tracker, fakeClock, state, [osSession(2, "alice", "Disconnected", connectTime=c1)]
        )
        t = fakeClock.now()
        assert outcome.events[0].periodEnd == t
        assert outcome.state.sessions[2].disconnectedSince == t

    def test_disconnect_with_os_time_outside_window_uses_detection_time(self, tracker, fakeClock):
        c1, state = self.setUp(tracker, fakeClock)
        stale = c1 - timedelta(minutes=1)
        outcome = poll(
            tracker,
            fakeClock,
            state,
            [osSession(2, "alice", "Disconnected", connectTime=c1, disconnectTime=stale)],
        )
        t = fakeClock.now()
        assert outcome.events[0].periodEnd == t
        assert outcome.state.sessions[2].disconnectedSince == t

    def test_staying_disconnected_keeps_disconnected_since_and_refreshes_fields(
        self, tracker, fakeClock
    ):
        c1, state = self.setUp(tracker, fakeClock)
        d = fakeClock.now() + timedelta(seconds=1)
        state = poll(
            tracker,
            fakeClock,
            state,
            [osSession(2, "alice", "Disconnected", connectTime=c1, disconnectTime=d)],
        ).state
        later = osSession(
            2,
            "alice",
            "Disconnected",
            connectTime=c1,
            disconnectTime=d + timedelta(seconds=30),
            clientName=None,
            clientAddress=None,
        )
        outcome = poll(tracker, fakeClock, state, [later])
        assert outcome.events == []
        tracked = outcome.state.sessions[2]
        assert tracked.disconnectedSince == d
        assert tracked.osDisconnectTime == d + timedelta(seconds=30)
        assert tracked.clientName is None


class TestAc05ReconnectThroughTransitional:
    def test_transitional_poll_is_silent_then_new_period(self, tracker, fakeClock):
        logon = fakeClock.now() - timedelta(hours=3)
        c1 = fakeClock.now() - timedelta(hours=1)
        alice = osSession(2, "alice", connectTime=c1, logonTime=logon)
        state = tracker.applySuccess(tracker.initialState(), [alice]).state
        oldId = state.sessions[2].openPeriod.usagePeriodId
        state = poll(
            tracker,
            fakeClock,
            state,
            [osSession(2, "alice", "Disconnected", connectTime=c1, logonTime=logon)],
        ).state
        before = state.sessions[2]

        p1 = poll(
            tracker,
            fakeClock,
            state,
            [osSession(2, "alice", "ConnectQuery", connectTime=c1, logonTime=logon)],
        )
        assert p1.events == []
        assert p1.state.sessions[2].state is SessionState.DISCONNECTED
        assert p1.state.sessions[2].disconnectedSince == before.disconnectedSince
        assert p1.state.status is PcStatus.AVAILABLE

        c2 = fakeClock.now() + timedelta(seconds=3)
        p2 = poll(
            tracker,
            fakeClock,
            p1.state,
            [
                osSession(
                    2,
                    "alice",
                    clientName="ALICE-DESKTOP",
                    clientAddress="192.168.1.30",
                    connectTime=c2,
                    logonTime=logon,
                )
            ],
        )
        assert len(p2.events) == 1
        start = p2.events[0]
        assert start.eventType is EventType.SESSION_START
        assert start.usagePeriodId != oldId
        assert start.sessionId == 2
        assert start.usagePeriodStart == c2
        assert start.durationApproximate is False
        assert (start.userName, start.logonTime) == ("alice", logon)
        assert (start.clientName, start.clientAddress) == ("ALICE-DESKTOP", "192.168.1.30")
        tracked = p2.state.sessions[2]
        assert (tracked.sessionId, tracked.userName, tracked.logonTime) == (2, "alice", logon)
        assert tracked.firstDetectedAt == before.firstDetectedAt
        assert tracked.disconnectedSince is None
        assert tracked.consecutiveTransitionalPolls == 0
        assert p2.state.status is PcStatus.IN_USE


class TestAc06MultipleSessions:
    def test_status_sequence_and_two_ends(self, tracker, fakeClock):
        c = fakeClock.now() - timedelta(minutes=5)
        alice = osSession(2, "alice", connectTime=c)
        bob = osSession(3, "bob", connectTime=c)
        aliceOff = osSession(2, "alice", "Disconnected", connectTime=c)
        state = tracker.applySuccess(tracker.initialState(), [alice, bob]).state

        statuses = []
        allEvents = []
        for snapshot in ([aliceOff, bob], [aliceOff, bob], [aliceOff, bob], [aliceOff]):
            outcome = poll(tracker, fakeClock, state, snapshot)
            statuses.append(outcome.state.status)
            allEvents.extend(outcome.events)
            state = outcome.state

        assert statuses == [PcStatus.IN_USE, PcStatus.IN_USE, PcStatus.IN_USE, PcStatus.AVAILABLE]
        assert [(e.eventType, e.userName, e.endCause) for e in allEvents] == [
            (EventType.SESSION_END, "alice", EndCause.DISCONNECT),
            (EventType.SESSION_END, "bob", EndCause.LOGOFF),
        ]

    def test_disconnected_bob_never_makes_in_use(self, tracker, fakeClock):
        c = fakeClock.now() - timedelta(minutes=5)
        alice = osSession(2, "alice", connectTime=c)
        bobOff = osSession(3, "bob", "Disconnected", connectTime=c)
        first = tracker.applySuccess(tracker.initialState(), [alice, bobOff])
        assert [e.userName for e in first.events] == ["alice"]
        assert first.state.status is PcStatus.IN_USE

        outcome = poll(
            tracker,
            fakeClock,
            first.state,
            [osSession(2, "alice", "Disconnected", connectTime=c), bobOff],
        )
        assert outcome.state.status is PcStatus.AVAILABLE
        assert {s.state for s in outcome.state.sessions.values()} == {SessionState.DISCONNECTED}
        assert [(e.userName, e.endCause) for e in outcome.events] == [
            ("alice", EndCause.DISCONNECT)
        ]

    def test_events_ordered_by_session_id(self, tracker, fakeClock):
        sessions = [osSession(sid, f"user{sid}") for sid in (7, 2, 5)]
        outcome = tracker.applySuccess(tracker.initialState(), sessions)
        assert [e.sessionId for e in outcome.events] == [2, 5, 7]
        assert list(outcome.state.sessions) == [2, 5, 7]


class TestAc07NullClientAndConnectTime:
    def test_null_fields_and_approximate_start_and_end(self, tracker, fakeClock):
        t1 = fakeClock.now()
        session = osSession(4, "dave", clientName=None, clientAddress=None, connectTime=None)
        outcome = tracker.applySuccess(tracker.initialState(), [session])
        start = outcome.events[0]
        assert (start.clientName, start.clientAddress, start.connectTime) == (None, None, None)
        assert start.usagePeriodStart == t1
        assert start.durationApproximate is True

        end = poll(
            tracker,
            fakeClock,
            outcome.state,
            [osSession(4, "dave", "Disconnected", clientName=None, connectTime=None)],
        ).events[0]
        assert end.durationApproximate is True
        assert end.endCause is EndCause.DISCONNECT
        assert end.reason is EventReason.OBSERVED
        assert end.periodEnd == fakeClock.now()
        assert end.durationSeconds == POLL

    def test_connect_time_appearing_later_is_not_a_reconnect(self, tracker, fakeClock):
        state = tracker.applySuccess(
            tracker.initialState(), [osSession(4, "dave", connectTime=None)]
        ).state
        connect = fakeClock.now() - timedelta(minutes=1)
        outcome = poll(tracker, fakeClock, state, [osSession(4, "dave", connectTime=connect)])
        assert outcome.events == []
        assert outcome.state.sessions[4].connectTime == connect
        outcome = poll(tracker, fakeClock, outcome.state, [osSession(4, "dave", connectTime=None)])
        assert outcome.events == []
        assert outcome.state.sessions[4].connectTime is None


class TestAc10LogoffOfDisconnected:
    def test_removed_without_event(self, tracker, fakeClock):
        t = fakeClock.now()
        state = trackerState(
            [
                trackedDisconnected(2, "alice", disconnectedSince=t - timedelta(minutes=9)),
                trackedActive(
                    3, "bob", connectTime=t - timedelta(minutes=8), startEventTimestamp=t
                ),
            ],
            t,
        )
        bob = osSession(3, "bob", connectTime=t - timedelta(minutes=8))
        outcome = poll(tracker, fakeClock, state, [bob])
        assert outcome.events == []
        assert list(outcome.state.sessions) == [3]
        assert outcome.state.status is PcStatus.IN_USE


class TestAc12ConsoleLogon:
    def test_console_logon_starts_console_period(self, tracker, fakeClock):
        state = tracker.applySuccess(tracker.initialState(), []).state
        assert state.status is PcStatus.AVAILABLE
        outcome = poll(tracker, fakeClock, state, [osSession(1, "erin", "Active", "Console")])
        assert outcome.state.status is PcStatus.IN_USE
        tracked = outcome.state.sessions[1]
        assert tracked.connectionType is ConnectionType.CONSOLE
        assert (tracked.clientName, tracked.clientAddress) == (None, None)
        assert len(outcome.events) == 1
        assert outcome.events[0].eventType is EventType.SESSION_START
        assert outcome.events[0].connectionType is ConnectionType.CONSOLE
        assert outcome.events[0].reason is EventReason.OBSERVED


class TestAc13RdpResumedAtConsole:
    def test_new_console_period_and_no_end_for_old(self, tracker, fakeClock):
        c1 = fakeClock.now() - timedelta(minutes=30)
        state = tracker.applySuccess(
            tracker.initialState(), [osSession(2, "alice", connectTime=c1)]
        ).state
        u1 = state.sessions[2].openPeriod.usagePeriodId
        disconnect = poll(
            tracker, fakeClock, state, [osSession(2, "alice", "Disconnected", connectTime=c1)]
        )
        assert [e.usagePeriodId for e in disconnect.events] == [u1]

        c2 = fakeClock.now() + timedelta(seconds=2)
        outcome = poll(
            tracker,
            fakeClock,
            disconnect.state,
            [osSession(2, "alice", "Active", "Console", connectTime=c2)],
        )
        assert len(outcome.events) == 1
        start = outcome.events[0]
        assert start.eventType is EventType.SESSION_START
        assert start.usagePeriodId != u1
        assert start.connectionType is ConnectionType.CONSOLE
        assert start.sessionId == 2
        assert outcome.state.sessions[2].connectionType is ConnectionType.CONSOLE
        assert activityDocumentProblems(disconnect.events + outcome.events) == []


class TestPreRefreshSessionOnEnd:
    """Critic TASK-003 S5: the old period's end records the old client, not the new one."""

    def test_fast_reconnect_from_another_client(self, tracker, fakeClock):
        c1 = fakeClock.now() - timedelta(minutes=30)
        old = osSession(2, "alice", clientName="OLD-PC", clientAddress="10.0.0.1", connectTime=c1)
        state = tracker.applySuccess(tracker.initialState(), [old]).state
        c2 = fakeClock.now() + timedelta(seconds=2)
        new = osSession(2, "alice", clientName="NEW-PC", clientAddress="10.0.0.2", connectTime=c2)

        outcome = poll(tracker, fakeClock, state, [new])

        end, start = outcome.events
        assert end.eventType is EventType.SESSION_END
        assert (end.clientName, end.clientAddress, end.connectTime) == ("OLD-PC", "10.0.0.1", c1)
        assert start.eventType is EventType.SESSION_START
        assert (start.clientName, start.clientAddress, start.connectTime) == (
            "NEW-PC",
            "10.0.0.2",
            c2,
        )
        assert outcome.state.sessions[2].clientName == "NEW-PC"

    def test_disconnect_end_keeps_client_seen_while_active(self, tracker, fakeClock):
        c1 = fakeClock.now() - timedelta(minutes=30)
        old = osSession(2, "alice", clientName="OLD-PC", clientAddress="10.0.0.1", connectTime=c1)
        state = tracker.applySuccess(tracker.initialState(), [old]).state
        gone = osSession(2, "alice", "Disconnected", clientName=None, clientAddress=None)
        end = poll(tracker, fakeClock, state, [gone]).events[0]
        assert (end.clientName, end.clientAddress) == ("OLD-PC", "10.0.0.1")


class TestFirstSeenDisconnected:
    def test_tracked_without_event_with_disconnected_since(self, tracker, fakeClock):
        t = fakeClock.now()
        d = t - timedelta(minutes=3)
        outcome = tracker.applySuccess(
            tracker.initialState(),
            [
                osSession(
                    2,
                    "alice",
                    "Disconnected",
                    connectTime=t - timedelta(minutes=9),
                    disconnectTime=d,
                )
            ],
        )
        assert outcome.events == []
        tracked = outcome.state.sessions[2]
        assert tracked.state is SessionState.DISCONNECTED
        assert tracked.openPeriod is None
        assert tracked.disconnectedSince == d
        assert tracked.firstDetectedAt == t
        assert outcome.state.status is PcStatus.AVAILABLE

    def test_future_disconnect_time_falls_back_to_detection_time(self, tracker, fakeClock):
        t = fakeClock.now()
        outcome = tracker.applySuccess(
            tracker.initialState(),
            [osSession(2, "alice", "Disconnected", disconnectTime=t + timedelta(hours=1))],
        )
        assert outcome.state.sessions[2].disconnectedSince == t

    def test_g4_every_start_timestamp_equals_period_start_event_timestamp(self, tracker, fakeClock):
        outcome = tracker.applySuccess(
            tracker.initialState(), [osSession(2, "alice"), osSession(3, "bob", connectTime=None)]
        )
        for start in starts(outcome):
            period = outcome.state.sessions[start.sessionId].openPeriod
            assert period.startEventTimestamp == start.timestamp == fakeClock.now()
        assert ends(outcome) == []
