"""Property-style invariants of SessionTracker over random poll sequences (TASK-004 AC-18).

Also asserts Critic TASK-003 G1 (lastKnownStatus never Unknown), G2 (every end is checked
like a ``newEndEvent`` output) and G4 (each start's timestamp equals its period's
``startEventTimestamp``), and the Critic TASK-004 S3 invariants:

- conservation: every started period is ended exactly once or is still the open period of
  exactly one tracked Active session (so a dropped end is caught);
- an end repeats its start's ``usagePeriodStart``;
- ``reason`` is ``detected_after_outage`` exactly when the poll follows a failure, or the
  session kept its outage context through transitional polls (M2); never ``detected_at_startup``;
- no overlap (M1 / I4, N1): on one sessionId, whatever the identity, a new period does not
  start before the last period on that sessionId ended, unless it starts at a detection time
  that a backward clock step put before that end.
"""

import logging
import random
from datetime import timedelta

from pc_handoff.detection.provider import ConnectionType
from pc_handoff.domain.models import EventReason, EventType, PcStatus, SessionState
from pc_handoff.domain.status import deriveStatus
from pc_handoff.domain.tracker import SessionTracker
from tests.conftest import FAKE_CLOCK_START
from tests.fakes import FakeClock
from tests.fakes.activity_checks import activityDocumentProblems
from tests.fakes.builders import TRANSITIONAL_RAW_NAMES, osSession

SEQUENCES = 200
POLLS = 50
USERS = ["alice", "bob", "carol", "", None]
DOMAINS = [None, "CORP", "corp"]
SESSION_IDS = [1, 2, 3]
STATES = ["Active", "Active", "Disconnected", "absent", *TRANSITIONAL_RAW_NAMES[:3]]


class Generator:
    """Random snapshots. Connect times are mostly per-session and only move forward (so a
    change is a genuine reconnect), with occasional stale or null values (S4)."""

    def __init__(self, rng: random.Random, base) -> None:
        self.rng = rng
        self.base = base
        self.latestConnect = dict.fromkeys(SESSION_IDS, base)

    def userName(self):
        user = self.rng.choice(USERS)
        if not user:
            return user
        return self.rng.choice([user, user.upper(), user.capitalize()])

    def connectTime(self, sid, now):
        choice = self.rng.random()
        if choice < 0.15:
            return None
        if choice < 0.25:
            return self.base  # stale: the first connection of the sequence
        if choice < 0.5:
            self.latestConnect[sid] = max(
                self.latestConnect[sid], now - timedelta(seconds=self.rng.randint(0, 30))
            )
        return self.latestConnect[sid]

    def snapshot(self, now):
        rng = self.rng
        logons = [None, self.base - timedelta(hours=1), self.base - timedelta(hours=2)]
        sessions = [
            osSession(0, None, "Disconnected", "Console", winStationName="Services"),
            osSession(65536, None, "Ignored", "Console", winStationName="RDP-Tcp"),
        ]
        for sid in SESSION_IDS:
            state = rng.choice(STATES)
            if state == "absent":
                continue
            sessions.append(
                osSession(
                    sid,
                    self.userName(),
                    state,
                    rng.choice(list(ConnectionType)),
                    domain=rng.choice(DOMAINS),
                    connectTime=self.connectTime(sid, now),
                    logonTime=rng.choice(logons),
                    disconnectTime=rng.choice(
                        [None, self.base, now - timedelta(seconds=rng.randint(0, 600)), now]
                    ),
                    clientName=rng.choice([None, "PC-A", "PC-B"]),
                )
            )
        rng.shuffle(sessions)
        return sessions


def checkState(outcome, snapshot):
    state = outcome.state
    assert state.lastKnownStatus is not PcStatus.UNKNOWN
    if snapshot is None:
        assert state.status is PcStatus.UNKNOWN
        return
    assert state.status is deriveStatus(state.sessions.values())
    byId = {s.sessionId: s for s in snapshot}
    for sid, tracked in state.sessions.items():
        assert tracked.userName
        assert sid not in (0, 65536)
        # Every tracked session is present in this snapshot under a non-empty user name.
        assert byId[sid].userName
        if tracked.state is SessionState.ACTIVE:
            assert tracked.openPeriod is not None
        else:
            assert tracked.openPeriod is None
            assert tracked.disconnectedSince is not None
    if state.status is PcStatus.IN_USE:
        assert any(byId[sid].userName for sid in state.sessions)


class SequenceChecker:
    """Cross-poll invariants, modelled independently of the tracker's internals."""

    def __init__(self) -> None:
        self.starts = {}
        self.ended = set()
        self.outagePending = set()  # sessionIds holding an outage context (M2)
        self.lastEnd = {}  # sessionId -> latest periodEnd of any period on it

    def check(self, previous, outcome, now, failed):
        for event in outcome.events:
            sid = event.sessionId
            expectOutage = previous.outagePending or sid in self.outagePending
            assert event.reason is (
                EventReason.DETECTED_AFTER_OUTAGE if expectOutage else EventReason.OBSERVED
            )
            periodId = event.usagePeriodId
            if event.eventType is EventType.SESSION_START:
                assert periodId not in self.starts
                # G4: the period's S is the start event's own timestamp.
                period = outcome.state.sessions[sid].openPeriod
                assert period.usagePeriodId == periodId
                assert period.startEventTimestamp == event.timestamp == now
                # M1 / I4 / N1: no overlap with any earlier period on this sessionId.
                previousEnd = self.lastEnd.get(sid)
                if previousEnd is not None:
                    assert event.usagePeriodStart >= previousEnd or (
                        event.usagePeriodStart == now < previousEnd
                    )
                self.starts[periodId] = event
            else:
                start = self.starts[periodId]
                assert periodId not in self.ended
                assert event.timestamp >= start.timestamp
                assert event.usagePeriodStart == start.usagePeriodStart
                assert event.endCause is not None
                assert event.periodEnd is None or event.periodEnd >= event.usagePeriodStart
                assert event.durationSeconds is None or event.durationSeconds >= 0
                self.ended.add(periodId)
                if event.periodEnd is not None:
                    self.lastEnd[sid] = max(self.lastEnd.get(sid, event.periodEnd), event.periodEnd)
        # Conservation: open periods are exactly the started, not yet ended ones.
        openIds = [
            s.openPeriod.usagePeriodId for s in outcome.state.sessions.values() if s.openPeriod
        ]
        assert len(openIds) == len(set(openIds))
        assert set(openIds) == set(self.starts) - self.ended
        if not failed:
            self.outagePending = {
                sid
                for sid, tracked in outcome.state.sessions.items()
                if tracked.consecutiveTransitionalPolls > 0
                and (previous.outagePending or sid in self.outagePending)
            }


def runSequence(seed: int) -> list:
    rng = random.Random(seed)
    clock = FakeClock(FAKE_CLOCK_START)
    tracker = SessionTracker(clock, logging.getLogger("pc_handoff.domain.tracker"))
    generator = Generator(rng, clock.now() - timedelta(hours=1))
    checker = SequenceChecker()
    state = tracker.initialState()
    events = []
    for _ in range(POLLS):
        if rng.random() < 1 / 8:
            clock.set(clock.now() - timedelta(seconds=rng.randint(1, 120)))
        else:
            clock.advance(rng.choice([1, 5, 5, 5, 30, 300]))
        failed = rng.random() < 0.1
        if failed:
            snapshot = None
            outcome = tracker.applyFailure(state, RuntimeError("fail"))
            assert outcome.events == []
        else:
            snapshot = generator.snapshot(clock.now())
            outcome = tracker.applySuccess(state, snapshot)
        checker.check(state, outcome, clock.now(), failed)
        checkState(outcome, snapshot)
        events.extend(outcome.events)
        state = outcome.state
    return events


def test_ac18_invariants_hold_over_random_sequences():
    totals = dict.fromkeys(EventType, 0)
    reasons = dict.fromkeys(EventReason, 0)
    for seed in range(SEQUENCES):
        events = runSequence(seed)
        assert activityDocumentProblems(events) == [], f"seed {seed}"
        for event in events:
            totals[event.eventType] += 1
            reasons[event.reason] += 1
    # The generator must actually exercise both event types and both runtime reasons.
    assert totals[EventType.SESSION_START] > 1000
    assert totals[EventType.SESSION_END] > 1000
    assert reasons[EventReason.DETECTED_AFTER_OUTAGE] > 100
    assert reasons[EventReason.DETECTED_AT_STARTUP] == 0
