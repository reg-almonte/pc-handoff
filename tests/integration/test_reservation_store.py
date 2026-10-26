"""Integration tests for ``ReservationStore`` and ``ActivityLogStore`` reservation events."""

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pc_handoff.domain.models import EventReason, newStartEvent
from pc_handoff.domain.reservations import (
    QueueState,
    ReservationStatus,
    emptyQueueState,
    enqueueReservation,
    postStatusNote,
)
from pc_handoff.persistence import reservation_store
from pc_handoff.persistence.activity_log_store import ActivityLogStore
from pc_handoff.persistence.atomic import PersistenceError
from pc_handoff.persistence.paths import DataPaths
from pc_handoff.persistence.reservation_store import ReservationLoadResult, ReservationStore
from pc_handoff.persistence.schemas import (
    decodeJson,
    validateActivityDocument,
    validateReservationsDocument,
)
from tests.fakes import FakeClock
from tests.fakes.builders import trackedActive

JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 10, 26, 9, 30, 0, tzinfo=JST)
BACKUP_NAME = "reservations.json.corrupt-20261026T093000"


@pytest.fixture
def paths(tmp_path: Path) -> DataPaths:
    dataDir = tmp_path / "data"
    dataDir.mkdir()
    return DataPaths(dataDir)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(NOW)


@pytest.fixture
def store(paths: DataPaths, clock: FakeClock) -> ReservationStore:
    return ReservationStore(paths, clock, logging.getLogger("pc_handoff.persistence.reservations"))


def sampleQueueState() -> QueueState:
    s0 = emptyQueueState(NOW - timedelta(minutes=10))
    s1 = enqueueReservation(s0, "alice", 60, NOW - timedelta(minutes=5), pcAvailable=True).state
    s2 = enqueueReservation(
        s1,
        "bob",
        30,
        NOW - timedelta(minutes=3),
        notes="Hardware check",
        pcAvailable=True,
    ).state
    return postStatusNote(s2, "alice", "Connecting shortly", NOW - timedelta(minutes=2)).state


def validReservationsBytes() -> bytes:
    return (json.dumps(sampleQueueState().toJson(), indent=2) + "\n").encode("utf-8")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- DataPaths & ReservationStore.write ---------------------------------------------------


def test_data_paths_reservations_property(paths: DataPaths) -> None:
    assert paths.reservations == paths.dataDir / "reservations.json"


def test_write_creates_valid_utf8_json_and_updates_timestamp(
    store: ReservationStore,
    paths: DataPaths,
    clock: FakeClock,
) -> None:
    state = sampleQueueState()
    clock.advance(15)
    store.write(state)

    raw = paths.reservations.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert raw.endswith(b"\n")
    doc = decodeJson(raw)
    assert validateReservationsDocument(doc) == []
    assert doc["updatedAt"] == "2026-10-26T09:30:15+09:00"
    assert len(doc["reservations"]) == 2
    assert len(doc["statusNotes"]) == 1


def test_write_rejects_unbuildable_or_invalid_state_without_overwriting(
    store: ReservationStore,
    paths: DataPaths,
) -> None:
    paths.reservations.write_bytes(b"existing")

    with pytest.raises(PersistenceError, match="could not be built"):
        store.write("not-a-queue-state")  # type: ignore[arg-type]
    assert paths.reservations.read_bytes() == b"existing"

    # Build a state whose reservation fails validation (READY without claimExpiresAt).
    state = sampleQueueState()
    brokenRes = replace(
        state.reservations[0],
        status=ReservationStatus.READY,
        claimExpiresAt=None,
    )
    badState = replace(state, reservations=(brokenRes,))
    with pytest.raises(PersistenceError, match="failed validation"):
        store.write(badState)
    assert paths.reservations.read_bytes() == b"existing"


# --- ReservationStore.loadAtStartup -------------------------------------------------------


def test_load_at_startup_missing_file_returns_empty_state_without_notice(
    store: ReservationStore,
    paths: DataPaths,
) -> None:
    result = store.loadAtStartup()
    assert result == ReservationLoadResult(emptyQueueState(NOW), False, None)
    assert not paths.reservations.exists()


def test_load_at_startup_valid_file_with_bom_round_trips(
    store: ReservationStore,
    paths: DataPaths,
) -> None:
    raw = validReservationsBytes()
    paths.reservations.write_bytes(b"\xef\xbb\xbf" + raw)
    result = store.loadAtStartup()
    assert result.usable is True
    assert result.notice is None
    assert result.state == QueueState.fromJson(json.loads(raw))


@pytest.mark.parametrize(
    "corruptBytes",
    [
        b'{"schemaVersion": 1, "reservations": ',
        b"[]",
        json.dumps({**json.loads(validReservationsBytes()), "schemaVersion": 2}).encode("utf-8"),
        json.dumps({**json.loads(validReservationsBytes()), "handoff": "bad"}).encode("utf-8"),
    ],
    ids=["unparseable", "root-list", "bad-schema-version", "invalid-handoff"],
)
def test_load_at_startup_corrupt_file_backs_up_and_returns_notice(
    corruptBytes: bytes,
    store: ReservationStore,
    paths: DataPaths,
    capturedAppLog: Callable[[], list[dict]],
) -> None:
    paths.reservations.write_bytes(corruptBytes)
    digest = sha(paths.reservations)

    result = store.loadAtStartup()

    backup = paths.dataDir / BACKUP_NAME
    assert backup.exists()
    assert sha(backup) == digest
    assert not paths.reservations.exists()
    assert result.usable is False
    assert result.state == emptyQueueState(NOW)
    assert result.notice is not None
    assert result.notice.fileName == "reservations.json"
    assert result.notice.backupName == BACKUP_NAME
    assert result.notice.text == f"Reservation data was reset (backup: {BACKUP_NAME})"
    warnings = [line["message"] for line in capturedAppLog() if line["level"] == "WARNING"]
    assert any("reservations.json was corrupt" in m for m in warnings)


def test_load_at_startup_read_error_and_backup_error_are_handled(
    store: ReservationStore,
    paths: DataPaths,
    monkeypatch: pytest.MonkeyPatch,
    capturedAppLog: Callable[[], list[dict]],
) -> None:
    # 1. Read failure returns empty state and logs ERROR.
    def failRead(path: Path) -> bytes:
        raise OSError("permission denied")

    monkeypatch.setattr(reservation_store, "readBytes", failRead)
    resReadFail = store.loadAtStartup()
    assert resReadFail.usable is False
    assert resReadFail.notice is None

    # 2. Backup failure on corrupt file logs ERROR and returns notice without backupName.
    monkeypatch.undo()
    paths.reservations.write_bytes(b"corrupt{{{")

    def failBackup(path: Path, clock: object) -> Path:
        raise OSError("disk read-only")

    monkeypatch.setattr(reservation_store, "backupCorrupt", failBackup)
    resBackupFail = store.loadAtStartup()
    assert resBackupFail.usable is False
    assert resBackupFail.notice is not None
    assert resBackupFail.notice.backupName is None
    assert "could not be backed up" in resBackupFail.notice.text
    errors = [line["message"] for line in capturedAppLog() if line["level"] == "ERROR"]
    assert len(errors) >= 2


# --- ActivityLogStore integration with ReservationAuditEvent ------------------------------


def test_activity_log_store_appends_deduplicates_and_loads_reservation_and_note_events(
    paths: DataPaths,
    clock: FakeClock,
) -> None:
    logger = logging.getLogger("pc_handoff.persistence.activity")
    logStore = ActivityLogStore(paths, clock, logger)
    assert logStore.loadAtStartup().usable is False

    # Build a session_start event, reservation events, and a status_note_posted event.
    session = trackedActive(2, "alice", connectTime=NOW, startEventTimestamp=NOW)
    assert session.openPeriod is not None
    startEv = newStartEvent(session, session.openPeriod, EventReason.OBSERVED, NOW)

    qOut = enqueueReservation(emptyQueueState(NOW), "bob", 45, NOW, pcAvailable=True)
    noteOut = postStatusNote(qOut.state, "alice", "Wrapping up in 5m", NOW)

    batch = [startEv, *qOut.events, *noteOut.events]
    res1 = logStore.appendBatch(batch)
    assert res1.written is True
    assert logStore.pendingCount() == 0

    # Idempotent retry of the exact same reservation and status note events is skipped.
    resDup = logStore.appendBatch([*qOut.events, *noteOut.events])
    assert resDup.written is True

    # Force slow path (external whitespace change) and verify buildIndex indexes reservation events.
    raw = paths.activityLog.read_bytes()
    paths.activityLog.write_bytes(raw + b"\n")
    resSlowDup = logStore.appendBatch([*qOut.events, *noteOut.events])
    assert resSlowDup.written is True

    # Reload at startup: valid document, only session events returned in result.events.
    reloadedStore = ActivityLogStore(paths, clock, logger)
    loadRes = reloadedStore.loadAtStartup()
    assert loadRes.usable is True
    assert loadRes.events == [startEv]

    parsed = decodeJson(paths.activityLog.read_bytes())
    assert validateActivityDocument(parsed) == []
    assert [e["eventType"] for e in parsed["events"]] == [
        "session_start",
        "reservation_queued",
        "handoff_initiated",
        "status_note_posted",
    ]
