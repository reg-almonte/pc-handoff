"""Integration tests for ``ActivityLogStore`` on a real temp directory (TASK-006, ADR-003).

Covers AC-3 to AC-8 and AC-10 to AC-13, plus the decisions.log constraints on the pairing
guard (F1, F2, G2/G4) and on validating before ``fromJson`` (G3). The round-trip property
test lives in ``test_activity_log_roundtrip.py``.
"""

import hashlib
import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from pc_handoff.domain.models import ActivityEvent, EventType
from pc_handoff.persistence import activity_log_store, atomic
from pc_handoff.persistence.activity_log_store import (
    EMPTY_SUFFIX,
    SUFFIX,
    ActivityLoadResult,
    ActivityLogStore,
    AppendResult,
    PairEntry,
    buildIndex,
    encodeJson,
    extrasOf,
    isCanonical,
    serializeCanonical,
)
from pc_handoff.persistence.atomic import PersistenceError
from pc_handoff.persistence.notices import StartupNotice
from pc_handoff.persistence.paths import DataPaths
from pc_handoff.persistence.schemas import decodeJson, validateActivityDocument
from tests.fakes import FakeClock
from tests.fakes.log_factory import LOG_BASE_TIME, endEvent, makeEvents, newId, startEvent

JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 10, 21, 9, 15, 2, tzinfo=JST)
BACKUP_NAME = "activity_log.json.corrupt-20261021T091502"
EMPTY_LOG = b'{"schemaVersion": 1, "events": []}\n'
T0 = datetime(2026, 10, 21, 10, 0, 0, tzinfo=JST)
BACKUP_FAILED_TEXT = (
    "activity_log.json is damaged and could not be backed up; "
    "new events are kept in memory until it can be"
)


# --- fixtures and helpers -----------------------------------------------------------------


@dataclass
class Env:
    paths: DataPaths
    clock: FakeClock
    logs: Callable[[], list[dict]]

    @property
    def path(self) -> Path:
        return self.paths.activityLog

    def newStore(self) -> ActivityLogStore:
        return ActivityLogStore(
            self.paths, self.clock, logging.getLogger("pc_handoff.persistence.activity_log")
        )

    def writeEvents(self, events: list[ActivityEvent], **dumpArgs: Any) -> bytes:
        dicts = [e.toJson() for e in events]
        if dumpArgs:
            raw = json.dumps({"schemaVersion": 1, "events": dicts}, **dumpArgs).encode("utf-8")
        else:
            raw = serializeCanonical({}, dicts)
        self.path.write_bytes(raw)
        return raw

    def started(self, events: list[ActivityEvent]) -> ActivityLogStore:
        self.writeEvents(events)
        store = self.newStore()
        assert store.loadAtStartup().usable is True
        return store

    def raw(self) -> bytes:
        return self.path.read_bytes()

    def doc(self) -> dict[str, Any]:
        doc = decodeJson(self.raw())
        assert isinstance(doc, dict)
        return doc

    def eventDicts(self) -> list[dict[str, Any]]:
        return self.doc()["events"]

    def eventIds(self) -> list[str]:
        return [e["eventId"] for e in self.eventDicts()]

    def assertValid(self) -> None:
        assert validateActivityDocument(self.doc()) == []

    def corruptFiles(self) -> list[Path]:
        return sorted(self.paths.dataDir.glob("activity_log.json.corrupt-*"))

    def messages(self, level: str) -> list[str]:
        return [line["message"] for line in self.logs() if line["level"] == level]


@pytest.fixture
def env(tmp_path: Path, capturedAppLog: Callable[[], list[dict]]) -> Env:
    dataDir = tmp_path / "data"
    dataDir.mkdir()
    return Env(DataPaths(dataDir), FakeClock(NOW), capturedAppLog)


@pytest.fixture
def loadsCalls(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[int]]:
    """Counts ``json.loads`` calls made from now on (AC-8)."""
    calls: list[int] = []
    original = json.loads

    def counting(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(json, "loads", counting)
    yield calls


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def eventLines(raw: bytes) -> list[bytes]:
    """The event lines of a canonical non-empty file, without the separating comma."""
    return [line.removesuffix(b",") for line in raw.split(b"\n")[1:-2]]


def period(start: datetime = T0, **kwargs: Any) -> tuple[ActivityEvent, ActivityEvent]:
    """A start/end pair for one fresh period: start at ``start``, end 30 minutes later."""
    periodId = newId()
    first = startEvent(periodId, start, **kwargs)
    last = endEvent(periodId, start + timedelta(minutes=30), usagePeriodStart=start, **kwargs)
    return first, last


def failingWrite(error: BaseException) -> Callable[[Path, bytes], None]:
    def write(path: Path, data: bytes) -> None:
        raise error

    return write


# --- canonical layout ---------------------------------------------------------------------


class TestCanonicalLayout:
    def test_constants(self) -> None:
        assert SUFFIX == b"\n]}\n"
        assert EMPTY_SUFFIX == b"]}\n"

    def test_empty_document(self) -> None:
        assert serializeCanonical({}, []) == EMPTY_LOG

    def test_one_event_per_line_with_default_separators(self) -> None:
        raw = serializeCanonical({}, [{"a": 1, "b": "é"}, {"c": None}])
        assert (
            raw
            == ('{"schemaVersion": 1, "events": [\n{"a": 1, "b": "é"},\n{"c": null}\n]}\n').encode()
        )

    def test_extra_keys_sit_between_schema_version_and_events(self) -> None:
        raw = serializeCanonical({"note": "kept", "n": [1, 2]}, [])
        assert raw == b'{"schemaVersion": 1, "note": "kept", "n": [1, 2], "events": []}\n'

    def test_lone_surrogates_are_escaped_so_the_bytes_stay_utf8(self) -> None:
        value = {"userName": "bad\ud800name", "note": "é"}
        raw = serializeCanonical({"x": "\udfff"}, [value])
        raw.decode("utf-8")  # strict UTF-8, no surrogates
        doc = decodeJson(raw)
        assert doc["events"] == [value]
        assert doc["x"] == "\udfff"
        assert isCanonical(raw, doc)

    def test_extras_of_skips_schema_version_and_events_in_order(self) -> None:
        doc = {"events": [], "b": 1, "schemaVersion": 1, "a": 2}
        assert list(extrasOf(doc).items()) == [("b", 1), ("a", 2)]

    @pytest.mark.parametrize(
        "raw",
        [
            json.dumps({"schemaVersion": 1, "events": [{"a": 1}]}, indent=4).encode(),
            b"\xef\xbb\xbf" + serializeCanonical({}, [{"a": 1}]),
            serializeCanonical({}, [{"a": 1}]).rstrip(b"\n"),
            b'{"schemaVersion": 1, "events": [], "note": 1}\n',
            b'{"schemaVersion": 1, "events": [\n{"a": "\\u00e9"}\n]}\n',
        ],
        ids=["indent-4", "bom", "no-newline", "key-after-events", "ascii-escape"],
    )
    def test_is_canonical_rejects_other_layouts(self, raw: bytes) -> None:
        assert isCanonical(raw, decodeJson(raw)) is False

    def test_is_canonical_accepts_its_own_output(self) -> None:
        raw = serializeCanonical({"note": "kept"}, [{"a": 1}, {"b": 2}])
        assert isCanonical(raw, decodeJson(raw)) is True


# --- loadAtStartup ------------------------------------------------------------------------


class TestLoadAtStartup:
    def test_ac3_missing_file_creates_a_fresh_empty_log(self, env: Env) -> None:
        store = env.newStore()
        result = store.loadAtStartup()
        assert result == ActivityLoadResult(None, False, None)
        assert env.raw() == EMPTY_LOG
        env.assertValid()
        assert env.corruptFiles() == []
        assert store.lastWrittenBytes == EMPTY_LOG
        assert store.eventCount == 0

    def test_missing_data_directory_is_created(self, tmp_path: Path) -> None:
        paths = DataPaths(tmp_path / "new" / "data")
        store = ActivityLogStore(paths, FakeClock(NOW), logging.getLogger("pc_handoff.t"))
        assert store.loadAtStartup().usable is False
        assert paths.activityLog.read_bytes() == EMPTY_LOG

    def test_ac3_unparseable_file_is_backed_up_and_reset(self, env: Env) -> None:
        original = b'{"schemaVersion": 1, "events": [{"eventId": '
        env.path.write_bytes(original)

        result = env.newStore().loadAtStartup()

        backup = env.paths.dataDir / BACKUP_NAME
        assert env.corruptFiles() == [backup]
        assert sha(backup.read_bytes()) == sha(original)
        assert env.raw() == EMPTY_LOG
        env.assertValid()
        assert result.usable is False
        assert result.events is None
        assert result.notice is not None
        assert result.notice.fileName == "activity_log.json"
        assert result.notice.backupName == BACKUP_NAME
        assert result.notice.text == (
            f"activity_log.json was corrupt and has been reset (backup: {BACKUP_NAME})"
        )
        warnings = env.messages("WARNING")
        assert any("activity_log.json was corrupt" in m and BACKUP_NAME in m for m in warnings)

    def test_a_file_failing_the_pairing_check_is_corrupt(self, env: Env) -> None:
        start, end = period()
        env.writeEvents([start, replace(start, eventId=newId()), end])  # second start
        result = env.newStore().loadAtStartup()
        assert result.usable is False
        assert len(env.corruptFiles()) == 1
        assert env.raw() == EMPTY_LOG
        assert any("second session_start" in m for m in env.messages("WARNING"))

    @pytest.mark.parametrize(
        "raw",
        [b"[]", b'{"schemaVersion": 2, "events": []}', b'{"schemaVersion": 1, "events": {}}'],
        ids=["root-array", "schema-2", "events-object"],
    )
    def test_wrong_outer_structure_is_corrupt(self, env: Env, raw: bytes) -> None:
        env.path.write_bytes(raw)
        assert env.newStore().loadAtStartup().usable is False
        assert [p.read_bytes() for p in env.corruptFiles()] == [raw]

    def test_valid_canonical_log_is_loaded_and_untouched(self, env: Env) -> None:
        events = makeEvents(7, openPeriods=1)
        raw = env.writeEvents(events)
        mtime = env.path.stat().st_mtime_ns
        store = env.newStore()

        result = store.loadAtStartup()

        assert result == ActivityLoadResult(events, True, None)
        assert env.raw() == raw
        assert env.path.stat().st_mtime_ns == mtime
        assert store.lastWrittenBytes == raw
        assert store.eventCount == 7
        assert not any("normalized" in m for m in env.messages("INFO"))

    def test_pair_index_and_event_keys_are_built_from_the_file(self, env: Env) -> None:
        start, end = period()
        openStart = startEvent(newId(), T0 + timedelta(hours=1))
        orphanEnd = endEvent(newId(), T0, usagePeriodStart=T0 - timedelta(hours=1))
        store = env.started([start, end, openStart, orphanEnd])
        assert store.pairIndex == {
            start.usagePeriodId: PairEntry(T0, True, True),
            openStart.usagePeriodId: PairEntry(T0 + timedelta(hours=1), True, False),
            orphanEnd.usagePeriodId: PairEntry(None, False, True),
        }
        assert store.fileEventKeys == {
            (e.eventId, e.eventType.value, e.usagePeriodId)
            for e in (start, end, openStart, orphanEnd)
        }

    def test_ac10_indent_4_log_is_normalized_and_appended_on_the_fast_path(
        self, env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events = makeEvents(3, openPeriods=1)
        env.writeEvents(events, indent=4)
        store = env.newStore()

        assert store.loadAtStartup().usable is True

        assert env.corruptFiles() == []
        assert "activity_log.json normalized to canonical layout" in env.messages("INFO")
        dicts = [e.toJson() for e in events]
        assert env.raw() == serializeCanonical({}, dicts)

        calls: list[int] = []
        original = json.loads
        monkeypatch.setattr(json, "loads", lambda *a, **k: calls.append(1) or original(*a, **k))
        newStart = startEvent(newId(), T0)
        assert store.appendBatch([newStart]).written is True
        assert calls == []
        monkeypatch.setattr(json, "loads", original)
        env.assertValid()
        assert env.eventDicts() == [*dicts, newStart.toJson()]

    def test_ac11_extra_key_is_kept_and_events_stays_last(self, env: Env) -> None:
        a, b = makeEvents(2)
        original = {"events": [a.toJson(), b.toJson()], "schemaVersion": 1, "note": "kept"}
        env.path.write_bytes(json.dumps(original).encode())
        store = env.newStore()
        assert store.loadAtStartup().usable is True

        first, second = period(T0)
        third = startEvent(newId(), T0 + timedelta(hours=1))
        store.appendBatch([first])
        store.appendBatch([second, third])

        raw = env.raw()
        assert raw.startswith(b'{"schemaVersion": 1, "note": "kept", "events": [')
        assert raw.endswith(SUFFIX)
        env.assertValid()
        assert env.doc()["note"] == "kept"
        assert env.eventIds() == [e.eventId for e in (a, b, first, second, third)]

    def test_ac11_empty_log_without_newline_is_normalized(self, env: Env) -> None:
        env.path.write_bytes(b'{"schemaVersion": 1, "events": []}')
        store = env.newStore()
        assert store.loadAtStartup().usable is True
        assert env.raw().endswith(EMPTY_SUFFIX)
        assert env.raw() == EMPTY_LOG

        event = startEvent(newId(), T0)
        assert store.appendBatch([event]).written is True
        env.assertValid()
        assert env.raw() == serializeCanonical({}, [event.toJson()])

    def test_bom_and_escapes_change_bytes_but_not_values(self, env: Env) -> None:
        event = startEvent(newId(), T0, userName="josé")
        doc = {"schemaVersion": 1, "events": [event.toJson()]}
        env.path.write_bytes(b"\xef\xbb\xbf" + json.dumps(doc).encode("ascii"))
        store = env.newStore()
        result = store.loadAtStartup()
        assert result.usable is True
        assert result.events == [event]
        assert not env.raw().startswith(b"\xef\xbb\xbf")
        assert "josé".encode() in env.raw()
        assert env.doc() == doc

    def test_normalization_write_failure_keeps_going_with_no_cache(
        self, env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events = makeEvents(2)
        raw = env.writeEvents(events, indent=4)
        monkeypatch.setattr(
            activity_log_store, "atomicWriteBytes", failingWrite(PersistenceError("locked"))
        )
        store = env.newStore()

        result = store.loadAtStartup()

        assert result == ActivityLoadResult(events, True, None)
        assert env.raw() == raw
        assert store.lastWrittenBytes is None
        assert any("could not be normalized" in m for m in env.messages("WARNING"))

    def test_backup_failure_at_startup_leaves_the_file(
        self, env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env.path.write_bytes(b"garbage")

        def failing(path: Path, clock: FakeClock) -> Path:
            raise PermissionError("locked")

        monkeypatch.setattr(activity_log_store, "backupCorrupt", failing)
        store = env.newStore()
        result = store.loadAtStartup()

        assert env.raw() == b"garbage"
        assert result.usable is False
        assert result.notice is not None
        assert result.notice.backupName is None
        assert result.notice.text == "activity_log.json is corrupt and could not be backed up"
        assert any("could not be backed up" in m for m in env.messages("ERROR"))
        assert store.lastWrittenBytes is None

    def test_fresh_file_failure_after_backup_is_reported(
        self, env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env.path.write_bytes(b"garbage")
        monkeypatch.setattr(
            activity_log_store, "atomicWriteBytes", failingWrite(PersistenceError("disk full"))
        )
        store = env.newStore()
        result = store.loadAtStartup()

        assert not env.path.exists()
        assert result.notice is not None
        assert result.notice.backupName == BACKUP_NAME
        assert result.notice.text == (
            f"activity_log.json was corrupt and has been moved to {BACKUP_NAME}; "
            "a new file will be created at the next write"
        )
        assert any("could not be created" in m for m in env.messages("ERROR"))

        # The next append creates the file without a second ("missing") notice.
        monkeypatch.setattr(activity_log_store, "atomicWriteBytes", atomic.atomicWriteBytes)
        event = startEvent(newId(), T0)
        assert store.appendBatch([event]) == AppendResult(True, None)
        assert env.eventIds() == [event.eventId]

    def test_missing_file_that_cannot_be_created_is_recreated_quietly_later(
        self, env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            activity_log_store, "atomicWriteBytes", failingWrite(PersistenceError("disk full"))
        )
        store = env.newStore()
        assert store.loadAtStartup() == ActivityLoadResult(None, False, None)
        assert not env.path.exists()
        assert any("could not be created" in m for m in env.messages("ERROR"))

        monkeypatch.setattr(activity_log_store, "atomicWriteBytes", atomic.atomicWriteBytes)
        event = startEvent(newId(), T0)
        assert store.appendBatch([event]) == AppendResult(True, None)
        assert env.eventIds() == [event.eventId]

    def test_unreadable_file_is_not_usable(self, env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
        env.writeEvents(makeEvents(2))

        def failing(path: Path) -> bytes:
            raise PermissionError("denied")

        monkeypatch.setattr(activity_log_store, "readBytes", failing)
        store = env.newStore()
        assert store.loadAtStartup() == ActivityLoadResult(None, False, None)
        assert env.corruptFiles() == []
        assert any("activity_log.json could not be read" in m for m in env.messages("ERROR"))

    def test_g3_validation_runs_before_from_json(
        self, env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        start = startEvent(newId(), T0)
        doc = {"schemaVersion": 1, "events": [{**start.toJson(), "sessionId": True}]}
        env.path.write_bytes(json.dumps(doc).encode())
        converted: list[Any] = []
        monkeypatch.setattr(
            activity_log_store.ActivityEvent,
            "fromJson",
            classmethod(lambda cls, d: converted.append(d)),
        )
        result = env.newStore().loadAtStartup()
        assert result.usable is False  # bool is not an integer (G5)
        assert converted == []

    def test_conversion_failure_after_validation_counts_as_corrupt(
        self, env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env.writeEvents(makeEvents(2))

        def failing(cls: type, doc: dict) -> ActivityEvent:
            raise ValueError("unexpected")

        monkeypatch.setattr(activity_log_store.ActivityEvent, "fromJson", classmethod(failing))
        result = env.newStore().loadAtStartup()
        assert result.usable is False
        assert len(env.corruptFiles()) == 1
        assert env.raw() == EMPTY_LOG


# --- appendBatch --------------------------------------------------------------------------


class TestAppend:
    def test_ac4_existing_event_lines_are_byte_identical(self, env: Env) -> None:
        store = env.started(makeEvents(10, openPeriods=1))
        before = env.raw()
        openId = next(k for k, v in store.pairIndex.items() if not v.hasEnd)
        startTs = store.pairIndex[openId].startTimestamp
        assert startTs is not None
        end = endEvent(openId, startTs + timedelta(minutes=5), usagePeriodStart=startTs)
        start = startEvent(newId(), startTs + timedelta(minutes=5))

        assert store.appendBatch([end, start]) == AppendResult(True, None)

        after = env.raw()
        assert eventLines(after)[:10] == eventLines(before)
        assert after.startswith(before[: -len(SUFFIX)])
        assert env.eventDicts()[10:] == [end.toJson(), start.toJson()]
        assert len(env.eventDicts()) == 12
        env.assertValid()
        assert store.pendingCount() == 0

    def test_ac8_fast_path_does_not_parse(self, env: Env, loadsCalls: list[int]) -> None:
        store = env.started(makeEvents(4))
        loadsCalls.clear()
        first, second = period()
        store.appendBatch([first])
        store.appendBatch([second])
        assert loadsCalls == []
        env.assertValid()

    def test_first_append_to_an_empty_log_uses_empty_suffix_slicing(
        self, env: Env, loadsCalls: list[int]
    ) -> None:
        store = env.newStore()
        store.loadAtStartup()
        loadsCalls.clear()
        event = startEvent(newId(), T0)
        store.appendBatch([event])
        assert loadsCalls == []
        assert env.raw() == serializeCanonical({}, [event.toJson()])

    def test_ac5_valid_external_edit_is_adopted_without_backup(
        self, env: Env, loadsCalls: list[int]
    ) -> None:
        store = env.started(makeEvents(2))
        replacement = makeEvents(5, openPeriods=1)
        env.writeEvents(replacement, indent=4)
        loadsCalls.clear()
        event = startEvent(newId(), T0)

        assert store.appendBatch([event]) == AppendResult(True, None)

        assert loadsCalls != []  # slow path
        assert env.corruptFiles() == []
        assert env.eventDicts() == [e.toJson() for e in replacement] + [event.toJson()]
        assert env.raw() == serializeCanonical({}, env.eventDicts())
        env.assertValid()
        # The cache now matches the file again: the next append is on the fast path.
        loadsCalls.clear()
        store.appendBatch([startEvent(newId(), T0)])
        assert loadsCalls == []

    @pytest.mark.parametrize(
        "damage",
        [b'{"schemaVersion": 1, "events": {}}', b'{"schemaVersion": 1, "events": [{"ev'],
        ids=["events-object", "truncated"],
    )
    def test_ac5_damaged_outer_structure_is_backed_up_at_runtime(
        self, env: Env, damage: bytes
    ) -> None:
        store = env.started(makeEvents(4))
        env.path.write_bytes(damage)
        first, second = period()

        result = store.appendBatch([first, second])

        backup = env.paths.dataDir / BACKUP_NAME
        assert env.corruptFiles() == [backup]
        assert backup.read_bytes() == damage
        assert env.eventDicts() == [first.toJson(), second.toJson()]
        env.assertValid()
        assert result.written is True
        assert result.notice is not None
        assert result.notice.backupName == BACKUP_NAME
        assert result.notice.text == (
            "activity_log.json was damaged while running and has been reset "
            f"(backup: {BACKUP_NAME})"
        )
        assert any("damaged while running" in m for m in env.messages("WARNING"))

    def test_runtime_reset_writes_every_pending_event(
        self, env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = env.started(makeEvents(2))
        first, second = period()
        monkeypatch.setattr(
            activity_log_store, "atomicWriteBytes", failingWrite(PersistenceError("locked"))
        )
        assert store.appendBatch([first]).written is False
        monkeypatch.setattr(activity_log_store, "atomicWriteBytes", atomic.atomicWriteBytes)
        env.path.write_bytes(b"not json")

        result = store.appendBatch([second])

        assert result.notice is not None
        assert env.eventIds() == [first.eventId, second.eventId]

    def test_runtime_backup_failure_keeps_the_queue_and_the_file(
        self, env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = env.started(makeEvents(2))
        env.path.write_bytes(b"not json")

        def failing(path: Path, clock: FakeClock) -> Path:
            raise PermissionError("locked")

        monkeypatch.setattr(activity_log_store, "backupCorrupt", failing)
        event = startEvent(newId(), T0)

        result = store.appendBatch([event])
        assert result.written is False
        assert result.notice == StartupNotice("activity_log.json", None, BACKUP_FAILED_TEXT)
        assert env.raw() == b"not json"
        assert store.pendingEvents == [event]
        assert any("could not be backed up" in m for m in env.messages("ERROR"))

    def test_write_failure_after_runtime_backup_still_returns_the_notice(
        self, env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = env.started(makeEvents(2))
        env.path.write_bytes(b"not json")
        monkeypatch.setattr(
            activity_log_store, "atomicWriteBytes", failingWrite(PersistenceError("locked"))
        )
        event = startEvent(newId(), T0)

        result = store.appendBatch([event])

        assert result.written is False
        assert result.notice is not None
        assert result.notice.backupName == BACKUP_NAME
        assert not env.path.exists()
        monkeypatch.setattr(activity_log_store, "atomicWriteBytes", atomic.atomicWriteBytes)
        # The file is now missing because of the backup: recreated without a second notice.
        assert store.appendBatch([]) == AppendResult(True, None)
        assert env.eventIds() == [event.eventId]
        env.assertValid()

    def test_ac6_failed_write_keeps_events_pending_and_retries_in_order(
        self, env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = env.started(makeEvents(2))
        before = env.raw()
        e1, e2 = period()
        monkeypatch.setattr(
            activity_log_store, "atomicWriteBytes", failingWrite(PersistenceError("locked"))
        )

        assert store.appendBatch([e1]) == AppendResult(False, None)
        assert env.raw() == before
        assert store.pendingEvents == [e1]
        assert store.pendingCount() == 1
        assert any("could not be written" in m for m in env.messages("ERROR"))

        monkeypatch.setattr(activity_log_store, "atomicWriteBytes", atomic.atomicWriteBytes)
        assert store.appendBatch([e2]) == AppendResult(True, None)
        ids = env.eventIds()
        assert ids[2:] == [e1.eventId, e2.eventId]
        assert len(ids) == len(set(ids)) == 4
        assert store.pendingCount() == 0
        env.assertValid()

    def test_read_failure_at_runtime_keeps_the_queue(
        self, env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = env.started(makeEvents(2))
        before = env.raw()

        def failing(path: Path) -> bytes:
            raise PermissionError("denied")

        monkeypatch.setattr(activity_log_store, "readBytes", failing)
        event = startEvent(newId(), T0)
        assert store.appendBatch([event]) == AppendResult(False, None)
        assert store.pendingEvents == [event]
        assert env.raw() == before
        assert any("could not be read" in m for m in env.messages("ERROR"))

    def test_ac12_deleted_log_is_recreated_without_backup(self, env: Env) -> None:
        store = env.started(makeEvents(4))
        env.path.unlink()
        event = startEvent(newId(), T0)

        result = store.appendBatch([event])

        assert result.written is True
        assert result.notice is not None
        assert result.notice.backupName is None
        assert result.notice.text == "activity_log.json was missing and has been recreated"
        assert env.eventDicts() == [event.toJson()]
        assert env.corruptFiles() == []
        assert any("was missing" in m for m in env.messages("WARNING"))
        env.assertValid()

    def test_deleted_data_directory_is_recreated(self, env: Env) -> None:
        store = env.started([])
        env.path.unlink()
        env.paths.dataDir.rmdir()
        event = startEvent(newId(), T0)
        assert store.appendBatch([event]).written is True
        assert env.eventIds() == [event.eventId]

    def test_suffix_mismatch_falls_back_to_the_slow_path(
        self, env: Env, loadsCalls: list[int]
    ) -> None:
        store = env.started(makeEvents(2))
        broken = env.raw().rstrip(b"\n")  # valid JSON, but not the cached canonical suffix
        env.path.write_bytes(broken)
        store.lastWrittenBytes = broken
        loadsCalls.clear()
        event = startEvent(newId(), T0)

        assert store.appendBatch([event]).written is True

        assert loadsCalls != []
        assert any("canonical suffix" in m for m in env.messages("ERROR"))
        assert env.raw().endswith(SUFFIX)
        assert env.eventIds()[-1] == event.eventId
        env.assertValid()

    def test_empty_suffix_mismatch_falls_back_to_the_slow_path(self, env: Env) -> None:
        store = env.started([])
        broken = b'{"schemaVersion": 1, "events": []}'
        env.path.write_bytes(broken)
        store.lastWrittenBytes = broken
        event = startEvent(newId(), T0)
        assert store.appendBatch([event]).written is True
        assert env.raw() == serializeCanonical({}, [event.toJson()])

    def test_empty_batch_on_the_fast_path_does_not_write(
        self, env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = env.started(makeEvents(2))
        monkeypatch.setattr(
            activity_log_store, "atomicWriteBytes", failingWrite(AssertionError("no write"))
        )
        assert store.appendBatch([]) == AppendResult(True, None)

    def test_empty_batch_on_the_slow_path_normalizes(self, env: Env) -> None:
        store = env.started(makeEvents(2))
        env.writeEvents(makeEvents(3), indent=4)
        assert store.appendBatch([]) == AppendResult(True, None)
        assert env.raw() == serializeCanonical({}, env.eventDicts())
        assert store.lastWrittenBytes == env.raw()
        assert store.eventCount == 3

    def test_enqueue_pending_is_written_by_the_next_append(self, env: Env) -> None:
        store = env.started([])
        first, second = period()
        store.enqueuePending([first])
        assert store.pendingCount() == 1
        assert env.raw() == EMPTY_LOG
        store.appendBatch([second])
        assert env.eventIds() == [first.eventId, second.eventId]
        assert store.pendingCount() == 0


# --- AC-13: idempotent retry ----------------------------------------------------------------


class RaiseAfterReplace:
    """Wraps ``atomic.replaceWithRetry``: the replace happens, then ``RuntimeError``."""

    def __init__(self, times: int = 1) -> None:
        self.times = times
        self.original = atomic.replaceWithRetry

    def __call__(self, source: Path, target: Path) -> None:
        self.original(source, target)
        if self.times > 0:
            self.times -= 1
            raise RuntimeError("injected after os.replace")


class TestIdempotentRetry:
    def test_ac13_exception_after_os_replace_never_duplicates(
        self, env: Env, monkeypatch: pytest.MonkeyPatch, loadsCalls: list[int]
    ) -> None:
        events = makeEvents(4, openPeriods=1)
        store = env.started(events)
        openStart = events[-1]
        end1 = endEvent(
            openStart.usagePeriodId,
            openStart.timestamp + timedelta(minutes=5),
            usagePeriodStart=openStart.usagePeriodStart,
        )
        start2 = startEvent(newId(), openStart.timestamp + timedelta(minutes=5))
        monkeypatch.setattr(atomic, "replaceWithRetry", RaiseAfterReplace())

        with pytest.raises(RuntimeError, match="after os.replace"):
            store.appendBatch([end1, start2])

        assert env.eventIds()[-2:] == [end1.eventId, start2.eventId]  # the replace happened
        assert store.pendingEvents == [end1, start2]
        loadsCalls.clear()
        e3 = startEvent(newId(), openStart.timestamp + timedelta(minutes=6))

        assert store.appendBatch([e3]) == AppendResult(True, None)

        assert loadsCalls != []  # slow path: the bytes differ from the stale cache
        skipped = [m for m in env.messages("INFO") if "already persisted" in m]
        assert len(skipped) == 2
        assert any(end1.eventId in m for m in skipped)
        assert any(start2.eventId in m for m in skipped)
        ids = env.eventIds()
        assert len(ids) == 4 + 3
        assert len(set(ids)) == len(ids)
        assert ids[-3:] == [end1.eventId, start2.eventId, e3.eventId]
        assert store.pendingEvents == []
        env.assertValid()
        # A restart accepts the file as it is.
        restarted = env.newStore()
        assert restarted.loadAtStartup().usable is True
        assert env.corruptFiles() == []

    def test_ac13_exception_inside_the_guard_keeps_the_queue(
        self, env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = env.started(makeEvents(2))
        before = env.raw()
        e1, e2 = period()
        original = activity_log_store.screenEvents
        calls: list[int] = []

        def exploding(*args: Any, **kwargs: Any) -> Any:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("injected in the guard")
            return original(*args, **kwargs)

        monkeypatch.setattr(activity_log_store, "screenEvents", exploding)
        with pytest.raises(RuntimeError, match="in the guard"):
            store.appendBatch([e1])
        assert env.raw() == before
        assert store.pendingEvents == [e1]

        assert store.appendBatch([e2]).written is True
        assert env.eventIds()[2:] == [e1.eventId, e2.eventId]
        env.assertValid()

    def test_ac13_failed_normalization_is_redone_by_the_first_append(
        self, env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events = makeEvents(3, openPeriods=1)
        env.writeEvents(events, indent=4)
        monkeypatch.setattr(
            activity_log_store, "atomicWriteBytes", failingWrite(PersistenceError("locked"))
        )
        store = env.newStore()
        store.loadAtStartup()
        monkeypatch.setattr(activity_log_store, "atomicWriteBytes", atomic.atomicWriteBytes)
        event = startEvent(newId(), T0)

        assert store.appendBatch([event]).written is True

        dicts = [e.toJson() for e in events] + [event.toJson()]
        assert env.raw() == serializeCanonical({}, dicts)
        assert env.eventDicts()[:3] == [e.toJson() for e in events]
        env.assertValid()

    def test_cache_values_are_assigned_with_last_written_bytes_last(self, env: Env) -> None:
        order: list[str] = []

        class Recording(ActivityLogStore):
            def __setattr__(self, name: str, value: Any) -> None:
                order.append(name)
                super().__setattr__(name, value)

        env.writeEvents(makeEvents(2))
        store = Recording(env.paths, env.clock, logging.getLogger("pc_handoff.t"))
        store.loadAtStartup()
        order.clear()
        store.appendBatch([startEvent(newId(), T0)])
        assert order[-5:] == [
            "pairIndex",
            "fileEventKeys",
            "eventCount",
            "lastWrittenBytes",
            "pendingEvents",
        ]

    @pytest.mark.parametrize("failAt", ["pairIndex", "fileEventKeys", "eventCount"])
    def test_interruption_between_cache_assignments_cannot_enable_a_stale_fast_path(
        self, env: Env, failAt: str
    ) -> None:
        armed: list[bool] = []

        class Interrupted(ActivityLogStore):
            def __setattr__(self, name: str, value: Any) -> None:
                if armed and name == failAt:
                    armed.clear()
                    raise RuntimeError(f"interrupted at {name}")
                super().__setattr__(name, value)

        env.writeEvents(makeEvents(2))
        store = Interrupted(env.paths, env.clock, logging.getLogger("pc_handoff.t"))
        store.loadAtStartup()
        start, end = period()
        armed.append(True)
        with pytest.raises(RuntimeError, match="interrupted"):
            store.appendBatch([start])

        # The same events again, plus the end: start must not be written twice.
        store.appendBatch([end])
        ids = env.eventIds()
        assert ids[2:] == [start.eventId, end.eventId]
        env.assertValid()


# --- pairing guard (F1, F2, G2/G4) --------------------------------------------------------


class TestPairingGuard:
    def test_ac7_second_end_is_refused_and_the_file_is_unchanged(self, env: Env) -> None:
        start, end = period()
        store = env.started([start, end])
        before = env.raw()
        again = endEvent(start.usagePeriodId, T0 + timedelta(hours=1), usagePeriodStart=T0)

        result = store.appendBatch([again])

        assert result == AppendResult(True, None)
        assert sha(env.raw()) == sha(before)
        assert store.pendingCount() == 0
        errors = env.messages("ERROR")
        assert any("second session_end" in m and again.eventId in m for m in errors)

    def test_f1_start_after_an_end_in_the_file_is_refused(self, env: Env) -> None:
        # The end's start lives in a backup (AC-18 shape); a start for that id must not follow.
        periodId = newId()
        orphanEnd = endEvent(periodId, T0 + timedelta(hours=1), usagePeriodStart=T0)
        store = env.started([orphanEnd])
        before = env.raw()
        late = startEvent(periodId, T0)  # earlier than the end: the validator would accept it

        store.appendBatch([late])

        assert env.raw() == before
        assert any(
            "session_start after its session_end" in m and late.eventId in m
            for m in env.messages("ERROR")
        )
        # Proof that this is stricter than the startup check, as F1 requires.
        doc = {"schemaVersion": 1, "events": [orphanEnd.toJson(), late.toJson()]}
        assert validateActivityDocument(doc) == []

    def test_f1_start_after_its_end_in_the_same_batch_is_refused(self, env: Env) -> None:
        store = env.started([])
        periodId = newId()
        end = endEvent(periodId, T0, usagePeriodStart=T0 - timedelta(hours=1))
        start = startEvent(periodId, T0 + timedelta(minutes=1))
        store.appendBatch([end, start])
        assert env.eventIds() == [end.eventId]
        assert any("session_start after its session_end" in m for m in env.messages("ERROR"))
        env.assertValid()

    def test_f1_start_whose_end_would_fail_the_validator_is_refused(self, env: Env) -> None:
        periodId = newId()
        orphanEnd = endEvent(periodId, T0, usagePeriodStart=T0 - timedelta(hours=1))
        store = env.started([orphanEnd])
        late = startEvent(periodId, T0 + timedelta(minutes=1))
        store.appendBatch([late])
        env.assertValid()
        assert env.eventIds() == [orphanEnd.eventId]

    def test_g2_second_start_is_refused(self, env: Env) -> None:
        start = startEvent(newId(), T0)
        store = env.started([start])
        again = startEvent(start.usagePeriodId, T0 + timedelta(minutes=1))
        store.appendBatch([again])
        assert env.eventIds() == [start.eventId]
        assert any("second session_start" in m for m in env.messages("ERROR"))

    def test_g2_second_start_and_end_within_one_batch_are_refused(self, env: Env) -> None:
        store = env.started([])
        start, end = period()
        dupStart = replace(start, eventId=newId())
        dupEnd = replace(end, eventId=newId())
        store.appendBatch([start, dupStart, end, dupEnd])
        assert env.eventIds() == [start.eventId, end.eventId]
        assert len([m for m in env.messages("ERROR") if "refused" in m]) == 2
        env.assertValid()

    def test_ac12_end_before_its_start_is_refused(self, env: Env) -> None:
        startAt = datetime(2026, 10, 21, 10, 0, 3, tzinfo=JST)
        start = startEvent(newId(), startAt)
        store = env.started([start])
        assert store.pairIndex[start.usagePeriodId].startTimestamp == startAt
        before = env.raw()
        early = endEvent(
            start.usagePeriodId,
            datetime(2026, 10, 21, 10, 0, 0, tzinfo=JST),
            usagePeriodStart=startAt,
            periodEnd=startAt,
        )

        assert store.appendBatch([early]) == AppendResult(True, None)

        assert env.raw() == before
        errors = env.messages("ERROR")
        assert any("precedes its session_start" in m and early.eventId in m for m in errors)

    def test_end_before_start_is_compared_across_utc_offsets(self, env: Env) -> None:
        start = startEvent(newId(), datetime(2026, 10, 21, 10, 0, 3, tzinfo=JST))
        store = env.started([start])
        utcEnd = datetime(2026, 10, 21, 1, 0, 0, tzinfo=UTC)  # 10:00:00+09:00
        early = endEvent(
            start.usagePeriodId, utcEnd, usagePeriodStart=start.timestamp, periodEnd=None
        )
        store.appendBatch([early])
        assert env.eventIds() == [start.eventId]

    def test_end_before_start_in_the_same_batch_is_refused(self, env: Env) -> None:
        store = env.started([])
        start = startEvent(newId(), T0)
        early = endEvent(
            start.usagePeriodId, T0 - timedelta(seconds=1), usagePeriodStart=T0, periodEnd=None
        )
        store.appendBatch([start, early])
        assert env.eventIds() == [start.eventId]

    def test_end_at_the_same_second_as_its_start_is_written(self, env: Env) -> None:
        start = startEvent(newId(), T0)
        store = env.started([start])
        end = endEvent(start.usagePeriodId, T0, usagePeriodStart=T0)
        store.appendBatch([end])
        assert env.eventIds() == [start.eventId, end.eventId]
        env.assertValid()

    def test_end_without_its_start_in_the_file_is_written(self, env: Env) -> None:
        # AC-18: the start lives in a .corrupt-* backup.
        store = env.started(makeEvents(2))
        end = endEvent(newId(), T0, usagePeriodStart=T0 - timedelta(hours=2))
        assert store.appendBatch([end]).written is True
        assert env.eventIds()[-1] == end.eventId
        env.assertValid()

    def test_f2_same_event_already_in_the_file_is_skipped(self, env: Env) -> None:
        start = startEvent(newId(), T0)
        store = env.started([start])
        before = env.raw()
        store.appendBatch([start])
        assert env.raw() == before
        assert any("already persisted" in m and start.eventId in m for m in env.messages("INFO"))
        assert not any("refused" in m for m in env.messages("ERROR"))

    def test_f2_duplicate_event_id_in_another_period_is_written(self, env: Env) -> None:
        start = startEvent(newId(), T0)
        store = env.started([start])
        other = startEvent(newId(), T0 + timedelta(minutes=1), eventId=start.eventId)

        store.appendBatch([other])

        assert env.eventIds() == [start.eventId, start.eventId]
        env.assertValid()
        assert not any("already persisted" in m for m in env.messages("INFO"))

    def test_f2_duplicate_event_id_with_another_type_in_the_same_period_is_written(
        self, env: Env
    ) -> None:
        start = startEvent(newId(), T0)
        store = env.started([start])
        end = endEvent(
            start.usagePeriodId,
            T0 + timedelta(minutes=5),
            usagePeriodStart=T0,
            eventId=start.eventId,
        )
        store.appendBatch([end])
        assert [e["eventType"] for e in env.eventDicts()] == ["session_start", "session_end"]
        env.assertValid()

    def test_f2_skip_survives_the_slow_path(self, env: Env) -> None:
        start = startEvent(newId(), T0)
        store = env.started([start])
        env.writeEvents([start], indent=2)  # external reformat: slow path
        store.appendBatch([start])
        assert env.eventIds() == [start.eventId]
        assert any("already persisted" in m for m in env.messages("INFO"))

    def test_index_is_rebuilt_from_an_externally_edited_file(self, env: Env) -> None:
        store = env.started([])
        start = startEvent(newId(), T0)
        env.writeEvents([start], indent=2)  # added outside the store
        again = startEvent(start.usagePeriodId, T0 + timedelta(minutes=1))
        store.appendBatch([again])
        assert env.eventIds() == [start.eventId]
        assert any("second session_start" in m for m in env.messages("ERROR"))

    def test_index_tolerates_malformed_events_on_the_slow_path(self, env: Env) -> None:
        store = env.started([])
        start = startEvent(newId(), T0)
        broken = {**start.toJson(), "timestamp": "yesterday"}
        env.path.write_bytes(
            json.dumps({"schemaVersion": 1, "events": [42, {"eventType": 1}, broken]}).encode()
        )
        end = endEvent(start.usagePeriodId, T0, usagePeriodStart=T0)
        assert store.appendBatch([end]).written is True  # start time unknown: allowed
        assert store.pairIndex[start.usagePeriodId] == PairEntry(None, True, True)
        assert env.eventDicts()[:3] == [42, {"eventType": 1}, broken]

    def test_build_index_counts_every_recognisable_event(self) -> None:
        periodId = newId()
        first = startEvent(periodId, T0).toJson()
        second = startEvent(periodId, T0 + timedelta(hours=1)).toJson()
        noId = {**endEvent(periodId, T0, usagePeriodStart=T0).toJson(), "eventId": None}
        unknownType = {**first, "eventType": "session_pause", "usagePeriodId": newId()}
        index, keys = buildIndex([first, second, noId, unknownType])
        # The first start's timestamp is kept, as by the validator's pairing check.
        assert index == {periodId: PairEntry(T0, True, True)}
        assert keys == {
            (first["eventId"], "session_start", periodId),
            (second["eventId"], "session_start", periodId),
        }

    @pytest.mark.parametrize(
        "badEvent",
        [
            replace(endEvent(newId(), T0, usagePeriodStart=T0), endCause=None),
            replace(endEvent(newId(), T0, usagePeriodStart=T0), durationSeconds=-5),
            replace(startEvent(newId(), T0), usagePeriodId="not-a-uuid"),
            replace(startEvent(newId(), T0), userName=""),
        ],
        ids=["null-end-cause", "negative-duration", "bad-period-id", "empty-user"],
    )
    def test_g2_events_failing_the_schema_are_refused(
        self, env: Env, badEvent: ActivityEvent
    ) -> None:
        store = env.started([])
        good = startEvent(newId(), T0)
        store.appendBatch([badEvent, good])
        assert env.eventIds() == [good.eventId]
        env.assertValid()
        assert any("refused" in m and "invalid" in m for m in env.messages("ERROR"))

    def test_an_event_that_cannot_be_serialized_is_refused(self, env: Env) -> None:
        store = env.started([])
        naive = replace(startEvent(newId(), T0), timestamp=datetime(2026, 10, 21, 10, 0, 0))
        wrongType = replace(startEvent(newId(), T0), eventType="session_start")
        store.appendBatch([naive, wrongType])
        assert env.raw() == EMPTY_LOG
        errors = [m for m in env.messages("ERROR") if "cannot be serialized" in m]
        assert len(errors) == 2

    def test_refused_events_leave_the_queue(self, env: Env, monkeypatch) -> None:
        start = startEvent(newId(), T0)
        store = env.started([start])
        again = startEvent(start.usagePeriodId, T0)
        fresh = startEvent(newId(), T0)
        monkeypatch.setattr(
            activity_log_store, "atomicWriteBytes", failingWrite(PersistenceError("locked"))
        )
        store.appendBatch([again, fresh])
        assert store.pendingEvents == [fresh]


class TestGuardAgreesWithTheStartupCheck:
    """On a valid file, an event is accepted exactly when the file with it appended passes
    ``validateActivityDocument``, except F1: a start after its period's end is always refused.
    """

    def test_exhaustive_single_event_cases(self, env: Env) -> None:
        startAt = T0
        cases = []
        for fileShape in ("none", "start", "end", "both"):
            for eventType in EventType:
                for offset in (-60, 0, 60):
                    cases.append((fileShape, eventType, offset))
        checked = 0
        for index, (fileShape, eventType, offset) in enumerate(cases):
            periodId = newId()
            fileEvents: list[ActivityEvent] = []
            if fileShape in ("start", "both"):
                fileEvents.append(startEvent(periodId, startAt))
            if fileShape in ("end", "both"):
                fileEvents.append(
                    endEvent(periodId, startAt + timedelta(minutes=30), usagePeriodStart=startAt)
                )
            moment = startAt + timedelta(seconds=offset)
            if eventType is EventType.SESSION_START:
                candidate = startEvent(periodId, moment)
            else:
                candidate = endEvent(periodId, moment, usagePeriodStart=startAt, periodEnd=None)
            env.path.unlink(missing_ok=True)
            store = env.started(fileEvents)
            before = env.raw()

            store.appendBatch([candidate])

            accepted = env.raw() != before
            combined = {
                "schemaVersion": 1,
                "events": [e.toJson() for e in fileEvents] + [candidate.toJson()],
            }
            validatorAccepts = validateActivityDocument(combined) == []
            isF1 = eventType is EventType.SESSION_START and fileShape == "end"
            if isF1:
                assert not accepted, (index, fileShape, eventType, offset)
            else:
                assert accepted == validatorAccepts, (index, fileShape, eventType, offset)
            env.assertValid()
            checked += 1
        assert checked == 24

    def test_makeevents_logs_are_valid(self) -> None:
        for n, k in ((0, 0), (1, 0), (1, 1), (10, 3), (11, 2)):
            events = makeEvents(n, openPeriods=k)
            doc = {"schemaVersion": 1, "events": [e.toJson() for e in events]}
            assert len(events) == n
            assert validateActivityDocument(doc) == []
            starts = {e.usagePeriodId for e in events if e.eventType is EventType.SESSION_START}
            ends = {e.usagePeriodId for e in events if e.eventType is EventType.SESSION_END}
            assert len(starts - ends) == k
        with pytest.raises(ValueError):
            makeEvents(1, openPeriods=2)

    def test_log_base_time_is_before_the_test_times(self) -> None:
        assert LOG_BASE_TIME < T0


# --- post-PASS fixes (Critic TASK-006 S1, S3, S4) -------------------------------------------


def failingBackup(calls: list[int] | None = None) -> Callable[[Path, FakeClock], Path]:
    def backup(path: Path, clock: FakeClock) -> Path:
        if calls is not None:
            calls.append(1)
        raise PermissionError("locked by another program")

    return backup


class TestRuntimeResetDetails:
    """S1: behaviours named in the Technical Approach that survived mutation (M9, M23, M24,
    M27)."""

    def test_m24_extra_key_survives_a_runtime_adoption(self, env: Env) -> None:
        a, b = makeEvents(2)
        env.path.write_bytes(serializeCanonical({"note": "kept"}, [a.toJson(), b.toJson()]))
        store = env.newStore()
        assert store.loadAtStartup().usable is True
        external = {"schemaVersion": 1, "note": "kept", "events": [a.toJson(), b.toJson()]}
        env.path.write_bytes(json.dumps(external, indent=4).encode())  # slow path next
        event = startEvent(newId(), T0)

        assert store.appendBatch([event]).written is True

        raw = env.raw()
        assert raw.startswith(b'{"schemaVersion": 1, "note": "kept", "events": [\n')
        assert raw == serializeCanonical({"note": "kept"}, env.eventDicts())
        assert env.doc()["note"] == "kept"
        env.assertValid()

    @pytest.mark.parametrize("change", ["damage", "delete"])
    def test_m9_m23_index_is_reset_when_the_file_is_replaced(self, env: Env, change: str) -> None:
        # U1's start at 10:00:03 was in the old file. After a reset it lives only in the
        # backup (or nowhere), so an end at 10:00:00 is an end without a start: written,
        # exactly as for AC-18.
        startAt = datetime(2026, 10, 21, 10, 0, 3, tzinfo=JST)
        start = startEvent(newId(), startAt)
        store = env.started([start])
        if change == "damage":
            env.path.write_bytes(b'{"schemaVersion": 1, "events": [{')
        else:
            env.path.unlink()
        end = endEvent(
            start.usagePeriodId,
            datetime(2026, 10, 21, 10, 0, 0, tzinfo=JST),
            usagePeriodStart=startAt,
            periodEnd=startAt,
        )

        assert store.appendBatch([end]).written is True

        assert env.eventIds() == [end.eventId]
        assert store.pairIndex == {start.usagePeriodId: PairEntry(None, False, True)}
        assert store.fileEventKeys == {(end.eventId, "session_end", start.usagePeriodId)}
        assert store.eventCount == 1
        assert not any("refused" in m for m in env.messages("ERROR"))
        env.assertValid()

    def test_m27_non_ascii_values_on_the_fast_path_stay_canonical(
        self, env: Env, loadsCalls: list[int]
    ) -> None:
        store = env.started(makeEvents(2))
        loadsCalls.clear()
        event = startEvent(newId(), T0, userName="zoë", clientName="ПК-1")

        store.appendBatch([event])

        assert loadsCalls == []  # fast path
        raw = env.raw()
        assert "zoë".encode() in raw
        assert b"\\u00eb" not in raw
        assert raw == serializeCanonical({}, env.eventDicts())
        assert raw.endswith(encodeJson(event.toJson()) + SUFFIX)
        assert isCanonical(raw, env.doc())


class TestBackupFailureEpisodes:
    """S3: a damaged log that cannot be backed up gives one notice, rate-limited ERROR lines
    and an INFO line on recovery; the events stay queued and nothing is written."""

    def test_s3_notice_once_and_errors_rate_limited(
        self, env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = env.started(makeEvents(2))
        store.backupFailureLogEvery = 3
        env.path.write_bytes(b"not json")
        calls: list[int] = []
        monkeypatch.setattr(activity_log_store, "backupCorrupt", failingBackup(calls))
        events = [startEvent(newId(), T0 + timedelta(minutes=i)) for i in range(7)]

        results = [store.appendBatch([e]) for e in events]

        assert len(calls) == 7  # the backup is retried on every append
        assert [r.written for r in results] == [False] * 7
        assert results[0].notice == StartupNotice("activity_log.json", None, BACKUP_FAILED_TEXT)
        assert [r.notice for r in results[1:]] == [None] * 6
        errors = [m for m in env.messages("ERROR") if "could not be backed up" in m]
        assert len(errors) == 3  # attempts 1, 4 and 7
        assert "attempt 1" in errors[0]
        assert "attempt 4" in errors[1]
        assert "attempt 7" in errors[2]
        debug = [m for m in env.messages("DEBUG") if "could not be backed up" in m]
        assert len(debug) == 4
        assert env.raw() == b"not json"
        assert store.pendingEvents == events

        monkeypatch.setattr(activity_log_store, "backupCorrupt", atomic.backupCorrupt)
        last = startEvent(newId(), T0 + timedelta(hours=1))
        result = store.appendBatch([last])

        assert result.written is True
        assert result.notice is not None
        assert result.notice.backupName == BACKUP_NAME
        assert (env.paths.dataDir / BACKUP_NAME).read_bytes() == b"not json"
        assert env.eventIds() == [e.eventId for e in [*events, last]]
        infos = env.messages("INFO")
        assert any(f"was backed up as {BACKUP_NAME} after 7 failed" in m for m in infos)
        env.assertValid()

    def test_s3_default_rate_limit(self) -> None:
        assert ActivityLogStore.backupFailureLogEvery == 60

    def test_s3_episode_ends_when_the_file_is_repaired_externally(
        self, env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events = makeEvents(2)
        store = env.started(events)
        env.path.write_bytes(b"not json")
        monkeypatch.setattr(activity_log_store, "backupCorrupt", failingBackup())
        first = startEvent(newId(), T0)
        assert store.appendBatch([first]).notice is not None

        env.writeEvents(events, indent=2)  # repaired by someone, still valid
        second = startEvent(newId(), T0 + timedelta(minutes=1))
        assert store.appendBatch([second]) == AppendResult(True, None)
        assert env.eventIds()[2:] == [first.eventId, second.eventId]
        assert any(
            "no longer needs a backup" in m and "after 1 failed" in m for m in env.messages("INFO")
        )

        # A later damage is a new episode: a new notice and a new first ERROR.
        env.path.write_bytes(b"broken again")
        result = store.appendBatch([startEvent(newId(), T0 + timedelta(minutes=2))])
        assert result.notice == StartupNotice("activity_log.json", None, BACKUP_FAILED_TEXT)
        errors = [m for m in env.messages("ERROR") if "could not be backed up" in m]
        assert len(errors) == 2
        assert all("attempt 1" in m for m in errors)

    def test_s3_episode_ends_when_the_file_is_deleted(
        self, env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = env.started(makeEvents(2))
        env.path.write_bytes(b"not json")
        monkeypatch.setattr(activity_log_store, "backupCorrupt", failingBackup())
        first = startEvent(newId(), T0)
        store.appendBatch([first])
        env.path.unlink()

        result = store.appendBatch([])

        assert result.written is True
        assert result.notice is not None  # the normal "missing" notice
        assert env.eventIds() == [first.eventId]
        assert any("no longer needs a backup" in m for m in env.messages("INFO"))

    def test_s10b_episode_ends_when_the_file_is_restored_to_the_cached_bytes(
        self, env: Env, monkeypatch: pytest.MonkeyPatch, loadsCalls: list[int]
    ) -> None:
        store = env.started(makeEvents(2))
        cached = store.lastWrittenBytes
        assert cached is not None
        env.path.write_bytes(b"not json")
        monkeypatch.setattr(activity_log_store, "backupCorrupt", failingBackup())
        first = startEvent(newId(), T0)
        assert store.appendBatch([first]).notice is not None

        env.path.write_bytes(cached)  # restored exactly: the fast path applies
        loadsCalls.clear()
        assert store.appendBatch([]) == AppendResult(True, None)

        assert loadsCalls == []
        assert env.eventIds()[2:] == [first.eventId]
        assert any(
            "no longer needs a backup" in m and "after 1 failed" in m for m in env.messages("INFO")
        )

        # New damage is a new episode: a fresh notice and a new attempt 1.
        env.path.write_bytes(b"broken again")
        result = store.appendBatch([startEvent(newId(), T0 + timedelta(minutes=1))])
        assert result.notice == StartupNotice("activity_log.json", None, BACKUP_FAILED_TEXT)
        errors = [m for m in env.messages("ERROR") if "could not be backed up" in m]
        assert len(errors) == 2
        assert all("attempt 1" in m for m in errors)


class TestRejectedAtStartup:
    """S4: a file that failed full validation and could not be backed up at startup is never
    adopted at runtime; every append retries the backup first."""

    def test_s4_invalid_file_is_not_appended_to(
        self, env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        start = startEvent(newId(), T0)
        invalid = env.writeEvents([start, replace(start, eventId=newId())])  # two starts
        calls: list[int] = []
        monkeypatch.setattr(activity_log_store, "backupCorrupt", failingBackup(calls))
        store = env.newStore()
        loaded = store.loadAtStartup()
        assert loaded.usable is False
        assert loaded.notice is not None
        assert loaded.notice.text == "activity_log.json is corrupt and could not be backed up"

        event = startEvent(newId(), T0 + timedelta(minutes=1))
        result = store.appendBatch([event])

        assert result == AppendResult(False, None)  # the startup notice already said so
        assert env.raw() == invalid
        assert store.pendingEvents == [event]
        assert len(calls) == 2  # startup, then the append retried it
        errors = [m for m in env.messages("ERROR") if "could not be backed up" in m]
        assert len(errors) == 1  # the append's failure is attempt 2: DEBUG only
        assert any("attempt 2" in m for m in env.messages("DEBUG"))

        monkeypatch.setattr(activity_log_store, "backupCorrupt", atomic.backupCorrupt)
        later = startEvent(newId(), T0 + timedelta(minutes=2))
        result = store.appendBatch([later])

        assert result.written is True
        assert result.notice is not None
        assert result.notice.backupName == BACKUP_NAME
        assert (env.paths.dataDir / BACKUP_NAME).read_bytes() == invalid
        assert env.eventIds() == [event.eventId, later.eventId]
        assert any("failed full validation at startup" in m for m in env.messages("WARNING"))
        env.assertValid()

        # S10a: the flag is cleared by the successful backup, so the good file is appended
        # to on the fast path and never backed up again.
        calls: list[int] = []
        original = json.loads
        monkeypatch.setattr(json, "loads", lambda *a, **k: calls.append(1) or original(*a, **k))
        third = startEvent(newId(), T0 + timedelta(minutes=3))
        assert store.appendBatch([third]) == AppendResult(True, None)
        assert calls == []  # fast path
        monkeypatch.setattr(json, "loads", original)
        assert env.corruptFiles() == [env.paths.dataDir / BACKUP_NAME]
        assert env.eventIds() == [event.eventId, later.eventId, third.eventId]
        env.assertValid()

    def test_s4_rejected_file_that_disappears_is_recreated(
        self, env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        start = startEvent(newId(), T0)
        env.writeEvents([start, replace(start, eventId=newId())])
        monkeypatch.setattr(activity_log_store, "backupCorrupt", failingBackup())
        store = env.newStore()
        store.loadAtStartup()
        env.path.unlink()
        event = startEvent(newId(), T0 + timedelta(minutes=1))

        assert store.appendBatch([event]).written is True
        assert env.eventIds() == [event.eventId]
        # The file is valid again: the next append uses the normal paths.
        store.appendBatch([startEvent(newId(), T0 + timedelta(minutes=2))])
        assert len(env.eventIds()) == 2
