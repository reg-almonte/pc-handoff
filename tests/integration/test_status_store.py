"""Integration tests for ``StatusStore`` on a real temp directory (TASK-006 AC-1, AC-2)."""

import dataclasses
import hashlib
import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pc_handoff.detection.provider import ConnectionType
from pc_handoff.domain.models import PcStatus, StatusDocument
from pc_handoff.persistence import status_store
from pc_handoff.persistence.atomic import PersistenceError
from pc_handoff.persistence.paths import DataPaths
from pc_handoff.persistence.schemas import decodeJson, validateStatusDocument
from pc_handoff.persistence.status_store import (
    StatusLoadResult,
    StatusStore,
    buildStatusDocument,
)
from tests.fakes import FakeClock
from tests.fakes.builders import trackedActive, trackedDisconnected, trackerState

JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 10, 21, 9, 15, 2, tzinfo=JST)
BACKUP_NAME = "status.json.corrupt-20261021T091502"
POLL_AT = NOW - timedelta(seconds=5)


@pytest.fixture
def paths(tmp_path: Path) -> DataPaths:
    dataDir = tmp_path / "data"
    dataDir.mkdir()
    return DataPaths(dataDir)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(NOW)


@pytest.fixture
def store(paths: DataPaths, clock: FakeClock) -> StatusStore:
    return StatusStore(paths, clock, logging.getLogger("pc_handoff.persistence.status"))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def activeState():
    alice = trackedActive(
        2,
        "alice",
        connectTime=NOW - timedelta(minutes=10),
        startEventTimestamp=NOW - timedelta(minutes=10),
        clientName=None,
        clientAddress=None,
    )
    bob = trackedDisconnected(3, "bob", disconnectedSince=NOW - timedelta(minutes=3))
    return trackerState([alice, bob], POLL_AT)


def validStatusBytes() -> bytes:
    document = buildStatusDocument(activeState(), NOW)
    return json.dumps(document.toJson(), indent=4).encode("utf-8")


def corruptFiles(paths: DataPaths) -> list[Path]:
    return sorted(paths.dataDir.glob("status.json.corrupt-*"))


# --- AC-1 ----------------------------------------------------------------------------------


class TestWrite:
    def test_ac1_write_replaces_garbage_without_reading_or_backing_up(
        self, store: StatusStore, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        paths.status.write_bytes(b"\x00\xffgarbage{{{")
        reads: list[Path] = []
        monkeypatch.setattr(status_store, "readBytes", lambda p: reads.append(p))

        store.write(activeState())

        doc = decodeJson(paths.status.read_bytes())
        assert validateStatusDocument(doc) == []
        assert doc["status"] == "In Use"
        assert doc["updatedAt"] == "2026-10-21T09:15:02+09:00"
        alice = doc["sessions"][0]
        assert alice["userName"] == "alice"
        assert alice["clientName"] is None
        assert alice["clientAddress"] is None
        assert doc["sessions"][1]["state"] == "Disconnected"
        assert reads == []
        assert corruptFiles(paths) == []

    def test_written_bytes_are_utf8_indent_2_with_trailing_newline(
        self, store: StatusStore, paths: DataPaths
    ) -> None:
        state = trackerState(
            [trackedActive(2, "zoë", connectTime=NOW, startEventTimestamp=NOW, clientName="ПК-1")],
            POLL_AT,
        )
        store.write(state)
        raw = paths.status.read_bytes()
        expected = buildStatusDocument(state, NOW).toJson()
        assert raw == (json.dumps(expected, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        assert not raw.startswith(b"\xef\xbb\xbf")
        assert "zoë".encode() in raw

    def test_write_uses_the_clock_for_updated_at(
        self, store: StatusStore, paths: DataPaths, clock: FakeClock
    ) -> None:
        clock.advance(7)
        store.write(activeState())
        assert decodeJson(paths.status.read_bytes())["updatedAt"] == "2026-10-21T09:15:09+09:00"

    def test_unknown_state_is_written_with_last_known_status(
        self, store: StatusStore, paths: DataPaths
    ) -> None:
        state = trackerState(list(activeState().sessions.values()), POLL_AT, outagePending=True)
        store.write(state)
        doc = decodeJson(paths.status.read_bytes())
        assert validateStatusDocument(doc) == []
        assert doc["status"] == "Unknown"
        assert doc["lastKnownStatus"] == "In Use"
        assert doc["detectionHealthy"] is False

    def test_initial_state_is_valid(self, store: StatusStore, paths: DataPaths, tracker) -> None:
        store.write(tracker.initialState())
        doc = decodeJson(paths.status.read_bytes())
        assert validateStatusDocument(doc) == []
        assert doc["sessions"] == []
        assert doc["lastKnownStatus"] is None

    def test_a_document_failing_validation_is_not_written(
        self, store: StatusStore, paths: DataPaths
    ) -> None:
        paths.status.write_bytes(b"previous")
        bad = trackerState(
            [trackedActive(2, "", connectTime=NOW, startEventTimestamp=NOW)], POLL_AT
        )
        with pytest.raises(PersistenceError, match="userName"):
            store.write(bad)
        assert paths.status.read_bytes() == b"previous"

    def test_write_failure_raises_persistence_error_and_keeps_the_file(
        self, store: StatusStore, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        paths.status.write_bytes(b"previous")

        def failing(path: Path, data: bytes) -> None:
            raise PersistenceError("disk full")

        monkeypatch.setattr(status_store, "atomicWriteBytes", failing)
        with pytest.raises(PersistenceError):
            store.write(activeState())
        assert paths.status.read_bytes() == b"previous"


class TestBuildStatusDocument:
    def test_builds_a_fresh_document_with_schema_version_1(self) -> None:
        state = activeState()
        document = buildStatusDocument(state, NOW)
        assert isinstance(document, StatusDocument)
        assert document.schemaVersion == 1
        assert document.status is PcStatus.IN_USE
        assert document.lastKnownStatus is PcStatus.IN_USE
        assert document.lastKnownStatusAt == POLL_AT
        assert document.lastSuccessfulPollAt == POLL_AT
        assert document.updatedAt == NOW
        assert document.detectionHealthy is True
        assert [s.userName for s in document.sessions] == ["alice", "bob"]
        assert document.sessions[0].connectionType is ConnectionType.RDP


# --- AC-2 and loading ----------------------------------------------------------------------


CORRUPT_STATUS_CASES = {
    "unparseable": b'{"schemaVersion": 1, "status": ',
    "invalid enum": json.dumps(
        {**json.loads(validStatusBytes()), "status": "Maybe"}, indent=2
    ).encode("utf-8"),
    "schemaVersion 3": json.dumps({**json.loads(validStatusBytes()), "schemaVersion": 3}).encode(
        "utf-8"
    ),
    "root not an object": b"[]",
}


class TestLoadAtStartup:
    def test_missing_file_is_not_usable_and_has_no_notice(
        self, store: StatusStore, paths: DataPaths
    ) -> None:
        assert store.loadAtStartup() == StatusLoadResult(None, False, None)
        assert not paths.status.exists()

    @pytest.mark.parametrize("case", sorted(CORRUPT_STATUS_CASES))
    def test_ac2_corrupt_file_is_backed_up_with_identical_bytes(
        self,
        case: str,
        store: StatusStore,
        paths: DataPaths,
        capturedAppLog: Callable[[], list[dict]],
    ) -> None:
        raw = CORRUPT_STATUS_CASES[case]
        paths.status.write_bytes(raw)
        digest = sha(paths.status)

        result = store.loadAtStartup()

        backup = paths.dataDir / BACKUP_NAME
        assert corruptFiles(paths) == [backup]
        assert sha(backup) == digest
        assert not paths.status.exists()
        assert result.usable is False
        assert result.document is None
        assert result.notice is not None
        assert result.notice.fileName == "status.json"
        assert result.notice.backupName == BACKUP_NAME
        assert result.notice.text == f"Status data was reset (backup: {BACKUP_NAME})"
        warnings = [line["message"] for line in capturedAppLog() if line["level"] == "WARNING"]
        assert any("status.json was corrupt" in m and BACKUP_NAME in m for m in warnings)

    def test_validation_issues_are_logged(
        self, store: StatusStore, paths: DataPaths, capturedAppLog: Callable[[], list[dict]]
    ) -> None:
        paths.status.write_bytes(CORRUPT_STATUS_CASES["invalid enum"])
        store.loadAtStartup()
        warnings = [line["message"] for line in capturedAppLog() if line["level"] == "WARNING"]
        assert any("$.status" in m and "Maybe" in m for m in warnings)

    def test_valid_file_is_loaded_and_untouched(self, store: StatusStore, paths: DataPaths) -> None:
        raw = validStatusBytes()
        paths.status.write_bytes(b"\xef\xbb\xbf" + raw)  # a BOM is tolerated
        digest = sha(paths.status)

        result = store.loadAtStartup()

        assert result.usable is True
        assert result.notice is None
        assert result.document == StatusDocument.fromJson(json.loads(raw))
        assert result.document.sessions[0].clientName is None
        assert sha(paths.status) == digest
        assert corruptFiles(paths) == []

    def test_backup_failure_leaves_the_file_and_reports_it(
        self,
        store: StatusStore,
        paths: DataPaths,
        monkeypatch: pytest.MonkeyPatch,
        capturedAppLog: Callable[[], list[dict]],
    ) -> None:
        paths.status.write_bytes(b"garbage")

        def failing(path: Path, clock: FakeClock) -> Path:
            raise PermissionError("locked")

        monkeypatch.setattr(status_store, "backupCorrupt", failing)
        result = store.loadAtStartup()

        assert paths.status.read_bytes() == b"garbage"
        assert result.usable is False
        assert result.notice is not None
        assert result.notice.backupName is None
        assert result.notice.text == "status.json is corrupt and could not be backed up"
        errors = [line["message"] for line in capturedAppLog() if line["level"] == "ERROR"]
        assert any("could not be backed up" in m for m in errors)

    def test_unreadable_file_is_not_usable_and_logged(
        self,
        store: StatusStore,
        paths: DataPaths,
        monkeypatch: pytest.MonkeyPatch,
        capturedAppLog: Callable[[], list[dict]],
    ) -> None:
        paths.status.write_bytes(validStatusBytes())

        def failing(path: Path) -> bytes:
            raise PermissionError("denied")

        monkeypatch.setattr(status_store, "readBytes", failing)
        assert store.loadAtStartup() == StatusLoadResult(None, False, None)
        assert paths.status.exists()
        errors = [line["message"] for line in capturedAppLog() if line["level"] == "ERROR"]
        assert any("status.json could not be read" in m for m in errors)


# --- post-PASS fix S2 (Critic TASK-006) ------------------------------------------------------


class TestUnencodableValues:
    def test_s2_lone_surrogate_is_written_with_ascii_escapes(
        self, store: StatusStore, paths: DataPaths
    ) -> None:
        state = trackerState(
            [
                trackedActive(
                    2,
                    "bad\ud800name",
                    connectTime=NOW,
                    startEventTimestamp=NOW,
                    clientName="zoë",
                )
            ],
            POLL_AT,
        )

        store.write(state)

        raw = paths.status.read_bytes()
        raw.decode("utf-8")  # strict: valid UTF-8, no surrogates
        assert b"\\ud800" in raw
        doc = decodeJson(raw)
        assert validateStatusDocument(doc) == []
        assert doc["sessions"][0]["userName"] == "bad\ud800name"
        assert doc["sessions"][0]["clientName"] == "zoë"
        assert raw.endswith(b"\n")

    @pytest.mark.parametrize("error", [TypeError("not serializable"), ValueError("nan")])
    def test_s2_a_document_that_cannot_be_serialized_raises_persistence_error(
        self,
        store: StatusStore,
        paths: DataPaths,
        monkeypatch: pytest.MonkeyPatch,
        error: Exception,
    ) -> None:
        paths.status.write_bytes(b"previous")

        def failing(value: object, indent: int | None = None) -> bytes:
            raise error

        monkeypatch.setattr(status_store, "encodeJson", failing)
        with pytest.raises(PersistenceError, match="could not be serialized"):
            store.write(activeState())
        assert paths.status.read_bytes() == b"previous"

    def test_s2_a_state_that_cannot_be_built_raises_persistence_error(
        self, store: StatusStore, paths: DataPaths
    ) -> None:
        broken = trackedActive(2, "alice", connectTime=NOW, startEventTimestamp=NOW)
        state = trackerState([dataclasses.replace(broken, openPeriod=None)], POLL_AT)
        with pytest.raises(PersistenceError, match="no open usage period"):
            store.write(state)
        assert not paths.status.exists()
