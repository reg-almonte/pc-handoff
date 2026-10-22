"""Startup reconciliation, pure part (TASK-008; REQ AC-17, AC-19, AC-20, AC-27 logic).

Times: detection time R = 09:00:00+09:00 (``fakeClock``), P = 08:50, alice's period U1
started at C1 = 08:30 with start-event timestamp S1 = 08:30:02. Every emitted batch is run
through ``activityDocumentProblems`` (decisions.log F3).
"""

import dataclasses
import logging
import random
import uuid
from datetime import datetime, timedelta

import pytest

from pc_handoff.app_logging import LOGGER_NAME
from pc_handoff.detection.provider import ConnectionType
from pc_handoff.domain.models import (
    EndCause,
    EventReason,
    EventType,
    PcStatus,
    SessionState,
    StatusSession,
)
from pc_handoff.domain.reconciliation import (
    OpenPeriodRecord,
    Reconciler,
    openPeriodsFromEvents,
    openPeriodsFromStatus,
    recordFromStartEvent,
    recordFromStatusSession,
    trackedFromStatusSession,
)
from pc_handoff.domain.tracker import SessionTracker
from tests.conftest import FAKE_CLOCK_START
from tests.fakes.activity_checks import activityDocumentProblems
from tests.fakes.builders import osSession
from tests.fakes.fake_session_provider import DEFAULT_LOGON_TIME
from tests.fakes.log_factory import endEvent, startEvent

R = FAKE_CLOCK_START
P = R - timedelta(minutes=10)
C1 = R - timedelta(minutes=30)
S1 = C1 + timedelta(seconds=2)
RDP = ConnectionType.RDP
CONSOLE = ConnectionType.CONSOLE
UNSET = object()


def record(
    sessionId: int = 2,
    user: str = "alice",
    *,
    connectTime: datetime | None = C1,
    usagePeriodStart: datetime | None = None,
    startEventTimestamp: datetime | None | object = UNSET,
    connectionType: ConnectionType = RDP,
    logonTime: datetime | None = DEFAULT_LOGON_TIME,
    domain: str | None = "CORP",
    durationApproximate: bool | None = None,
    usagePeriodId: str | None = None,
) -> OpenPeriodRecord:
    """A source-1 style record: the period's connect time is its start event's connectTime."""
    start = usagePeriodStart or connectTime
    assert start is not None
    s = start + timedelta(seconds=2) if startEventTimestamp is UNSET else startEventTimestamp
    return OpenPeriodRecord(
        usagePeriodId=usagePeriodId or str(uuid.uuid4()),
        sessionId=sessionId,
        userName=user,
        domain=domain,
        logonTime=logonTime,
        connectTime=connectTime,
        connectionType=connectionType,
        usagePeriodStart=start,
        durationApproximate=(connectTime is None)
        if durationApproximate is None
        else durationApproximate,
        startEventTimestamp=s,  # type: ignore[arg-type]
        clientName="CLIENT-PC",
        clientAddress="192.168.1.20",
        connectTimeAtStart=connectTime,
    )


@pytest.fixture
def reconciler(tracker: SessionTracker) -> Reconciler:
    return Reconciler(tracker)


def ends(events):
    return [e for e in events if e.eventType is EventType.SESSION_END]


def starts(events):
    return [e for e in events if e.eventType is EventType.SESSION_START]


def run(reconciler, records, snapshot, *, p=P, statusSessions=(), detectionTime=R):
    outcome = reconciler.reconcile(records, p, statusSessions, snapshot, detectionTime)
    assert activityDocumentProblems(outcome.events) == []
    return outcome


def warnings(lines):
    return [line["message"] for line in lines() if line["level"] == "WARNING"]


# --- AC-1 / AC-17: corrupt status.json, periods from the log, P unknown --------------------


class TestAc01SourceOneWithoutP:
    def test_alice_continues_and_bob_is_closed_at_his_start_event(self, reconciler):
        u1 = record(2, "alice")
        b2 = R - timedelta(hours=2)
        s2 = b2 + timedelta(seconds=3)
        u2 = record(3, "bob", connectTime=b2, startEventTimestamp=s2)

        outcome = run(reconciler, [u1, u2], [osSession(2, "alice", connectTime=C1)], p=None)

        assert starts(outcome.events) == []
        [end] = outcome.events
        assert end.usagePeriodId == u2.usagePeriodId
        assert end.eventType is EventType.SESSION_END
        assert end.reason is EventReason.DETECTED_AT_STARTUP
        assert end.endCause is EndCause.LOGOFF
        assert end.durationApproximate is True
        assert end.periodEnd == s2
        assert end.durationSeconds == int((s2 - b2).total_seconds())
        assert end.timestamp == R
        alice = outcome.state.sessions[2]
        assert alice.openPeriod is not None
        assert alice.openPeriod.usagePeriodId == u1.usagePeriodId
        assert alice.openPeriod.usagePeriodStart == C1
        assert set(outcome.state.sessions) == {2}
        assert outcome.state.status is PcStatus.IN_USE


# --- AC-4 / AC-19: restart keeps the ongoing period -----------------------------------------


class TestAc04MatchingAtRestart:
    def test_exact_period_matched_by_identity_and_connect_time(self, reconciler):
        u1 = record()
        outcome = run(reconciler, [u1], [osSession(2, "alice", connectTime=C1)])

        assert outcome.events == []
        period = outcome.state.sessions[2].openPeriod
        assert period is not None
        assert (period.usagePeriodId, period.usagePeriodStart) == (u1.usagePeriodId, C1)
        assert period.durationApproximate is False
        assert period.startEventTimestamp == u1.startEventTimestamp
        assert period.connectTimeAtStart == C1
        assert outcome.state.sessions[2].state is SessionState.ACTIVE
        assert outcome.state.detectionHealthy is True
        assert outcome.state.lastSuccessfulPollAt == R

    @pytest.mark.parametrize(
        "snapshotConnect", [C1, C1 + timedelta(minutes=5), None], ids=["same", "other", "none"]
    )
    def test_approximate_period_matched_by_identity_alone(self, reconciler, snapshotConnect):
        start = R - timedelta(minutes=40)
        u1 = record(connectTime=None, usagePeriodStart=start)
        outcome = run(reconciler, [u1], [osSession(2, "alice", connectTime=snapshotConnect)])

        assert outcome.events == []
        period = outcome.state.sessions[2].openPeriod
        assert period is not None
        assert period.usagePeriodId == u1.usagePeriodId
        assert period.usagePeriodStart == start
        assert period.durationApproximate is True

    def test_snapshot_without_connect_time_matches_an_exact_period(self, reconciler):
        u1 = record()
        outcome = run(reconciler, [u1], [osSession(2, "alice", connectTime=None)])
        assert outcome.events == []
        assert outcome.state.sessions[2].openPeriod.usagePeriodId == u1.usagePeriodId

    def test_different_connect_time_is_a_reconnect_at_startup(self, reconciler):
        u1 = record()
        c2 = R - timedelta(minutes=5)  # after P, so it is accepted
        outcome = run(reconciler, [u1], [osSession(2, "alice", connectTime=c2)])

        end, start = outcome.events
        assert end.usagePeriodId == u1.usagePeriodId
        assert end.endCause is EndCause.RECONNECT_DETECTED
        assert end.reason is EventReason.DETECTED_AT_STARTUP
        assert end.durationApproximate is True
        assert end.periodEnd == P  # max(P, S1); no OS disconnect time
        assert start.eventType is EventType.SESSION_START
        assert start.usagePeriodId != u1.usagePeriodId
        assert start.reason is EventReason.DETECTED_AT_STARTUP
        assert start.usagePeriodStart == c2
        assert start.durationApproximate is False
        assert outcome.state.sessions[2].openPeriod.usagePeriodId == start.usagePeriodId

    def test_reconnect_accepts_os_disconnect_time_in_window_c1_c2(self, reconciler):
        u1 = record()
        c2 = R - timedelta(minutes=5)
        d = c2 - timedelta(minutes=1)
        outcome = run(reconciler, [u1], [osSession(2, "alice", connectTime=c2, disconnectTime=d)])
        assert outcome.events[0].periodEnd == d
        assert outcome.events[1].usagePeriodStart == c2

    def test_different_domain_is_not_the_same_identity(self, reconciler):
        u1 = record()
        outcome = run(reconciler, [u1], [osSession(2, "alice", connectTime=C1, domain="OTHER")])
        end, start = outcome.events
        assert end.endCause is EndCause.LOGOFF
        assert start.reason is EventReason.DETECTED_AT_STARTUP

    def test_user_name_compared_case_insensitively(self, reconciler):
        u1 = record(user="Alice")
        outcome = run(reconciler, [u1], [osSession(2, "ALICE", connectTime=C1)])
        assert outcome.events == []

    def test_different_logon_time_is_identity_replaced(self, reconciler):
        u1 = record()
        newLogon = DEFAULT_LOGON_TIME + timedelta(minutes=50)
        outcome = run(
            reconciler,
            [u1],
            [osSession(2, "alice", connectTime=R - timedelta(minutes=1), logonTime=newLogon)],
        )
        end, start = outcome.events
        assert end.endCause is EndCause.LOGOFF
        assert end.periodEnd == P
        assert start.usagePeriodStart == R - timedelta(minutes=1)

    def test_unknown_logon_time_matches_and_logs_r10(self, reconciler, capturedAppLog):
        u1 = record(logonTime=None)
        outcome = run(reconciler, [u1], [osSession(2, "alice", connectTime=C1)])
        assert outcome.events == []
        messages = [line["message"] for line in capturedAppLog()]
        assert "identity matched without logonTime for session 2" in messages


# --- AC-5 / AC-20: changes during downtime under the blind rule -----------------------------


class TestAc05ChangesDuringDowntime:
    CB = R - timedelta(hours=3)
    SB = CB + timedelta(seconds=2)
    CD = R - timedelta(hours=1)
    SD = CD + timedelta(seconds=2)

    def scenario(self, reconciler, *, d, p=P):
        bob = record(3, "bob", connectTime=self.CB, startEventTimestamp=self.SB)
        dave = record(4, "dave", connectTime=self.CD, startEventTimestamp=self.SD)
        snapshot = [
            osSession(4, "dave", "Disconnected", connectTime=self.CD, disconnectTime=d),
            osSession(5, "carol", connectTime=R - timedelta(minutes=3)),
        ]
        return bob, dave, run(reconciler, [bob, dave], snapshot, p=p)

    def test_bob_logoff_dave_disconnect_carol_start(self, reconciler):
        d = R - timedelta(minutes=20)
        bob, dave, outcome = self.scenario(reconciler, d=d)

        bobEnd, daveEnd, carolStart = outcome.events
        assert (bobEnd.sessionId, daveEnd.sessionId, carolStart.sessionId) == (3, 4, 5)
        assert bobEnd.usagePeriodId == bob.usagePeriodId
        assert bobEnd.endCause is EndCause.LOGOFF
        assert bobEnd.periodEnd == max(P, self.SB)
        assert bobEnd.durationSeconds == int((bobEnd.periodEnd - self.CB).total_seconds())
        assert daveEnd.usagePeriodId == dave.usagePeriodId
        assert daveEnd.endCause is EndCause.DISCONNECT
        assert daveEnd.periodEnd == d
        assert daveEnd.durationSeconds == int((d - self.CD).total_seconds())
        for end in (bobEnd, daveEnd):
            assert end.reason is EventReason.DETECTED_AT_STARTUP
            assert end.durationApproximate is True
        assert carolStart.eventType is EventType.SESSION_START
        assert carolStart.reason is EventReason.DETECTED_AT_STARTUP
        assert carolStart.userName == "carol"

        state = outcome.state
        assert state.status is PcStatus.IN_USE
        assert state.sessions[4].state is SessionState.DISCONNECTED
        assert state.sessions[4].disconnectedSince == d
        assert state.sessions[4].openPeriod is None
        assert 3 not in state.sessions

    @pytest.mark.parametrize(
        "d",
        [None, CD - timedelta(minutes=1), R + timedelta(minutes=1)],
        ids=["absent", "before-window", "after-window"],
    )
    def test_dave_without_accepted_disconnect_time_uses_fallback(self, reconciler, d):
        _, _, outcome = self.scenario(reconciler, d=d)
        daveEnd = outcome.events[1]
        assert daveEnd.periodEnd == max(P, self.SD)
        assert daveEnd.endCause is EndCause.DISCONNECT
        # I2: disconnectedSince uses the observed window, so it is the detection time here.
        assert outcome.state.sessions[4].disconnectedSince == R

    def test_start_event_after_p_wins_the_fallback(self, reconciler):
        _, _, outcome = self.scenario(reconciler, d=None, p=self.SD - timedelta(minutes=5))
        assert outcome.events[0].periodEnd == self.SD - timedelta(minutes=5)  # P > Sb
        assert outcome.events[1].periodEnd == self.SD  # Sd > P


# --- AC-8: transitional session at the first poll ------------------------------------------


class TestAc08Transitional:
    def test_same_identity_transitional_keeps_period_open(self, reconciler, tracker, fakeClock):
        u1 = record()
        outcome = run(reconciler, [u1], [osSession(2, "alice", "ConnectQuery", connectTime=C1)])

        assert outcome.events == []
        alice = outcome.state.sessions[2]
        assert alice.state is SessionState.ACTIVE
        assert alice.openPeriod.usagePeriodId == u1.usagePeriodId
        assert alice.consecutiveTransitionalPolls == 1
        assert outcome.state.status is PcStatus.IN_USE

        fakeClock.advance(5)
        later = tracker.applySuccess(outcome.state, [osSession(2, "alice", connectTime=C1)])
        assert later.events == []
        assert later.state.sessions[2].openPeriod.usagePeriodId == u1.usagePeriodId
        assert later.state.sessions[2].consecutiveTransitionalPolls == 0

    def test_transitional_with_another_user_ends_the_period(self, reconciler):
        u1 = record()
        outcome = run(reconciler, [u1], [osSession(2, "bob", "Shadow")])
        [end] = outcome.events
        assert end.endCause is EndCause.LOGOFF
        assert outcome.state.sessions == {}

    def test_unknown_transitional_session_is_ignored(self, reconciler):
        outcome = run(reconciler, [], [osSession(7, "erin", "Connected")])
        assert outcome.events == []
        assert outcome.state.sessions == {}
        assert outcome.state.status is PcStatus.AVAILABLE


# --- AC-11 / I3: connection-type change at startup ------------------------------------------


class TestAc11TypeChangeAtStartup:
    def test_type_change_with_new_connect_time_starts_at_c2(self, reconciler):
        u1 = record(connectionType=RDP)
        c2 = R - timedelta(minutes=2)
        outcome = run(
            reconciler, [u1], [osSession(2, "alice", connectionType="Console", connectTime=c2)]
        )
        end, start = outcome.events
        assert end.endCause is EndCause.RECONNECT_DETECTED
        assert end.reason is EventReason.DETECTED_AT_STARTUP
        assert end.connectionType is RDP
        assert start.connectionType is CONSOLE
        assert start.reason is EventReason.DETECTED_AT_STARTUP
        assert start.usagePeriodId != u1.usagePeriodId
        assert start.usagePeriodStart == c2
        assert start.durationApproximate is False

    @pytest.mark.parametrize("snapshotConnect", [C1, None], ids=["unchanged", "missing"])
    def test_type_change_with_unchanged_connect_time_applies_i1(self, reconciler, snapshotConnect):
        u1 = record(connectionType=RDP)
        d = R - timedelta(minutes=1)  # inside [C1, R]: I1 makes R the window's upper bound
        snapshot = [
            osSession(
                2,
                "alice",
                connectionType="Console",
                connectTime=snapshotConnect,
                disconnectTime=d,
            )
        ]
        outcome = run(reconciler, [u1], snapshot)
        end, start = outcome.events
        assert end.endCause is EndCause.RECONNECT_DETECTED
        assert end.periodEnd == d
        assert start.connectionType is CONSOLE
        assert start.usagePeriodStart == R
        assert start.durationApproximate is True
        assert start.connectTime is None

    def test_type_change_of_an_approximate_period_is_not_matched(self, reconciler):
        u1 = record(connectTime=None, usagePeriodStart=R - timedelta(hours=1))
        outcome = run(
            reconciler, [u1], [osSession(2, "alice", connectionType="Console", connectTime=C1)]
        )
        end, start = outcome.events
        assert end.endCause is EndCause.RECONNECT_DETECTED
        assert start.usagePeriodStart == R  # I1: the connect time is not comparable
        assert start.durationApproximate is True


# --- S1: source-2 periods are matched on the period's own connect time -------------------


def sourceTwoEntry(*, connectTime, approximate, usagePeriodStart=C1):
    return StatusSession(
        sessionId=2,
        userName="alice",
        domain="CORP",
        state=SessionState.ACTIVE,
        connectionType=RDP,
        clientName="CLIENT-PC",
        clientAddress="192.168.1.20",
        logonTime=DEFAULT_LOGON_TIME,
        firstDetectedAt=None,
        connectTime=connectTime,
        usagePeriodId=str(uuid.uuid4()),
        usagePeriodStart=usagePeriodStart,
        durationApproximate=approximate,
        disconnectedSince=None,
    )


class TestS1SourceTwoMatchBasis:
    def test_exact_period_with_null_connect_time_detects_a_downtime_reconnect(self, reconciler):
        """The last poll before the stop got no connect time (M3); alice reconnected at C2
        while the app was down. Same result as the log-sourced copy of the period."""
        entry = sourceTwoEntry(connectTime=None, approximate=False)
        c2 = R - timedelta(minutes=5)
        snapshot = [osSession(2, "alice", connectTime=c2)]
        fromStatus = run(reconciler, [recordFromStatusSession(entry)], snapshot)
        fromLog = run(
            reconciler,
            [record(connectTime=C1, usagePeriodId=entry.usagePeriodId)],
            snapshot,
        )
        for outcome in (fromStatus, fromLog):
            end, start = outcome.events
            assert end.usagePeriodId == entry.usagePeriodId
            assert end.endCause is EndCause.RECONNECT_DETECTED
            assert end.periodEnd == P
            assert start.usagePeriodStart == c2
        assert fromStatus.events[0].connectTime == fromLog.events[0].connectTime == C1

    def test_approximate_period_matches_by_identity_alone(self, reconciler):
        """An I1/I4 period: status.json holds the session's old connect time C1, but the
        period itself has none, so any snapshot connect time matches (AC-19)."""
        start = R - timedelta(minutes=20)
        entry = sourceTwoEntry(connectTime=C1, approximate=True, usagePeriodStart=start)
        snapshot = [osSession(2, "alice", connectTime=C1 + timedelta(minutes=3))]
        outcome = run(reconciler, [recordFromStatusSession(entry)], snapshot)
        assert outcome.events == []
        period = outcome.state.sessions[2].openPeriod
        assert (period.usagePeriodId, period.usagePeriodStart) == (entry.usagePeriodId, start)
        assert period.durationApproximate is True


# --- R1, ordering, unknown P/S, clamps ----------------------------------------------------


class TestEdgeRules:
    @pytest.mark.parametrize("user", [None, ""], ids=["none", "empty"])
    def test_empty_user_name_closes_the_period_as_gone(self, reconciler, user):
        u1 = record()
        outcome = run(reconciler, [u1], [osSession(2, user, connectTime=C1)])
        [end] = outcome.events
        assert end.endCause is EndCause.LOGOFF
        assert end.reason is EventReason.DETECTED_AT_STARTUP
        assert end.periodEnd == P
        assert outcome.state.sessions == {}
        assert outcome.state.status is PcStatus.AVAILABLE

    def test_events_are_ordered_by_session_id(self, reconciler):
        records = [record(9, "zed"), record(3, "bob")]
        snapshot = [osSession(5, "carol"), osSession(1, "amy")]
        outcome = run(reconciler, records, snapshot)
        assert [e.sessionId for e in outcome.events] == [1, 3, 5, 9]

    def test_source_two_period_end_timestamp_is_detection_time(self, reconciler):
        u1 = record(startEventTimestamp=None)
        outcome = run(reconciler, [u1], [])
        [end] = outcome.events
        assert end.timestamp == R
        assert end.periodEnd == P

    def test_nothing_known_gives_null_period_end(self, reconciler):
        u1 = record(startEventTimestamp=None)
        outcome = run(reconciler, [u1], [], p=None)
        [end] = outcome.events
        assert end.periodEnd is None
        assert end.durationSeconds is None
        assert 2 not in outcome.state.lastPeriodEndBySessionId

    def test_p_before_period_start_is_clamped_never_negative(self, reconciler, capturedAppLog):
        """TASK-005 S10 / validator difference #2: periodEnd < usagePeriodStart never escapes."""
        start = P + timedelta(minutes=5)
        u1 = record(connectTime=start, startEventTimestamp=None)
        outcome = run(reconciler, [u1], [])
        [end] = outcome.events
        assert end.periodEnd == start
        assert end.durationSeconds == 0
        assert any("wall clock moved backwards" in m for m in warnings(capturedAppLog))

    def test_backward_clock_step_clamps_end_timestamp_to_s(self, reconciler, capturedAppLog):
        """AC-10 (pure part): R is 600 s before S."""
        s = R + timedelta(seconds=600)
        u1 = record(connectTime=s - timedelta(seconds=2), startEventTimestamp=s)
        outcome = run(reconciler, [u1], [], p=s + timedelta(seconds=10))
        [end] = outcome.events
        assert end.timestamp == s
        assert end.periodEnd >= end.usagePeriodStart
        assert end.durationSeconds >= 0
        assert any("wall clock moved backwards by 600 s" in m for m in warnings(capturedAppLog))


# --- constraint 4 (G4) and 5 (N1) -----------------------------------------------------------


class TestStartTimestampsG4:
    def test_rebuilt_period_keeps_the_logged_start_timestamp(self, reconciler):
        start = startEvent(timestamp=S1, usagePeriodStart=C1, sessionId=2, userName="alice")
        [rec] = openPeriodsFromEvents([start])
        outcome = run(
            reconciler, [rec], [osSession(2, "alice", connectTime=C1, logonTime=start.logonTime)]
        )
        assert outcome.events == []
        assert outcome.state.sessions[2].openPeriod.startEventTimestamp == start.timestamp

    def test_every_new_start_has_s_equal_to_its_timestamp(self, reconciler):
        u1 = record()
        snapshot = [
            osSession(2, "alice", connectTime=R - timedelta(minutes=1)),
            osSession(3, "bob", connectTime=None),
        ]
        outcome = run(reconciler, [u1], snapshot)
        newStarts = starts(outcome.events)
        assert len(newStarts) == 2
        for event in newStarts:
            period = outcome.state.sessions[event.sessionId].openPeriod
            assert period.usagePeriodId == event.usagePeriodId
            assert period.startEventTimestamp == event.timestamp == R


class TestN1SeedingAndBound:
    def test_closed_period_ends_seed_the_session_map(self, reconciler):
        bob = record(3, "bob", connectTime=C1)
        dave = record(4, "dave", connectTime=C1)
        alice = record(2, "alice")
        d = R - timedelta(minutes=4)
        snapshot = [
            osSession(2, "alice", connectTime=C1),
            osSession(4, "dave", "Disconnected", connectTime=C1, disconnectTime=d),
        ]
        outcome = run(reconciler, [alice, bob, dave], snapshot)
        assert outcome.state.lastPeriodEndBySessionId == {3: P, 4: d}

    def test_seeded_map_holds_the_clamped_period_end(self, reconciler):
        start = P + timedelta(minutes=5)
        u1 = record(connectTime=start, startEventTimestamp=None)
        outcome = run(reconciler, [u1], [])
        assert outcome.state.lastPeriodEndBySessionId == {2: start}

    def test_new_logon_connect_time_before_the_closed_end_is_not_reported(
        self, reconciler, capturedAppLog
    ):
        """I4/N1 applied to detected_at_startup starts: carol reuses bob's sessionId."""
        bob = record(3, "bob", connectTime=C1)
        stale = P - timedelta(minutes=1)  # earlier than bob's end max(P, Sb) = P
        outcome = run(reconciler, [bob], [osSession(3, "carol", connectTime=stale)])
        end, start = outcome.events
        assert end.periodEnd == P
        assert start.usagePeriodStart == R
        assert start.durationApproximate is True
        assert start.connectTime is None
        assert any("(I4)" in m for m in warnings(capturedAppLog))

    def test_new_logon_connect_time_after_the_closed_end_is_kept(self, reconciler):
        bob = record(3, "bob", connectTime=C1)
        fresh = P + timedelta(minutes=1)
        outcome = run(reconciler, [bob], [osSession(3, "carol", connectTime=fresh)])
        assert outcome.events[1].usagePeriodStart == fresh
        assert outcome.events[1].durationApproximate is False

    def test_reconnect_connect_time_before_the_blind_end_is_not_reported(self, reconciler):
        """I4 on the reconnect path: C2 earlier than max(P, S) cannot be the new connection."""
        u1 = record()
        c2 = P - timedelta(minutes=1)
        outcome = run(reconciler, [u1], [osSession(2, "alice", connectTime=c2)])
        end, start = outcome.events
        assert end.periodEnd == P
        assert start.usagePeriodStart == R
        assert start.durationApproximate is True

    def test_tracker_uses_the_seeded_map_on_later_polls(self, reconciler, tracker, fakeClock):
        """bob's period is closed at startup (end P); carol appears on his sessionId one poll
        later with a connect time before P. Only the seeded map can reject it (R6 + N1)."""
        bob = record(3, "bob")
        outcome = run(reconciler, [bob], [])
        assert outcome.state.lastPeriodEndBySessionId == {3: P}

        fakeClock.advance(5)
        later = tracker.applySuccess(
            outcome.state, [osSession(3, "carol", connectTime=P - timedelta(minutes=1))]
        )
        [start] = later.events
        assert start.usagePeriodStart == fakeClock.now()
        assert start.durationApproximate is True


# --- constraint 6: in-memory TrackedSession fields ------------------------------------------


class TestInMemoryFieldSeeding:
    def test_last_known_connect_time_seeded_from_the_period(self, reconciler, tracker, fakeClock):
        u1 = record()
        outcome = run(reconciler, [u1], [osSession(2, "alice", connectTime=None)])
        alice = outcome.state.sessions[2]
        assert alice.connectTime is None
        assert alice.lastKnownConnectTime == C1

        # M3 across a restart: C1 (log) -> None (first poll) -> C2 is one reconnect.
        fakeClock.advance(5)
        c2 = fakeClock.now() - timedelta(seconds=2)
        later = tracker.applySuccess(outcome.state, [osSession(2, "alice", connectTime=c2)])
        end, start = later.events
        assert end.endCause is EndCause.RECONNECT_DETECTED
        assert end.reason is EventReason.OBSERVED
        assert start.usagePeriodStart == c2

    def test_definite_sessions_get_no_blind_context(self, reconciler, tracker, fakeClock):
        """A restart whose first poll sees every session in a definite state applies the
        blind rule at startup, once; nothing is carried into later polls."""
        snapshot = [
            osSession(2, "alice", connectTime=C1),
            osSession(3, "bob", "Disconnected"),
            osSession(4, "carol"),
        ]
        outcome = run(reconciler, [record()], snapshot)
        assert outcome.state.outagePending is False
        for session in outcome.state.sessions.values():
            assert session.outagePriorPollAt is None
            assert session.blindReason is None

        fakeClock.advance(5)
        later = tracker.applySuccess(outcome.state, [osSession(3, "bob", "Disconnected")])
        for end in later.events:
            assert end.reason is EventReason.OBSERVED
            assert end.periodEnd == fakeClock.now()


class TestM1TransitionalAtStartupKeepsTheDowntimeContext:
    """Critic TASK-008 M1, the Critic's 8 h probe: down from P8 = R - 8 h; U1 started at
    C = P8 - 1 h with S = C + 2 s. The first poll after the restart reports alice
    transitional; the period is resolved one poll later, at R + 5 s."""

    P8 = R - timedelta(hours=8)
    C = P8 - timedelta(hours=1)
    S = C + timedelta(seconds=2)

    def startTransitional(self, reconciler, p=P8):
        u1 = record(connectTime=self.C, startEventTimestamp=self.S)
        transitional = [osSession(2, "alice", "ConnectQuery", connectTime=self.C)]
        outcome = run(reconciler, [u1], transitional, p=p)
        assert outcome.events == []
        return u1, outcome.state

    def nextPoll(self, tracker, fakeClock, state, snapshot):
        fakeClock.advance(5)
        outcome = tracker.applySuccess(state, snapshot)
        assert activityDocumentProblems(outcome.events) == []
        return outcome

    def assertStartupEnd(self, end, u1, cause, periodEnd):
        assert end.usagePeriodId == u1.usagePeriodId
        assert end.endCause is cause
        assert end.reason is EventReason.DETECTED_AT_STARTUP
        assert end.durationApproximate is True
        assert end.periodEnd == periodEnd
        assert end.durationSeconds == int((periodEnd - self.C).total_seconds())

    def test_context_is_seeded_with_p_and_the_startup_reason(self, reconciler):
        _, state = self.startTransitional(reconciler)
        alice = state.sessions[2]
        assert alice.outagePriorPollAt == self.P8
        assert alice.blindReason is EventReason.DETECTED_AT_STARTUP
        assert state.outagePending is False

    def test_gone_next_poll_is_a_startup_end_at_p(self, reconciler, tracker, fakeClock):
        u1, state = self.startTransitional(reconciler)
        [end] = self.nextPoll(tracker, fakeClock, state, []).events
        self.assertStartupEnd(end, u1, EndCause.LOGOFF, self.P8)
        assert end.durationSeconds == 3600  # the probe recorded 32405 s before the fix

    def test_disconnected_without_os_time_is_a_startup_end_at_p(
        self, reconciler, tracker, fakeClock
    ):
        u1, state = self.startTransitional(reconciler)
        snapshot = [osSession(2, "alice", "Disconnected", connectTime=self.C)]
        outcome = self.nextPoll(tracker, fakeClock, state, snapshot)
        [end] = outcome.events
        self.assertStartupEnd(end, u1, EndCause.DISCONNECT, self.P8)
        assert end.durationSeconds == 3600
        alice = outcome.state.sessions[2]
        assert alice.disconnectedSince == fakeClock.now()  # I2: observed window, no OS time
        assert (alice.outagePriorPollAt, alice.blindReason) == (None, None)

    def test_disconnected_with_accepted_os_time_ends_there(self, reconciler, tracker, fakeClock):
        u1, state = self.startTransitional(reconciler)
        d = self.P8 + timedelta(hours=2)
        snapshot = [osSession(2, "alice", "Disconnected", connectTime=self.C, disconnectTime=d)]
        [end] = self.nextPoll(tracker, fakeClock, state, snapshot).events
        self.assertStartupEnd(end, u1, EndCause.DISCONNECT, d)

    def test_reconnect_ends_at_p_not_at_the_detection_time(self, reconciler, tracker, fakeClock):
        u1, state = self.startTransitional(reconciler)
        c2 = R
        snapshot = [osSession(2, "alice", connectTime=c2)]
        outcome = self.nextPoll(tracker, fakeClock, state, snapshot)
        end, start = outcome.events
        self.assertStartupEnd(end, u1, EndCause.RECONNECT_DETECTED, self.P8)
        assert start.reason is EventReason.DETECTED_AT_STARTUP
        assert start.usagePeriodStart == c2
        alice = outcome.state.sessions[2]
        assert (alice.outagePriorPollAt, alice.blindReason) == (None, None)

    def test_identity_replaced_is_a_startup_end(self, reconciler, tracker, fakeClock):
        u1, state = self.startTransitional(reconciler)
        [end, start] = self.nextPoll(tracker, fakeClock, state, [osSession(2, "bob")]).events
        self.assertStartupEnd(end, u1, EndCause.LOGOFF, self.P8)
        assert start.reason is EventReason.DETECTED_AT_STARTUP

    def test_active_with_the_same_connection_clears_the_context(
        self, reconciler, tracker, fakeClock
    ):
        u1, state = self.startTransitional(reconciler)
        same = [osSession(2, "alice", connectTime=self.C)]
        outcome = self.nextPoll(tracker, fakeClock, state, same)
        assert outcome.events == []
        alice = outcome.state.sessions[2]
        assert alice.openPeriod.usagePeriodId == u1.usagePeriodId
        assert (alice.outagePriorPollAt, alice.blindReason) == (None, None)

        [end] = self.nextPoll(tracker, fakeClock, outcome.state, []).events
        assert end.reason is EventReason.OBSERVED
        assert end.periodEnd == fakeClock.now()

    def test_context_survives_further_transitional_polls_and_an_outage(
        self, reconciler, tracker, fakeClock
    ):
        u1, state = self.startTransitional(reconciler)
        transitional = [osSession(2, "alice", "Shadow", connectTime=self.C)]
        state = self.nextPoll(tracker, fakeClock, state, transitional).state
        state = tracker.applyFailure(state, RuntimeError("down")).state
        state = self.nextPoll(tracker, fakeClock, state, transitional).state
        assert state.sessions[2].outagePriorPollAt == self.P8
        assert state.sessions[2].blindReason is EventReason.DETECTED_AT_STARTUP
        [end] = self.nextPoll(tracker, fakeClock, state, []).events
        self.assertStartupEnd(end, u1, EndCause.LOGOFF, self.P8)

    def test_unknown_p_falls_back_to_the_start_event(self, reconciler, tracker, fakeClock):
        u1, state = self.startTransitional(reconciler, p=None)
        assert state.sessions[2].outagePriorPollAt is None
        assert state.sessions[2].blindReason is EventReason.DETECTED_AT_STARTUP
        [end] = self.nextPoll(tracker, fakeClock, state, []).events
        self.assertStartupEnd(end, u1, EndCause.LOGOFF, self.S)


# --- firstDetectedAt carried from status.json (R10) ----------------------------------------


def statusEntry(sessionId=2, user="alice", *, state="Active", firstDetectedAt, logonTime=None):
    active = state == "Active"
    return StatusSession(
        sessionId=sessionId,
        userName=user,
        domain="CORP",
        state=SessionState(state),
        connectionType=RDP,
        clientName=None,
        clientAddress=None,
        logonTime=DEFAULT_LOGON_TIME if logonTime is None else logonTime,
        firstDetectedAt=firstDetectedAt,
        connectTime=C1,
        usagePeriodId=str(uuid.uuid4()) if active else None,
        usagePeriodStart=C1 if active else None,
        durationApproximate=False,
        disconnectedSince=None if active else C1,
    )


class TestFirstDetectedAt:
    F = R - timedelta(days=1)

    def test_matched_session_keeps_persisted_first_detected_at(self, reconciler):
        entry = statusEntry(firstDetectedAt=self.F)
        outcome = run(
            reconciler,
            [record()],
            [osSession(2, "alice", connectTime=C1)],
            statusSessions=(entry,),
        )
        assert outcome.state.sessions[2].firstDetectedAt == self.F

    def test_disconnected_session_keeps_persisted_first_detected_at(self, reconciler):
        entry = statusEntry(3, "bob", state="Disconnected", firstDetectedAt=self.F)
        outcome = run(
            reconciler, [], [osSession(3, "bob", "Disconnected")], statusSessions=(entry,)
        )
        assert outcome.state.sessions[3].firstDetectedAt == self.F

    def test_other_identity_does_not_inherit_it(self, reconciler):
        entry = statusEntry(
            firstDetectedAt=self.F, logonTime=DEFAULT_LOGON_TIME - timedelta(hours=5)
        )
        outcome = run(reconciler, [], [osSession(2, "alice")], statusSessions=(entry,))
        assert outcome.state.sessions[2].firstDetectedAt == R

    def test_other_domain_does_not_inherit_it(self, reconciler):
        entry = dataclasses.replace(statusEntry(firstDetectedAt=self.F), domain="OTHER")
        outcome = run(reconciler, [], [osSession(2, "alice")], statusSessions=(entry,))
        assert outcome.state.sessions[2].firstDetectedAt == R

    def test_null_persisted_value_falls_back_to_detection_time(self, reconciler):
        entry = statusEntry(firstDetectedAt=None)
        outcome = run(reconciler, [], [osSession(2, "alice")], statusSessions=(entry,))
        assert outcome.state.sessions[2].firstDetectedAt == R


# --- several open periods on one sessionId (lost end, inconsistent status.json) --------------


class TestSupersededPeriods:
    def test_older_period_of_same_identity_closed_as_reconnect(self, reconciler):
        old = record(
            connectTime=C1 - timedelta(hours=1), startEventTimestamp=C1 - timedelta(hours=1)
        )
        new = record(connectTime=C1, startEventTimestamp=S1)
        outcome = run(reconciler, [new, old], [osSession(2, "alice", connectTime=C1)], p=P)

        [end] = outcome.events
        assert end.usagePeriodId == old.usagePeriodId
        assert end.endCause is EndCause.RECONNECT_DETECTED
        # P is after the successor's start, so it is not a moment the old period was Active.
        assert end.periodEnd == old.startEventTimestamp
        assert outcome.state.sessions[2].openPeriod.usagePeriodId == new.usagePeriodId

    def test_p_before_successor_start_is_used(self, reconciler):
        old = record(connectTime=C1 - timedelta(hours=1))
        new = record(connectTime=C1, startEventTimestamp=S1)
        p = C1 - timedelta(minutes=10)
        outcome = run(reconciler, [old, new], [osSession(2, "alice", connectTime=C1)], p=p)
        assert outcome.events[0].periodEnd == p

    def test_older_period_of_another_user_closed_as_logoff(self, reconciler):
        old = record(2, "bob", connectTime=C1 - timedelta(hours=1))
        new = record(2, "alice", connectTime=C1)
        outcome = run(reconciler, [old, new], [])
        first, second = outcome.events
        assert (first.usagePeriodId, first.endCause) == (old.usagePeriodId, EndCause.LOGOFF)
        assert (second.usagePeriodId, second.endCause) == (new.usagePeriodId, EndCause.LOGOFF)

    def test_source_two_duplicates_use_p(self, reconciler):
        old = record(connectTime=C1 - timedelta(hours=1), startEventTimestamp=None)
        new = record(connectTime=C1, startEventTimestamp=None)
        outcome = run(reconciler, [old, new], [osSession(2, "alice", connectTime=C1)])
        [end] = outcome.events
        assert end.usagePeriodId == old.usagePeriodId
        assert end.periodEnd == P

    def test_duplicate_period_ids_are_reconciled_once(self, reconciler):
        u1 = record()
        outcome = run(reconciler, [u1, u1], [])
        assert len(outcome.events) == 1

    def test_superseded_end_bounds_a_later_start_on_the_session(self, reconciler):
        """Critic TASK-008 S3. The superseded end (its late S, 08:30+30 min) is later than the
        latest period's end (P), and both bound carol's start on the same sessionId (N1)."""
        p = C1 + timedelta(minutes=10)
        old = record(
            connectTime=C1 - timedelta(hours=1), startEventTimestamp=C1 + timedelta(minutes=30)
        )
        latest = record(connectTime=C1, startEventTimestamp=None)
        stale = C1 + timedelta(minutes=20)  # after the latest end, before the superseded one
        outcome = run(reconciler, [old, latest], [osSession(2, "carol", connectTime=stale)], p=p)

        oldEnd, latestEnd, start = outcome.events
        assert (oldEnd.usagePeriodId, oldEnd.periodEnd) == (
            old.usagePeriodId,
            C1 + timedelta(minutes=30),
        )
        assert (latestEnd.usagePeriodId, latestEnd.periodEnd) == (latest.usagePeriodId, p)
        assert outcome.state.lastPeriodEndBySessionId == {2: C1 + timedelta(minutes=30)}
        assert start.usagePeriodStart == R  # I4: the stale connect time is not reported
        assert start.durationApproximate is True


# --- record builders --------------------------------------------------------------------


class TestRecordBuilders:
    def test_open_periods_from_events_are_starts_without_end(self):
        closedId, openId = str(uuid.uuid4()), str(uuid.uuid4())
        events = [
            startEvent(closedId, C1),
            endEvent(closedId, C1 + timedelta(minutes=5), usagePeriodStart=C1),
            endEvent(str(uuid.uuid4()), C1, usagePeriodStart=C1 - timedelta(hours=1)),
            startEvent(openId, S1, usagePeriodStart=C1, connectTime=None, durationApproximate=True),
        ]
        [rec] = openPeriodsFromEvents(events)
        assert rec.usagePeriodId == openId
        assert rec.startEventTimestamp == S1
        assert rec.usagePeriodStart == C1
        assert rec.connectTime is None
        assert rec.connectTimeAtStart is None
        assert rec.durationApproximate is True

    def test_record_from_start_event_copies_the_event(self):
        event = startEvent(timestamp=S1, usagePeriodStart=C1, connectionType=CONSOLE)
        rec = recordFromStartEvent(event)
        assert rec.usagePeriodId == event.usagePeriodId
        assert (rec.sessionId, rec.userName, rec.domain) == (2, "alice", "CORP")
        assert rec.logonTime == event.logonTime
        assert rec.connectTime == rec.connectTimeAtStart == C1
        assert rec.connectionType is CONSOLE
        assert (rec.clientName, rec.clientAddress) == (event.clientName, event.clientAddress)
        period = rec.toUsagePeriod()
        assert period.startEventTimestamp == S1
        assert period.connectionTypeAtStart is CONSOLE

    @pytest.mark.parametrize("approximate", [False, True])
    def test_record_from_status_session(self, approximate):
        entry = StatusSession(
            sessionId=2,
            userName="alice",
            domain=None,
            state=SessionState.ACTIVE,
            connectionType=RDP,
            clientName="PC",
            clientAddress="10.0.0.1",
            logonTime=None,
            firstDetectedAt=None,
            connectTime=C1,
            usagePeriodId=str(uuid.uuid4()),
            usagePeriodStart=R - timedelta(minutes=1) if approximate else C1,
            durationApproximate=approximate,
            disconnectedSince=None,
        )
        rec = recordFromStatusSession(entry)
        assert rec.startEventTimestamp is None
        # S1: matched on the period's own connect time, not the session's latest one.
        assert rec.connectTime == rec.connectTimeAtStart == (None if approximate else C1)
        assert rec.usagePeriodStart == entry.usagePeriodStart

    def test_open_periods_from_status_skip_disconnected_and_duplicates(self):
        active = statusEntry(firstDetectedAt=None)
        duplicate = dataclasses.replace(
            statusEntry(5, "zed", firstDetectedAt=None), usagePeriodId=active.usagePeriodId
        )
        disconnected = statusEntry(3, "bob", state="Disconnected", firstDetectedAt=None)
        records = openPeriodsFromStatus([active, disconnected, duplicate])
        assert [r.usagePeriodId for r in records] == [active.usagePeriodId]

    def test_record_from_disconnected_status_session_is_refused(self):
        with pytest.raises(ValueError, match="no open usage period"):
            recordFromStatusSession(statusEntry(state="Disconnected", firstDetectedAt=None))

    def test_tracked_from_status_session(self):
        active = trackedFromStatusSession(statusEntry(firstDetectedAt=R))
        assert active.state is SessionState.ACTIVE
        assert active.openPeriod is not None
        assert active.openPeriod.startEventTimestamp is None
        assert active.lastKnownConnectTime == C1
        disconnected = trackedFromStatusSession(
            statusEntry(3, "bob", state="Disconnected", firstDetectedAt=R)
        )
        assert disconnected.openPeriod is None
        assert disconnected.disconnectedSince == C1


# --- summary log line and randomized invariants -------------------------------------------


def test_summary_line_is_logged(reconciler, capturedAppLog):
    run(reconciler, [record(), record(3, "bob")], [osSession(2, "alice", connectTime=C1)])
    messages = [line["message"] for line in capturedAppLog() if line["level"] == "INFO"]
    assert (
        "startup reconciliation: 2 open period(s): 1 continued, 1 closed; 0 session_start(s)"
        in messages
    )


USERS = ("alice", "bob", "carol")
STATES = ("Active", "Active", "Disconnected", "ConnectQuery", None)


def randomRecord(rng: random.Random, sessionId: int) -> OpenPeriodRecord:
    connect = R - timedelta(minutes=rng.randint(1, 600))
    approximate = rng.random() < 0.3
    s: datetime | None = connect + timedelta(seconds=rng.randint(0, 120))
    if rng.random() < 0.3:
        s = None
    elif rng.random() < 0.1:
        s = R + timedelta(seconds=rng.randint(1, 900))  # backward clock step
    return record(
        sessionId,
        rng.choice(USERS),
        connectTime=None if approximate else connect,
        usagePeriodStart=connect,
        startEventTimestamp=s,
        connectionType=rng.choice((RDP, CONSOLE)),
        logonTime=rng.choice((DEFAULT_LOGON_TIME, None)),
    )


def randomSnapshot(rng: random.Random):
    sessions = []
    for sessionId in range(1, 5):
        state = rng.choice(STATES)
        if state is None:
            continue
        connect = rng.choice((C1, R - timedelta(minutes=rng.randint(0, 700)), None))
        sessions.append(
            osSession(
                sessionId,
                rng.choice(USERS + ("",)),
                state,
                rng.choice(("RDP", "Console")),
                connectTime=connect,
                disconnectTime=rng.choice((None, R - timedelta(minutes=rng.randint(0, 700)))),
                logonTime=rng.choice((DEFAULT_LOGON_TIME, None)),
            )
        )
    return sessions


@pytest.mark.parametrize("seed", range(150))
def test_random_reconciliations_keep_every_invariant(fakeClock, seed):
    rng = random.Random(seed)
    tracker = SessionTracker(fakeClock, logging.getLogger(f"{LOGGER_NAME}.test.random"))
    records = [randomRecord(rng, rng.randint(1, 4)) for _ in range(rng.randint(0, 5))]
    p = rng.choice((None, P, R - timedelta(hours=12)))
    outcome = Reconciler(tracker).reconcile(records, p, (), randomSnapshot(rng), R)

    assert activityDocumentProblems(outcome.events) == []
    ids = {r.usagePeriodId for r in records}
    ended = [e.usagePeriodId for e in ends(outcome.events)]
    continued = {
        s.openPeriod.usagePeriodId for s in outcome.state.sessions.values() if s.openPeriod
    }
    # Every open period is either continued or closed exactly once, never both.
    assert sorted(ended) == sorted(set(ended))
    assert ids == (set(ended) | (continued & ids))
    assert not set(ended) & continued
    for event in ends(outcome.events):
        assert event.reason is EventReason.DETECTED_AT_STARTUP
        assert event.durationApproximate is True
    clockStepped = any(r.startEventTimestamp and r.startEventTimestamp > R for r in records)
    for event in starts(outcome.events):
        assert event.reason is EventReason.DETECTED_AT_STARTUP
        assert event.usagePeriodId not in ids
        # N1: no new start before an end emitted on the same sessionId (except after a
        # backward clock step, where R11 clamps the end to S > R, as at runtime).
        bound = outcome.state.lastPeriodEndBySessionId.get(event.sessionId)
        assert clockStepped or bound is None or event.usagePeriodStart >= bound
    for session in outcome.state.sessions.values():
        assert (session.state is SessionState.ACTIVE) == (session.openPeriod is not None)
