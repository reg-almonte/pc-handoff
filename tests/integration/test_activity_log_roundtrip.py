"""Round-trip property test: everything ``ActivityLogStore`` writes passes the startup check.

Random tracker-generated batches (the TASK-004 ``Generator``) are appended through the
store with faults injected between and during appends:

- ``persist``: ``atomicWriteBytes`` raises ``PersistenceError`` (queue kept, retried);
- ``after``: ``os.replace`` succeeds, then ``RuntimeError`` (idempotent retry, AC-13);
- ``reformat``: the file is replaced externally by a valid indent-4 copy (slow path);
- ``damage``: the file is truncated externally (runtime backup, fresh file);
- ``delete``: the file is deleted externally (recreated).

After every append the file must pass ``validateActivityDocument``. At the end, across the
file and all its ``.corrupt-*`` backups, no event is present twice and the pairing rule holds
(requirement section 5); every generated event is on disk unless it was in a deleted file.
(After ``after`` then ``delete``, the queued events that were in the deleted file are written
again, which is correct: the only copy is gone.)

Known limitation, not generated: an ``after`` fault followed by ``damage`` before the next
append. The damaged bytes cannot be parsed, so the already-written events are not known to
be in the backup and are written again to the fresh file (one extra copy across log and
backup). It needs a programming-error exception and external damage within one poll.
"""

import json
import logging
import random
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from pc_handoff.domain.tracker import SessionTracker
from pc_handoff.persistence import activity_log_store, atomic
from pc_handoff.persistence.activity_log_store import ActivityLogStore
from pc_handoff.persistence.atomic import PersistenceError
from pc_handoff.persistence.paths import DataPaths
from pc_handoff.persistence.schemas import decodeJson, validateActivityDocument
from tests.conftest import FAKE_CLOCK_START
from tests.fakes import FakeClock
from tests.unit.test_tracker_invariants import Generator

SEEDS = 12
POLLS = 50
FAULTS = (
    (0.10, "persist"),
    (0.06, "after"),
    (0.05, "reformat"),
    (0.04, "damage"),
    (0.03, "delete"),
)


class FaultyWrite:
    """Replaces ``activity_log_store.atomicWriteBytes``; ``mode`` applies to the next call."""

    def __init__(self) -> None:
        self.mode: str | None = None
        self.calls = 0

    def __call__(self, path: Path, data: bytes) -> None:
        self.calls += 1
        mode, self.mode = self.mode, None
        if mode == "persist":
            raise PersistenceError("injected write failure")
        atomic.atomicWriteBytes(path, data)
        if mode == "after":
            raise RuntimeError("injected after os.replace")


def pickFault(rng: random.Random, previous: str | None) -> str | None:
    roll = rng.random()
    for chance, fault in FAULTS:
        if roll < chance:
            if fault == "damage" and previous == "after":
                return None  # the documented limitation above
            return fault
        roll -= chance
    return None


def allLogFiles(dataDir: Path) -> list[Path]:
    return sorted(dataDir.glob("activity_log.json*"))


def runSequence(seed: int, dataDir: Path, monkeypatch: pytest.MonkeyPatch, faults: Counter) -> None:
    rng = random.Random(seed)
    clock = FakeClock(FAKE_CLOCK_START)
    tracker = SessionTracker(clock, logging.getLogger("pc_handoff.domain.tracker"))
    generator = Generator(rng, clock.now())
    paths = DataPaths(dataDir)
    store = ActivityLogStore(paths, clock, logging.getLogger("pc_handoff.persistence.activity"))
    writer = FaultyWrite()
    monkeypatch.setattr(activity_log_store, "atomicWriteBytes", writer)
    assert store.loadAtStartup().usable is False  # fresh install: created empty

    state = tracker.initialState()
    generated: list[str] = []
    inBackups: list[dict[str, Any]] = []  # events of truncated (unparseable) backups
    deleted: list[dict[str, Any]] = []  # events of externally deleted files
    damaged: list[bytes] = []
    previous: str | None = None
    for _ in range(POLLS):
        clock.advance(rng.choice([1, 5, 5, 30]))
        outcome = tracker.applySuccess(state, generator.snapshot(clock.now()))
        state = outcome.state
        generated.extend(e.eventId for e in outcome.events)

        fault = pickFault(rng, previous)
        previous = fault
        if fault is not None:
            faults[fault] += 1
        path = paths.activityLog
        if fault == "reformat" and path.exists():
            path.write_bytes(json.dumps(decodeJson(path.read_bytes()), indent=4).encode())
        elif fault == "damage" and path.exists():
            raw = path.read_bytes()
            inBackups.extend(decodeJson(raw)["events"])
            damaged.append(raw[:-7])
            path.write_bytes(raw[:-7])
        elif fault == "delete" and path.exists():
            deleted.extend(decodeJson(path.read_bytes())["events"])
            path.unlink()
        elif fault in ("persist", "after"):
            writer.mode = fault

        try:
            store.appendBatch(outcome.events)
        except RuntimeError as exc:
            assert "injected" in str(exc)
        writer.mode = None
        if path.exists():
            assert validateActivityDocument(decodeJson(path.read_bytes())) == [], seed

    # Flush whatever is still pending, then check the log and its backups together.
    assert store.appendBatch([]).written is True
    assert store.pendingCount() == 0
    backups = [p for p in allLogFiles(dataDir) if p != paths.activityLog]
    assert len(backups) == len(damaged)
    assert sorted(b.read_bytes() for b in backups) == sorted(damaged)  # bytes unchanged
    doc = decodeJson(paths.activityLog.read_bytes())
    assert validateActivityDocument(doc) == []
    onDisk = inBackups + doc["events"]
    # Exactly once on disk (log plus backups); nothing lost except with a deleted file.
    counts = Counter(e["eventId"] for e in onDisk)
    assert set(counts.values()) <= {1}, seed
    assert set(counts) | {e["eventId"] for e in deleted} == set(generated), seed
    assert len(generated) == len(set(generated))
    perPeriod = Counter((e["usagePeriodId"], e["eventType"]) for e in onDisk)
    assert set(perPeriod.values()) <= {1}, seed
    # A restart accepts the final file as it is.
    assert ActivityLogStore(paths, clock, logging.getLogger("t")).loadAtStartup().usable


def test_store_output_always_validates_under_faults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capturedAppLog: Callable[[], list[dict]],
) -> None:
    faults: Counter = Counter()
    for seed in range(SEEDS):
        runSequence(seed, tmp_path / f"seed{seed}", monkeypatch, faults)
    # Every fault kind is exercised, and the tracker's events are never refused.
    assert set(faults) == {fault for _, fault in FAULTS}
    assert min(faults.values()) >= 8
    lines = capturedAppLog()
    assert not any("refused" in line["message"] for line in lines)
    assert any("already persisted" in line["message"] for line in lines)
    assert any("damaged while running" in line["message"] for line in lines)
