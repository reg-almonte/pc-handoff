"""Poll-cycle benchmark at 10,000 events (TASK-006 AC-9, REQ AC-21, ADR-003).

Excluded from the default run by ``-m 'not perf'`` in ``pyproject.toml``. Run with::

    pytest -m perf --no-cov -s

The same test is run on the shared PC in TASK-011.
"""

import logging
import math
import statistics
import time
from datetime import timedelta
from pathlib import Path

import pytest

from pc_handoff.detection.timed_source import TimedSessionSource
from pc_handoff.domain.tracker import SessionTracker
from pc_handoff.persistence.activity_log_store import ActivityLogStore, serializeCanonical
from pc_handoff.persistence.paths import DataPaths
from pc_handoff.persistence.schemas import decodeJson, validateActivityDocument
from pc_handoff.persistence.status_store import StatusStore
from tests.conftest import FAKE_CLOCK_START
from tests.fakes import FakeClock, FakeSessionProvider, makeSession
from tests.fakes.log_factory import makeEvents

LOG_EVENTS = 10_000
POLLS = 100
P95_BUDGET_SECONDS = 1.0
POLL_INTERVAL_SECONDS = 5


def percentile(samples: list[float], fraction: float) -> float:
    """Nearest-rank percentile: the smallest sample with at least ``fraction`` at or below."""
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


@pytest.mark.perf
def test_ac9_poll_cycle_p95_at_10000_events(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataDir = tmp_path / "data"
    dataDir.mkdir()
    paths = DataPaths(dataDir)
    paths.activityLog.write_bytes(
        serializeCanonical({}, [e.toJson() for e in makeEvents(LOG_EVENTS, openPeriods=2)])
    )
    clock = FakeClock(FAKE_CLOCK_START)
    logger = logging.getLogger("pc_handoff.perf")
    activityStore = ActivityLogStore(paths, clock, logger)
    statusStore = StatusStore(paths, clock, logger)
    assert activityStore.loadAtStartup().usable is True
    tracker = SessionTracker(clock, logger)
    provider = FakeSessionProvider()
    source = TimedSessionSource(provider)
    state = tracker.initialState()

    def setSnapshot() -> None:
        # The Active session reconnects every poll (new connect time): an end and a start
        # per poll, the worst case. The Disconnected session never changes.
        provider.setSessions(
            [
                makeSession(2, "alice", "Active", "RDP", connectTime=clock.now()),
                makeSession(
                    3,
                    "bob",
                    "Disconnected",
                    "RDP",
                    disconnectTime=FAKE_CLOCK_START - timedelta(minutes=5),
                ),
            ]
        )

    def pollCycle() -> int:
        nonlocal state
        outcome = tracker.applySuccess(state, source.enumerate())
        state = outcome.state
        assert activityStore.appendBatch(outcome.events).written is True
        statusStore.write(state)
        return len(outcome.events)

    setSnapshot()
    assert pollCycle() == 1  # warm-up: alice's first start
    durations: list[float] = []
    for _ in range(POLLS):
        clock.advance(POLL_INTERVAL_SECONDS)
        setSnapshot()
        started = time.perf_counter()
        eventCount = pollCycle()
        durations.append(time.perf_counter() - started)
        assert eventCount == 2

    p50 = statistics.median(durations)
    p95 = percentile(durations, 0.95)
    with capsys.disabled():
        print(
            f"\n[perf] {POLLS} poll cycles at {LOG_EVENTS} events: "
            f"p50 {p50 * 1000:.1f} ms, p95 {p95 * 1000:.1f} ms, max {max(durations) * 1000:.1f} ms"
        )
    doc = decodeJson(paths.activityLog.read_bytes())
    assert len(doc["events"]) == LOG_EVENTS + 1 + 2 * POLLS
    assert validateActivityDocument(doc) == []
    assert p95 <= P95_BUDGET_SECONDS


def test_percentile_is_nearest_rank() -> None:
    samples = [float(n) for n in range(1, 101)]
    assert percentile(samples, 0.95) == 95.0
    assert percentile(samples, 0.5) == 50.0
    assert percentile([3.0], 0.95) == 3.0
