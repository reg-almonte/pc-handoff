"""Integration tests for paths.py and atomic.py on a real temp directory.

Covers TASK-005 AC-1, AC-2 and AC-9.
"""

import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pc_handoff.persistence import atomic
from pc_handoff.persistence.atomic import (
    REPLACE_ATTEMPTS,
    PersistenceError,
    atomicWriteBytes,
    backupCorrupt,
    cleanupTempFiles,
    readBytes,
)
from pc_handoff.persistence.paths import DataPaths
from tests.fakes import FakeClock

JST = timezone(timedelta(hours=9))
BACKUP_TIME = datetime(2026, 10, 21, 9, 15, 2, tzinfo=JST)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tempFiles(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if ".tmp-" in p.name)


@pytest.fixture
def dataDir(tmp_path: Path) -> Path:
    directory = tmp_path / "data"
    directory.mkdir()
    return directory


@pytest.fixture
def noSleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    sleeps: list[float] = []
    monkeypatch.setattr(atomic.time, "sleep", sleeps.append)
    return sleeps


# --- DataPaths (AC-9) ----------------------------------------------------------------------


class TestDataPaths:
    def test_well_known_paths(self, dataDir: Path) -> None:
        paths = DataPaths(dataDir)
        root = dataDir.resolve()
        assert paths.dataDir == root
        assert paths.config == root / "config.json"
        assert paths.status == root / "status.json"
        assert paths.activityLog == root / "activity_log.json"
        assert paths.lock == root / "pc-handoff.lock"
        assert paths.logsDir == root / "logs"
        assert paths.appLog == root / "logs" / "app.log"

    def test_accepts_a_string(self, dataDir: Path) -> None:
        assert DataPaths(str(dataDir)).config == dataDir.resolve() / "config.json"

    def test_resolve_inside_allows_nested_names(self, dataDir: Path) -> None:
        paths = DataPaths(dataDir)
        assert paths.resolveInside("logs/app.log") == dataDir.resolve() / "logs" / "app.log"
        assert paths.resolveInside("a/../b.json") == dataDir.resolve() / "b.json"

    @pytest.mark.parametrize(
        "name", ["../x.json", "..", "logs/../../x.json", "", ".", "/etc/passwd", "C:/x.json"]
    )
    def test_resolve_inside_rejects_escapes_and_the_directory_itself(
        self, dataDir: Path, name: str
    ) -> None:
        """Security constraint: file access is limited to the data directory."""
        with pytest.raises(ValueError):
            DataPaths(dataDir).resolveInside(name)

    def test_rejects_absolute_path_inside_too(self, dataDir: Path) -> None:
        """Only relative names are accepted, even if an absolute one points inside."""
        with pytest.raises(ValueError):
            DataPaths(dataDir).resolveInside(str(dataDir.resolve() / "x.json"))


# --- atomicWriteBytes (AC-1) ---------------------------------------------------------------


class TestAtomicWrite:
    def test_creates_new_file(self, dataDir: Path) -> None:
        target = dataDir / "status.json"
        atomicWriteBytes(target, b"B")
        assert target.read_bytes() == b"B"
        assert tempFiles(dataDir) == []

    def test_replaces_existing_file(self, dataDir: Path) -> None:
        target = dataDir / "status.json"
        target.write_bytes(b"A")
        atomicWriteBytes(target, b"B" * 10_000)
        assert target.read_bytes() == b"B" * 10_000
        assert tempFiles(dataDir) == []

    def test_temp_file_is_in_the_same_directory_with_the_adr_003_name(
        self, dataDir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[str, str]] = []
        realReplace = os.replace

        def spyReplace(src: str, dst: str) -> None:
            seen.append((str(src), str(dst)))
            realReplace(src, dst)

        monkeypatch.setattr(atomic.os, "replace", spyReplace)
        target = dataDir / "status.json"
        atomicWriteBytes(target, b"B")
        ((src, dst),) = seen
        assert Path(dst) == target
        assert Path(src).parent == dataDir
        prefix = f"status.json.tmp-{os.getpid()}-"
        name = Path(src).name
        assert name.startswith(prefix)
        suffix = name[len(prefix) :]
        assert len(suffix) == 8 and int(suffix, 16) >= 0

    def test_data_is_flushed_and_fsynced_before_replace(
        self, dataDir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        realFsync, realReplace = os.fsync, os.replace
        monkeypatch.setattr(atomic.os, "fsync", lambda fd: (calls.append("fsync"), realFsync(fd)))
        monkeypatch.setattr(
            atomic.os, "replace", lambda s, d: (calls.append("replace"), realReplace(s, d))
        )
        atomicWriteBytes(dataDir / "status.json", b"B")
        assert calls == ["fsync", "replace"]

    def test_replace_failing_every_time_keeps_target_and_removes_temp(
        self, dataDir: Path, monkeypatch: pytest.MonkeyPatch, noSleep: list[float]
    ) -> None:
        """AC-1: a crash-equivalent failure at os.replace leaves content A, and no temp file."""
        target = dataDir / "status.json"
        target.write_bytes(b"A")
        before = sha256(target)
        calls: list[str] = []

        def failingReplace(src: str, dst: str) -> None:
            calls.append(src)
            assert Path(src).read_bytes() == b"B"  # the temp file was fully written first
            raise OSError("disk on fire")

        monkeypatch.setattr(atomic.os, "replace", failingReplace)
        with pytest.raises(PersistenceError) as excInfo:
            atomicWriteBytes(target, b"B")
        assert sha256(target) == before
        assert tempFiles(dataDir) == []
        assert isinstance(excInfo.value.__cause__, OSError)
        assert len(calls) == 1  # a non-permission OSError is not retried
        assert noSleep == []

    def test_permission_error_on_every_attempt_retries_then_fails(
        self, dataDir: Path, monkeypatch: pytest.MonkeyPatch, noSleep: list[float]
    ) -> None:
        target = dataDir / "status.json"
        target.write_bytes(b"A")
        calls: list[str] = []

        def lockedReplace(src: str, dst: str) -> None:
            calls.append(src)
            raise PermissionError(13, "locked by antivirus")

        monkeypatch.setattr(atomic.os, "replace", lockedReplace)
        with pytest.raises(PersistenceError):
            atomicWriteBytes(target, b"B")
        assert target.read_bytes() == b"A"
        assert tempFiles(dataDir) == []
        assert len(calls) == REPLACE_ATTEMPTS == 6  # the first try plus 5 retries
        assert noSleep == [0.02] * 5

    def test_permission_error_twice_then_success(
        self, dataDir: Path, monkeypatch: pytest.MonkeyPatch, noSleep: list[float]
    ) -> None:
        """AC-1: a briefly locked target (antivirus, indexer) is retried with a 20 ms sleep."""
        target = dataDir / "status.json"
        target.write_bytes(b"A")
        realReplace = os.replace
        failures = [PermissionError(13, "locked"), PermissionError(13, "locked")]

        def flakyReplace(src: str, dst: str) -> None:
            if failures:
                raise failures.pop(0)
            realReplace(src, dst)

        monkeypatch.setattr(atomic.os, "replace", flakyReplace)
        atomicWriteBytes(target, b"B")
        assert target.read_bytes() == b"B"
        assert tempFiles(dataDir) == []
        assert noSleep == [0.02, 0.02]

    def test_write_failure_removes_temp_and_raises(
        self, dataDir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failure while writing the temp file (e.g. disk full) never touches the target."""
        target = dataDir / "status.json"
        target.write_bytes(b"A")

        def failingFsync(fd: int) -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(atomic.os, "fsync", failingFsync)
        with pytest.raises(PersistenceError):
            atomicWriteBytes(target, b"B")
        assert target.read_bytes() == b"A"
        assert tempFiles(dataDir) == []

    def test_missing_directory_raises_persistence_error(self, tmp_path: Path) -> None:
        with pytest.raises(PersistenceError):
            atomicWriteBytes(tmp_path / "absent" / "status.json", b"B")

    def test_temp_cleanup_failure_is_logged_not_raised_over_the_original_error(
        self,
        dataDir: Path,
        monkeypatch: pytest.MonkeyPatch,
        noSleep: list[float],
        capturedAppLog,
    ) -> None:
        monkeypatch.setattr(atomic.os, "replace", lambda s, d: (_ for _ in ()).throw(OSError("x")))

        def failingUnlink(self: Path, missing_ok: bool = False) -> None:
            raise PermissionError(13, "still open")

        monkeypatch.setattr(atomic.Path, "unlink", failingUnlink)
        with pytest.raises(PersistenceError):
            atomicWriteBytes(dataDir / "status.json", b"B")
        warnings = [line for line in capturedAppLog() if line["level"] == "WARNING"]
        assert any("could not remove temp file" in line["message"] for line in warnings)

    def test_non_os_error_removes_temp_and_propagates_unchanged(
        self, dataDir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Critic TASK-005 S7: any exception after the temp file exists still removes it, and
        the original exception propagates unchanged (not wrapped in PersistenceError)."""
        target = dataDir / "status.json"
        target.write_bytes(b"A")
        error = RuntimeError("boom during write")

        def explodingFsync(fd: int) -> None:
            raise error

        monkeypatch.setattr(atomic.os, "fsync", explodingFsync)
        with pytest.raises(RuntimeError) as excInfo:
            atomicWriteBytes(target, b"B")
        assert excInfo.value is error
        assert target.read_bytes() == b"A"
        assert tempFiles(dataDir) == []

    def test_keyboard_interrupt_during_replace_removes_temp(
        self, dataDir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def interruptedReplace(src: str, dst: str) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(atomic.os, "replace", interruptedReplace)
        with pytest.raises(KeyboardInterrupt):
            atomicWriteBytes(dataDir / "status.json", b"B")
        assert tempFiles(dataDir) == []


class TestReadBytes:
    def test_missing_file_is_none(self, dataDir: Path) -> None:
        assert readBytes(dataDir / "config.json") is None

    def test_reads_exact_bytes(self, dataDir: Path) -> None:
        target = dataDir / "config.json"
        target.write_bytes(b"\xef\xbb\xbf{}\r\n")
        assert readBytes(target) == b"\xef\xbb\xbf{}\r\n"

    def test_directory_raises_instead_of_returning_none(self, dataDir: Path) -> None:
        """Critic TASK-005 S1: only a missing file is None (PermissionError on Windows,
        IsADirectoryError elsewhere)."""
        (dataDir / "config.json").mkdir()
        with pytest.raises(OSError) as excInfo:
            readBytes(dataDir / "config.json")
        assert not isinstance(excInfo.value, FileNotFoundError)

    @pytest.mark.parametrize(
        "error",
        [PermissionError(13, "access denied"), IsADirectoryError(21, "is a directory")],
        ids=["permission", "is-a-directory"],
    )
    def test_other_os_errors_propagate(
        self, dataDir: Path, monkeypatch: pytest.MonkeyPatch, error: OSError
    ) -> None:
        target = dataDir / "config.json"
        target.write_bytes(b"{}")

        def failingRead(self: Path) -> bytes:
            raise error

        monkeypatch.setattr(atomic.Path, "read_bytes", failingRead)
        with pytest.raises(type(error)) as excInfo:
            readBytes(target)
        assert excInfo.value is error


# --- backupCorrupt (AC-2) ------------------------------------------------------------------


class TestBackupCorrupt:
    def test_two_backups_in_the_same_second(self, dataDir: Path, capturedAppLog) -> None:
        clock = FakeClock(BACKUP_TIME)
        target = dataDir / "activity_log.json"
        first, second = b"\x00\xffnot json{{", b'{"schemaVersion": 7}'

        target.write_bytes(first)
        backup1 = backupCorrupt(target, clock)
        target.write_bytes(second)
        backup2 = backupCorrupt(target, clock)

        assert backup1 == dataDir / "activity_log.json.corrupt-20261021T091502"
        assert backup2 == dataDir / "activity_log.json.corrupt-20261021T091502-2"
        assert backup1.read_bytes() == first
        assert backup2.read_bytes() == second
        assert not target.exists()
        messages = [line["message"] for line in capturedAppLog()]
        assert any("activity_log.json.corrupt-20261021T091502-2" in m for m in messages)

    def test_third_backup_gets_suffix_3(self, dataDir: Path) -> None:
        clock = FakeClock(BACKUP_TIME)
        target = dataDir / "config.json"
        names = []
        for content in (b"1", b"2", b"3"):
            target.write_bytes(content)
            names.append(backupCorrupt(target, clock).name)
        assert names == [
            "config.json.corrupt-20261021T091502",
            "config.json.corrupt-20261021T091502-2",
            "config.json.corrupt-20261021T091502-3",
        ]

    def test_timestamp_is_the_clock_local_time(self, dataDir: Path) -> None:
        """The name uses the clock's own local wall time, not UTC."""
        clock = FakeClock(datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=-5))))
        target = dataDir / "status.json"
        target.write_bytes(b"x")
        assert backupCorrupt(target, clock).name == "status.json.corrupt-20260102T030405"

    def test_missing_file_raises(self, dataDir: Path) -> None:
        with pytest.raises(FileNotFoundError):
            backupCorrupt(dataDir / "status.json", FakeClock(BACKUP_TIME))


# --- cleanupTempFiles (AC-9) ---------------------------------------------------------------


class TestCleanupTempFiles:
    def test_removes_only_the_application_temp_files(self, dataDir: Path, capturedAppLog) -> None:
        keep = [
            "notes.txt",
            "status.json.corrupt-20261021T091502",
            "status.json",
            "other.json.tmp-1-aaaaaaaa",
            "xstatus.json.tmp-1-aaaaaaaa",
        ]
        remove = [
            "status.json.tmp-123-abcd1234",
            "activity_log.json.tmp-9-ffff0000",
            "config.json.tmp-77-01234567",
        ]
        for name in keep + remove:
            (dataDir / name).write_bytes(b"x")
        # Critic TASK-005 S4: a matching directory, and a matching file in a subdirectory,
        # are never touched (top level, regular files only).
        (dataDir / "status.json.tmp-5-dddddddd").mkdir()
        (dataDir / "logs").mkdir()
        nested = dataDir / "logs" / "status.json.tmp-1-aaaaaaaa"
        nested.write_bytes(b"x")

        removed = cleanupTempFiles(dataDir)

        assert sorted(p.name for p in removed) == sorted(remove)
        assert sorted(p.name for p in dataDir.iterdir()) == sorted(
            keep + ["status.json.tmp-5-dddddddd", "logs"]
        )
        assert (dataDir / "status.json.tmp-5-dddddddd").is_dir()
        assert nested.read_bytes() == b"x"
        lines = capturedAppLog()
        messages = [line["message"] for line in lines]
        for name in remove:
            assert any(name in m for m in messages)
        assert not [line for line in lines if line["level"] != "INFO"]
        assert not [m for m in messages if "dddddddd" in m or "logs" in m]

    def test_spec_example(self, dataDir: Path) -> None:
        """AC-9 Given: only the two .tmp- files are deleted."""
        names = [
            "status.json.tmp-123-abcd1234",
            "activity_log.json.tmp-9-ffff0000",
            "notes.txt",
            "status.json.corrupt-20261021T091502",
        ]
        for name in names:
            (dataDir / name).write_bytes(b"x")
        cleanupTempFiles(dataDir)
        assert sorted(p.name for p in dataDir.iterdir()) == [
            "notes.txt",
            "status.json.corrupt-20261021T091502",
        ]

    def test_missing_directory_is_a_no_op(self, tmp_path: Path) -> None:
        assert cleanupTempFiles(tmp_path / "absent") == []

    def test_undeletable_temp_file_is_logged_and_skipped(
        self, dataDir: Path, monkeypatch: pytest.MonkeyPatch, capturedAppLog
    ) -> None:
        (dataDir / "status.json.tmp-1-aaaaaaaa").write_bytes(b"x")
        (dataDir / "config.json.tmp-2-bbbbbbbb").write_bytes(b"x")
        realUnlink = Path.unlink

        def selectiveUnlink(self: Path, missing_ok: bool = False) -> None:
            if self.name.startswith("status"):
                raise PermissionError(13, "in use")
            realUnlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(atomic.Path, "unlink", selectiveUnlink)
        removed = cleanupTempFiles(dataDir)
        assert [p.name for p in removed] == ["config.json.tmp-2-bbbbbbbb"]
        assert any(
            line["level"] == "WARNING" and "status.json.tmp-1-aaaaaaaa" in line["message"]
            for line in capturedAppLog()
        )

    def test_uses_given_logger(self, dataDir: Path, capturedAppLog) -> None:
        (dataDir / "status.json.tmp-1-aaaaaaaa").write_bytes(b"x")
        cleanupTempFiles(dataDir, logging.getLogger("pc_handoff.custom"))
        assert any("status.json.tmp-1-aaaaaaaa" in line["message"] for line in capturedAppLog())
