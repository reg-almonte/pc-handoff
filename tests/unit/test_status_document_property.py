"""Every tracker state the runtime can write makes a valid ``status.json`` (Critic TASK-005 F4).

Replays the TASK-004 invariant generator (random snapshots, failures, transitional states,
backward clock steps) and checks the ``StatusDocument`` built from every resulting state
against the ADR-005 validator, plus a JSON round trip. The first sequences also go through
the real ``StatusStore.write``, which validates its own output before writing.
"""

import json
import logging
import random
from datetime import timedelta
from pathlib import Path

from pc_handoff.domain.models import StatusDocument
from pc_handoff.domain.tracker import SessionTracker
from pc_handoff.persistence.paths import DataPaths
from pc_handoff.persistence.schemas import validateStatusDocument
from pc_handoff.persistence.status_store import StatusStore, buildStatusDocument
from tests.conftest import FAKE_CLOCK_START
from tests.fakes import FakeClock
from tests.unit.test_tracker_invariants import POLLS, SEQUENCES, Generator

WRITTEN_SEQUENCES = 1  # each write fsyncs; the validator check above covers every state

# The tracker's WARNING/INFO lines are irrelevant here and expensive under log capture.
QUIET = logging.getLogger("tests.status_document_property")
QUIET.disabled = True


def replay(seed: int):
    """Yield ``(state, now)`` for every poll of the TASK-004 sequence ``seed``."""
    rng = random.Random(seed)
    clock = FakeClock(FAKE_CLOCK_START)
    tracker = SessionTracker(clock, QUIET)
    generator = Generator(rng, clock.now() - timedelta(hours=1))
    state = tracker.initialState()
    yield state, clock.now()
    for _ in range(POLLS):
        if rng.random() < 1 / 8:
            clock.set(clock.now() - timedelta(seconds=rng.randint(1, 120)))
        else:
            clock.advance(rng.choice([1, 5, 5, 5, 30, 300]))
        if rng.random() < 0.1:
            state = tracker.applyFailure(state, RuntimeError("fail")).state
        else:
            state = tracker.applySuccess(state, generator.snapshot(clock.now())).state
        yield state, clock.now()


def test_every_generated_state_builds_a_valid_status_document():
    checked = withSessions = unknown = 0
    for seed in range(SEQUENCES):
        for state, now in replay(seed):
            doc = buildStatusDocument(state, now).toJson()
            assert validateStatusDocument(doc) == [], f"seed {seed}: {doc}"
            assert StatusDocument.fromJson(json.loads(json.dumps(doc))).toJson() == doc
            checked += 1
            withSessions += bool(doc["sessions"])
            unknown += doc["status"] == "Unknown"
    assert checked == SEQUENCES * (POLLS + 1)
    assert withSessions > checked // 2
    assert unknown > 500


def test_the_status_store_writes_every_generated_state(tmp_path: Path):
    for seed in range(WRITTEN_SEQUENCES):
        dataDir = tmp_path / str(seed)
        dataDir.mkdir()
        for state, now in replay(seed):
            clock = FakeClock(now)
            store = StatusStore(DataPaths(dataDir), clock, QUIET)
            store.write(state)  # raises PersistenceError if its own output were invalid
            doc = json.loads((dataDir / "status.json").read_text(encoding="utf-8"))
            assert validateStatusDocument(doc) == []
            assert len(doc["sessions"]) == len(state.sessions)
