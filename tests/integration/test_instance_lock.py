"""Integration tests for the single-instance lock (TASK-007, ADR-004).

Multi-process tests use real child processes: ``lock_holder_script.py`` holds the lock until it
is killed, and a ``python -c`` child runs ``runSecondInstanceGuard`` as a real second instance.
Every child is killed and reaped by the ``processes`` fixture, whatever the test outcome.
"""

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

import pc_handoff
from pc_handoff.persistence import instance_lock
from pc_handoff.persistence.atomic import cleanupTempFiles, isManagedTempFile
from pc_handoff.persistence.instance_lock import (
    EXIT_ALREADY_RUNNING,
    EXIT_LOCK_UNAVAILABLE,
    InstanceLock,
    LockHeldError,
    LockUnavailableError,
    reportSecondInstance,
    runSecondInstanceGuard,
)
from pc_handoff.persistence.paths import LOCK_FILE_NAME
from tests.fakes import FakeClock

HOLDER_SCRIPT = Path(__file__).with_name("lock_holder_script.py")
CHILD_TIMEOUT_SECONDS = 30.0
SRC_DIR = Path(pc_handoff.__file__).resolve().parents[1]
# A venv python.exe on Windows is a redirector that starts the real interpreter as a child,
# so Popen.pid / Popen.kill() would hit the redirector and leave the lock holder running.
# Children therefore run the base interpreter directly, with the package on PYTHONPATH.
CHILD_PYTHON = getattr(sys, "_base_executable", None) or sys.executable

GUARD_CODE = """
import sys, time
from pc_handoff.clock import SystemClock
from pc_handoff.persistence.instance_lock import runSecondInstanceGuard
start = time.monotonic()
code = runSecondInstanceGuard(sys.argv[1], SystemClock())
print(f"ELAPSED {time.monotonic() - start:.3f}", flush=True)
sys.exit(code)
"""

CONFIG_BYTES = (
    b'{"schemaVersion": 1, "dashboardRefreshIntervalSeconds": 10, '
    b'"sessionPollIntervalSeconds": 5, "dashboardPort": 8501}\n'
)
STATUS_BYTES = (
    b'{"schemaVersion": 1, "lastSuccessfulPollAt": "2026-10-21T09:00:00+09:00", "sessions": []}\n'
)
ACTIVITY_BYTES = b'{"schemaVersion": 1, "events": []}\n'


# --------------------------------------------------------------------------------------------
# helpers and fixtures
# --------------------------------------------------------------------------------------------


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def readDiagnostic(lockPath: Path) -> dict:
    """Read the diagnostic JSON after byte 0 (byte 0 may be locked by another handle)."""
    with open(lockPath, "rb") as handle:
        handle.seek(1)
        return json.loads(handle.read().decode("utf-8"))


def readTail(lockPath: Path) -> bytes:
    with open(lockPath, "rb") as handle:
        handle.seek(1)
        return handle.read()


def writeDataFiles(dataDir: Path) -> None:
    dataDir.mkdir(parents=True, exist_ok=True)
    (dataDir / "config.json").write_bytes(CONFIG_BYTES)
    (dataDir / "status.json").write_bytes(STATUS_BYTES)
    (dataDir / "activity_log.json").write_bytes(ACTIVITY_BYTES)


def snapshot(dataDir: Path) -> dict[str, tuple[str, int]]:
    """sha256 and mtime of every file under ``dataDir`` except the lock file.

    The lock file is not a data file, and on Windows its byte 0 cannot be read while another
    handle holds the lock; its content after byte 0 is compared separately.
    """
    result: dict[str, tuple[str, int]] = {}
    for path in sorted(dataDir.rglob("*")):
        if path.is_file() and path.name != LOCK_FILE_NAME:
            result[path.relative_to(dataDir).as_posix()] = (sha256(path), path.stat().st_mtime_ns)
    return result


def childEnv() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "__PYVENV_LAUNCHER__"}
    env["PYTHONPATH"] = str(SRC_DIR)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def spawnHolder(dataDir: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [CHILD_PYTHON, str(HOLDER_SCRIPT), str(dataDir)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=childEnv(),
    )


def readLineWithTimeout(proc: subprocess.Popen, timeout: float) -> str:
    box: list[str] = []
    reader = threading.Thread(target=lambda: box.append(proc.stdout.readline()), daemon=True)
    reader.start()
    reader.join(timeout)
    if reader.is_alive() or not box:
        proc.kill()
        pytest.fail(f"child did not print a line within {timeout} s")
    return box[0]


@pytest.fixture
def processes() -> Iterator[list[subprocess.Popen]]:
    """Children started by a test; each is killed (if still alive) and reaped afterwards."""
    started: list[subprocess.Popen] = []
    try:
        yield started
    finally:
        for proc in started:
            if proc.poll() is None:
                proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):  # only if the OS is stuck
                proc.wait(timeout=10)
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()


@pytest.fixture
def startHolder(processes: list[subprocess.Popen]) -> Callable[[Path], subprocess.Popen]:
    """Start ``lock_holder_script.py`` on a data directory and wait until it holds the lock."""

    def start(dataDir: Path) -> subprocess.Popen:
        proc = spawnHolder(dataDir)
        processes.append(proc)
        line = readLineWithTimeout(proc, CHILD_TIMEOUT_SECONDS)
        # The holder prints its own pid: Popen.pid must be the process that holds the lock.
        assert line.split() == ["READY", str(proc.pid)], f"holder said {line!r}"
        return proc

    return start


def runGuardProcess(dataDir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [CHILD_PYTHON, "-c", GUARD_CODE, str(dataDir)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=childEnv(),
        timeout=CHILD_TIMEOUT_SECONDS,
    )


def killAndReap(proc: subprocess.Popen) -> None:
    proc.kill()  # TerminateProcess on Windows, SIGKILL elsewhere
    proc.wait(timeout=CHILD_TIMEOUT_SECONDS)


def parseJsonLines(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# --------------------------------------------------------------------------------------------
# acquire / diagnostic content
# --------------------------------------------------------------------------------------------


class TestAcquire:
    def test_creates_data_dir_and_lock_file_with_diagnostic(
        self, tmpDataDir: Path, fakeClock: FakeClock
    ) -> None:
        lock = InstanceLock(tmpDataDir, fakeClock)
        lock.acquire()
        try:
            assert lock.isHeld
            assert lock.lockPath == (tmpDataDir / LOCK_FILE_NAME).resolve()
            assert readDiagnostic(lock.lockPath) == {
                "pid": os.getpid(),
                "startedAt": "2026-10-21T09:00:00+09:00",
            }
            assert sorted(p.name for p in tmpDataDir.iterdir()) == [LOCK_FILE_NAME]
        finally:
            lock.release()
        assert not lock.isHeld

    def test_existing_lock_file_with_any_content_is_reused_and_byte0_kept(
        self, tmpDataDir: Path, fakeClock: FakeClock
    ) -> None:
        tmpDataDir.mkdir(parents=True)
        lockPath = tmpDataDir / LOCK_FILE_NAME
        lockPath.write_bytes(b"\xff" + b"\x00garbage not json\xfe" * 500)
        with InstanceLock(tmpDataDir, fakeClock):
            assert readDiagnostic(lockPath)["pid"] == os.getpid()
        content = lockPath.read_bytes()
        assert content[:1] == b"\xff"
        assert json.loads(content[1:].decode("utf-8"))["pid"] == os.getpid()

    def test_existing_empty_lock_file_is_reused(
        self, tmpDataDir: Path, fakeClock: FakeClock
    ) -> None:
        tmpDataDir.mkdir(parents=True)
        (tmpDataDir / LOCK_FILE_NAME).write_bytes(b"")
        with InstanceLock(tmpDataDir, fakeClock) as lock:
            assert readDiagnostic(lock.lockPath)["pid"] == os.getpid()

    def test_acquire_twice_on_same_object_raises(
        self, tmpDataDir: Path, fakeClock: FakeClock
    ) -> None:
        with InstanceLock(tmpDataDir, fakeClock) as lock:
            with pytest.raises(RuntimeError):
                lock.acquire()
            assert lock.isHeld

    def test_second_handle_in_same_process_is_refused_and_closes_its_fd(
        self, tmpDataDir: Path, fakeClock: FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sleeps: list[float] = []
        realOpen = os.open
        openedFds: list[int] = []

        def recordingOpen(*args, **kwargs) -> int:
            fd = realOpen(*args, **kwargs)
            openedFds.append(fd)
            return fd

        monkeypatch.setattr(instance_lock.os, "open", recordingOpen)
        with InstanceLock(tmpDataDir, fakeClock) as first:
            before = readTail(first.lockPath)
            second = InstanceLock(tmpDataDir, fakeClock, sleep=sleeps.append)
            with pytest.raises(LockHeldError) as info:
                second.acquire()
            assert str(first.lockPath) in str(info.value)
            assert not second.isHeld
            assert readTail(first.lockPath) == before  # the loser writes no diagnostic
            loserFd = openedFds[1]
            with pytest.raises(OSError):
                os.fstat(loserFd)  # the losing attempt closed its handle
        assert sleeps == [0.2, 0.2]  # 3 attempts, a delay only between attempts
        with InstanceLock(tmpDataDir, fakeClock):  # nothing leaked by the failed attempt
            pass

    def test_retries_from_byte0_until_lock_succeeds(
        self, tmpDataDir: Path, fakeClock: FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        realLock = instance_lock.lockByte
        positions: list[int] = []

        def flakyLock(fd: int) -> None:
            positions.append(os.lseek(fd, 0, os.SEEK_CUR))
            if len(positions) < 3:
                os.lseek(fd, 5, os.SEEK_SET)  # move away; the next attempt must seek back
                raise OSError("busy")
            realLock(fd)

        monkeypatch.setattr(instance_lock, "lockByte", flakyLock)
        sleeps: list[float] = []
        lock = InstanceLock(tmpDataDir, fakeClock, retryDelaySeconds=0.05, sleep=sleeps.append)
        lock.acquire()
        lock.release()
        assert positions == [0, 0, 0]
        assert sleeps == [0.05, 0.05]

    def test_custom_attempts(
        self, tmpDataDir: Path, fakeClock: FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[int] = []

        def alwaysBusy(fd: int) -> None:
            calls.append(fd)
            raise BlockingIOError("busy")

        monkeypatch.setattr(instance_lock, "lockByte", alwaysBusy)
        with pytest.raises(LockHeldError):
            InstanceLock(tmpDataDir, fakeClock, attempts=1, sleep=lambda s: None).acquire()
        assert len(calls) == 1

    def test_invalid_attempts_rejected(self, tmpDataDir: Path, fakeClock: FakeClock) -> None:
        with pytest.raises(ValueError):
            InstanceLock(tmpDataDir, fakeClock, attempts=0)

    def test_diagnostic_write_failure_keeps_the_lock(
        self, tmpDataDir: Path, fakeClock: FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def failingTruncate(fd: int, length: int) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(instance_lock.os, "ftruncate", failingTruncate)
        with InstanceLock(tmpDataDir, fakeClock) as lock:
            assert lock.isHeld
            with pytest.raises(LockHeldError):
                InstanceLock(tmpDataDir, fakeClock, sleep=lambda s: None).acquire()


# --------------------------------------------------------------------------------------------
# AC-3: release
# --------------------------------------------------------------------------------------------


class TestRelease:
    def test_ac03_release_twice_then_reacquire_and_file_kept(
        self, tmpDataDir: Path, fakeClock: FakeClock
    ) -> None:
        lock = InstanceLock(tmpDataDir, fakeClock)
        lock.acquire()
        lock.release()
        lock.release()  # idempotent: no exception
        assert (tmpDataDir / LOCK_FILE_NAME).exists()
        other = InstanceLock(tmpDataDir, fakeClock, sleep=lambda s: None)
        other.acquire()
        other.release()
        assert (tmpDataDir / LOCK_FILE_NAME).exists()

    def test_same_object_can_acquire_again_after_release(
        self, tmpDataDir: Path, fakeClock: FakeClock
    ) -> None:
        lock = InstanceLock(tmpDataDir, fakeClock)
        lock.acquire()
        lock.release()
        lock.acquire()
        assert lock.isHeld
        lock.release()

    def test_release_without_acquire_is_a_no_op(
        self, tmpDataDir: Path, fakeClock: FakeClock
    ) -> None:
        InstanceLock(tmpDataDir, fakeClock).release()
        assert not tmpDataDir.exists()

    def test_release_seeks_to_byte0_before_unlocking(
        self, tmpDataDir: Path, fakeClock: FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        realUnlock = instance_lock.unlockByte
        positions: list[int] = []

        def recordingUnlock(fd: int) -> None:
            positions.append(os.lseek(fd, 0, os.SEEK_CUR))
            realUnlock(fd)

        monkeypatch.setattr(instance_lock, "unlockByte", recordingUnlock)
        lock = InstanceLock(tmpDataDir, fakeClock)
        lock.acquire()
        lock.release()
        assert positions == [0]

    def test_release_closes_even_if_unlock_fails(
        self, tmpDataDir: Path, fakeClock: FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def failingUnlock(fd: int) -> None:
            raise OSError("unlock failed")

        lock = InstanceLock(tmpDataDir, fakeClock)
        lock.acquire()
        monkeypatch.setattr(instance_lock, "unlockByte", failingUnlock)
        lock.release()  # no exception; closing the handle drops the lock
        assert not lock.isHeld
        monkeypatch.undo()
        with InstanceLock(tmpDataDir, fakeClock, sleep=lambda s: None):
            pass

    def test_context_manager_releases_on_exception(
        self, tmpDataDir: Path, fakeClock: FakeClock
    ) -> None:
        with pytest.raises(KeyError), InstanceLock(tmpDataDir, fakeClock) as lock:
            assert lock.isHeld
            raise KeyError("boom")
        assert not lock.isHeld
        with InstanceLock(tmpDataDir, fakeClock, sleep=lambda s: None):
            pass


# --------------------------------------------------------------------------------------------
# AC-4: unopenable lock path
# --------------------------------------------------------------------------------------------


class TestUnavailable:
    def test_lock_path_is_a_directory_raises_unavailable(
        self, tmpDataDir: Path, fakeClock: FakeClock
    ) -> None:
        (tmpDataDir / LOCK_FILE_NAME).mkdir(parents=True)
        with pytest.raises(LockUnavailableError) as info:
            InstanceLock(tmpDataDir, fakeClock).acquire()
        assert LOCK_FILE_NAME in str(info.value)

    def test_data_dir_is_a_file_raises_unavailable(
        self, tmp_path: Path, fakeClock: FakeClock
    ) -> None:
        notADir = tmp_path / "data"
        notADir.write_bytes(b"x")
        with pytest.raises(LockUnavailableError):
            InstanceLock(notADir, fakeClock).acquire()

    def test_ac04_guard_returns_4_logs_and_creates_no_data_file(
        self, tmpDataDir: Path, fakeClock: FakeClock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmpDataDir / LOCK_FILE_NAME).mkdir(parents=True)
        assert runSecondInstanceGuard(tmpDataDir, fakeClock) == EXIT_LOCK_UNAVAILABLE
        errLines = parseJsonLines(capsys.readouterr().err)
        assert len(errLines) == 1
        assert errLines[0]["level"] == "ERROR"
        assert LOCK_FILE_NAME in errLines[0]["message"]
        assert errLines[0]["detail"]["exitCode"] == EXIT_LOCK_UNAVAILABLE
        logLines = parseJsonLines((tmpDataDir / "logs" / "app.log").read_text(encoding="utf-8"))
        assert logLines == errLines
        files = sorted(p.relative_to(tmpDataDir).as_posix() for p in tmpDataDir.rglob("*"))
        assert files == ["logs", "logs/app.log", LOCK_FILE_NAME]


# --------------------------------------------------------------------------------------------
# runSecondInstanceGuard / reportSecondInstance in-process
# --------------------------------------------------------------------------------------------


class TestGuardInProcess:
    def test_free_lock_returns_0_releases_and_reports_nothing(
        self, tmpDataDir: Path, fakeClock: FakeClock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert runSecondInstanceGuard(tmpDataDir, fakeClock) == 0
        assert capsys.readouterr().err == ""
        assert sorted(p.name for p in tmpDataDir.iterdir()) == [LOCK_FILE_NAME]
        with InstanceLock(tmpDataDir, fakeClock, sleep=lambda s: None):  # released
            pass

    def test_held_lock_returns_3_within_2s_and_reports_once(
        self, tmpDataDir: Path, fakeClock: FakeClock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        writeDataFiles(tmpDataDir)
        with InstanceLock(tmpDataDir, fakeClock) as holder:
            before = snapshot(tmpDataDir)
            started = time.monotonic()
            code = runSecondInstanceGuard(tmpDataDir, fakeClock)
            elapsed = time.monotonic() - started
            after = snapshot(tmpDataDir)
        assert code == EXIT_ALREADY_RUNNING
        assert elapsed < 2.0
        errLines = parseJsonLines(capsys.readouterr().err)
        assert len(errLines) == 1
        line = errLines[0]
        assert line["level"] == "ERROR"
        assert line["timestamp"] == "2026-10-21T09:00:00+09:00"
        assert str(holder.lockPath) in line["message"]
        assert line["detail"] == {
            "lockPath": str(holder.lockPath),
            "exitCode": EXIT_ALREADY_RUNNING,
        }
        assert set(after) == set(before) | {"logs/app.log"}
        assert {k: after[k] for k in before} == before
        logText = (tmpDataDir / "logs" / "app.log").read_text(encoding="utf-8")
        assert parseJsonLines(logText) == errLines

    def test_report_appends_to_existing_app_log(
        self, tmpDataDir: Path, fakeClock: FakeClock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        logPath = tmpDataDir / "logs" / "app.log"
        logPath.parent.mkdir(parents=True)
        logPath.write_text('{"message": "earlier"}\n', encoding="utf-8")
        reportSecondInstance(tmpDataDir, LockHeldError("held"), fakeClock)
        lines = logPath.read_text(encoding="utf-8").splitlines()
        assert lines[0] == '{"message": "earlier"}'
        assert json.loads(lines[1])["message"] == "held"
        assert json.loads(lines[1])["detail"]["exitCode"] == EXIT_ALREADY_RUNNING

    def test_report_still_writes_stderr_when_app_log_unwritable(
        self, tmpDataDir: Path, fakeClock: FakeClock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        tmpDataDir.mkdir(parents=True)
        (tmpDataDir / "logs").write_bytes(b"not a directory")
        reportSecondInstance(tmpDataDir, LockUnavailableError("cannot open"), fakeClock)
        errLines = parseJsonLines(capsys.readouterr().err)
        assert [line["message"] for line in errLines] == ["cannot open"]
        assert (tmpDataDir / "logs").read_bytes() == b"not a directory"

    def test_report_escapes_for_a_stderr_that_cannot_encode_the_path(
        self, tmp_path: Path, fakeClock: FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        raw = io.BytesIO()
        asciiStderr = io.TextIOWrapper(raw, encoding="ascii", errors="strict")
        monkeypatch.setattr(sys, "stderr", asciiStderr)
        dataDir = tmp_path / "データ"
        reportSecondInstance(dataDir, LockHeldError(f"holds {dataDir}"), fakeClock)
        written = raw.getvalue().decode("ascii")
        assert len(written.splitlines()) == 1
        logText = (dataDir / "logs" / "app.log").read_text(encoding="utf-8")
        assert json.loads(written) == json.loads(logText)

    def test_report_without_stderr_still_appends_app_log(
        self, tmpDataDir: Path, fakeClock: FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "stderr", None)
        reportSecondInstance(tmpDataDir, LockHeldError("held"), fakeClock)
        logText = (tmpDataDir / "logs" / "app.log").read_text(encoding="utf-8")
        assert json.loads(logText)["message"] == "held"

    def test_report_keeps_non_ascii_paths_readable(
        self, tmp_path: Path, fakeClock: FakeClock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        dataDir = tmp_path / "データ"
        reportSecondInstance(dataDir, LockHeldError(f"holds {dataDir}"), fakeClock)
        logText = (dataDir / "logs" / "app.log").read_text(encoding="utf-8")
        assert "データ" in logText
        assert json.loads(logText)["message"] == f"holds {dataDir}"


# --------------------------------------------------------------------------------------------
# post-PASS fixes (Critic TASK-007 S1-S3)
# --------------------------------------------------------------------------------------------


class BrokenStderr(io.StringIO):
    """A stderr whose ``write`` or ``flush`` raises; ``mode="fallback"`` fails both writes."""

    def __init__(self, mode: str, error: BaseException) -> None:
        super().__init__()
        self.mode = mode
        self.error = error
        self.writes = 0

    def write(self, text: str) -> int:
        self.writes += 1
        if self.mode == "write":
            raise self.error
        if self.mode == "fallback":
            if self.writes == 1:
                raise UnicodeEncodeError("ascii", text, 0, 1, "cannot encode")
            raise self.error
        return super().write(text)

    def flush(self) -> None:
        if self.mode == "flush":
            raise self.error
        super().flush()


BROKEN_STDERR_CASES = [
    pytest.param("write", OSError(22, "Invalid argument"), id="write-oserror22"),
    pytest.param("write", ValueError("I/O operation on closed file"), id="write-closed"),
    pytest.param("flush", OSError(22, "Invalid argument"), id="flush-oserror22"),
    pytest.param("fallback", OSError(32, "Broken pipe"), id="ascii-fallback-oserror"),
]


class TestPostPassFixes:
    @pytest.mark.parametrize(("mode", "error"), BROKEN_STDERR_CASES)
    def test_s1_broken_stderr_still_returns_3_and_logs_once(
        self,
        tmpDataDir: Path,
        fakeClock: FakeClock,
        monkeypatch: pytest.MonkeyPatch,
        mode: str,
        error: BaseException,
    ) -> None:
        writeDataFiles(tmpDataDir)
        monkeypatch.setattr(sys, "stderr", BrokenStderr(mode, error))
        with InstanceLock(tmpDataDir, fakeClock) as holder:
            before = snapshot(tmpDataDir)
            code = runSecondInstanceGuard(tmpDataDir, fakeClock)
            after = snapshot(tmpDataDir)
        assert code == EXIT_ALREADY_RUNNING
        logLines = parseJsonLines((tmpDataDir / "logs" / "app.log").read_text(encoding="utf-8"))
        assert len(logLines) == 1
        assert str(holder.lockPath) in logLines[0]["message"]
        assert set(after) == set(before) | {"logs/app.log"}
        assert {k: after[k] for k in before} == before

    @pytest.mark.parametrize(("mode", "error"), BROKEN_STDERR_CASES)
    def test_s1_broken_stderr_still_returns_4_when_unavailable(
        self,
        tmpDataDir: Path,
        fakeClock: FakeClock,
        monkeypatch: pytest.MonkeyPatch,
        mode: str,
        error: BaseException,
    ) -> None:
        (tmpDataDir / LOCK_FILE_NAME).mkdir(parents=True)
        monkeypatch.setattr(sys, "stderr", BrokenStderr(mode, error))
        assert runSecondInstanceGuard(tmpDataDir, fakeClock) == EXIT_LOCK_UNAVAILABLE
        logLines = parseJsonLines((tmpDataDir / "logs" / "app.log").read_text(encoding="utf-8"))
        assert len(logLines) == 1
        assert logLines[0]["detail"]["exitCode"] == EXIT_LOCK_UNAVAILABLE
        files = sorted(p.relative_to(tmpDataDir).as_posix() for p in tmpDataDir.rglob("*"))
        assert files == ["logs", "logs/app.log", LOCK_FILE_NAME]

    def test_s1_app_log_is_written_before_stderr(
        self, tmpDataDir: Path, fakeClock: FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        appLog = tmpDataDir / "logs" / "app.log"
        seenInLog: list[bool] = []

        class ObservingStderr(io.StringIO):
            def write(self, text: str) -> int:
                seenInLog.append(appLog.exists() and text in appLog.read_text(encoding="utf-8"))
                return super().write(text)

        stderr = ObservingStderr()
        monkeypatch.setattr(sys, "stderr", stderr)
        reportSecondInstance(tmpDataDir, LockHeldError("held"), fakeClock)
        assert seenInLog == [True]  # exactly one stderr line, written after app.log has it
        assert parseJsonLines(stderr.getvalue()) == parseJsonLines(
            appLog.read_text(encoding="utf-8")
        )

    def test_s2_lock_file_is_opened_in_binary_mode(
        self, tmpDataDir: Path, fakeClock: FakeClock
    ) -> None:
        payload = b'{"pid": 1,\n "startedAt": "x"}\n'
        with InstanceLock(tmpDataDir, fakeClock) as lock:
            # Write through the lock's own handle, as writeDiagnostic does.
            os.lseek(lock.fd, 1, os.SEEK_SET)
            written = os.write(lock.fd, payload)
            os.ftruncate(lock.fd, 1 + written)
            assert readTail(lock.lockPath) == payload  # no \r\n translation
        assert written == len(payload)
        assert lock.lockPath.read_bytes()[1:] == payload

    def test_s3_seek_failure_is_a_failed_attempt_and_closes_the_fd(
        self, tmpDataDir: Path, fakeClock: FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        realOpen = os.open
        openedFds: list[int] = []
        seeks: list[int] = []

        def recordingOpen(*args, **kwargs) -> int:
            fd = realOpen(*args, **kwargs)
            openedFds.append(fd)
            return fd

        def failingSeek(fd: int, pos: int, how: int) -> int:
            seeks.append(fd)
            raise OSError(22, "Invalid argument")

        sleeps: list[float] = []
        with monkeypatch.context() as patch:
            patch.setattr(instance_lock.os, "open", recordingOpen)
            patch.setattr(instance_lock.os, "lseek", failingSeek)
            lock = InstanceLock(tmpDataDir, fakeClock, sleep=sleeps.append)
            with pytest.raises(LockHeldError):
                lock.acquire()
        assert not lock.isHeld
        assert len(seeks) == 3  # every attempt failed on its seek
        assert sleeps == [0.2, 0.2]
        with pytest.raises(OSError):
            os.fstat(openedFds[0])  # the handle was closed
        with InstanceLock(tmpDataDir, fakeClock, sleep=lambda s: None):  # nothing left locked
            pass


# --------------------------------------------------------------------------------------------
# the lock file is not a data file and survives temp cleanup
# --------------------------------------------------------------------------------------------


class TestLockFileIsNotADataFile:
    def test_cleanup_temp_files_never_deletes_the_lock_file(
        self, tmpDataDir: Path, fakeClock: FakeClock
    ) -> None:
        with InstanceLock(tmpDataDir, fakeClock) as lock:
            assert not isManagedTempFile(lock.lockPath)
            assert cleanupTempFiles(tmpDataDir) == []
            assert lock.lockPath.exists()
        assert cleanupTempFiles(tmpDataDir) == []
        assert (tmpDataDir / LOCK_FILE_NAME).exists()


# --------------------------------------------------------------------------------------------
# AC-1 and AC-2 with real processes
# --------------------------------------------------------------------------------------------


class TestMultiProcess:
    def test_ac01_lock_held_by_another_process_refuses_acquire(
        self, tmpDataDir: Path, fakeClock: FakeClock, startHolder
    ) -> None:
        holder = startHolder(tmpDataDir)
        assert readDiagnostic(tmpDataDir / LOCK_FILE_NAME)["pid"] == holder.pid
        with pytest.raises(LockHeldError):
            InstanceLock(tmpDataDir, fakeClock).acquire()
        assert holder.poll() is None

    def test_ac01_second_instance_process_exits_3_and_leaves_data_files_untouched(
        self, tmpDataDir: Path, startHolder
    ) -> None:
        writeDataFiles(tmpDataDir)
        (tmpDataDir / "status.json.tmp-leftover").write_bytes(b"partial")
        appLog = tmpDataDir / "logs" / "app.log"
        appLog.parent.mkdir()
        appLog.write_bytes(b'{"message": "from instance A"}\n')
        holder = startHolder(tmpDataDir)
        lockPath = tmpDataDir / LOCK_FILE_NAME
        before = snapshot(tmpDataDir)
        lockTailBefore = readTail(lockPath)
        appLogBefore = appLog.read_bytes()

        result = runGuardProcess(tmpDataDir)

        assert result.returncode == EXIT_ALREADY_RUNNING, result.stderr
        elapsed = float(result.stdout.split()[1])
        assert elapsed < 2.0
        errLines = parseJsonLines(result.stderr)
        assert len(errLines) == 1
        assert errLines[0]["level"] == "ERROR"
        assert str(lockPath.resolve()) in errLines[0]["message"]
        after = snapshot(tmpDataDir)
        assert set(after) == set(before)  # nothing created
        changed = sorted(name for name in before if after[name] != before[name])
        assert changed == ["logs/app.log"]  # every data file: same sha256 and mtime
        appLogAfter = appLog.read_bytes()
        assert appLogAfter.startswith(appLogBefore)
        assert parseJsonLines(appLogAfter[len(appLogBefore) :].decode("utf-8")) == errLines
        assert readTail(lockPath) == lockTailBefore  # B wrote no diagnostic
        assert holder.poll() is None  # A is unaffected

    def test_ac01_holder_script_as_second_instance_reports_held(
        self, tmpDataDir: Path, startHolder, processes: list[subprocess.Popen]
    ) -> None:
        startHolder(tmpDataDir)
        second = spawnHolder(tmpDataDir)
        processes.append(second)
        assert readLineWithTimeout(second, CHILD_TIMEOUT_SECONDS).strip() == "HELD"
        assert second.wait(timeout=CHILD_TIMEOUT_SECONDS) == EXIT_ALREADY_RUNNING

    def test_ac02_killed_holder_does_not_block_next_acquire(
        self, tmpDataDir: Path, fakeClock: FakeClock, startHolder
    ) -> None:
        holder = startHolder(tmpDataDir)
        lockPath = tmpDataDir / LOCK_FILE_NAME
        assert readDiagnostic(lockPath)["pid"] == holder.pid
        killAndReap(holder)
        assert lockPath.exists()  # the stale file is left behind

        started = time.monotonic()
        with InstanceLock(tmpDataDir, fakeClock):
            elapsed = time.monotonic() - started
            assert readDiagnostic(lockPath)["pid"] == os.getpid()
        assert elapsed < 1.0

    def test_ac02_killed_holder_does_not_block_a_new_process(
        self, tmpDataDir: Path, startHolder
    ) -> None:
        first = startHolder(tmpDataDir)
        killAndReap(first)
        second = startHolder(tmpDataDir)  # READY: the new process acquired the lock
        assert readDiagnostic(tmpDataDir / LOCK_FILE_NAME)["pid"] == second.pid
        killAndReap(second)
        assert runGuardProcess(tmpDataDir).returncode == 0


# --------------------------------------------------------------------------------------------
# Windows-specific: msvcrt mandatory byte-range lock
# --------------------------------------------------------------------------------------------


@pytest.mark.windows
@pytest.mark.skipif(sys.platform != "win32", reason="msvcrt.locking is Windows-only")
def test_windows_uses_msvcrt_and_byte0_is_locked_for_other_handles(
    tmpDataDir: Path, fakeClock: FakeClock
) -> None:
    assert instance_lock.LOCK_BACKEND == "msvcrt"
    with InstanceLock(tmpDataDir, fakeClock) as lock:
        with open(lock.lockPath, "rb") as handle, pytest.raises(PermissionError):
            handle.read(1)  # byte 0 is locked by the holder's handle
        assert readDiagnostic(lock.lockPath)["pid"] == os.getpid()  # bytes 1.. readable
    assert lock.lockPath.read_bytes()[1:]  # readable once released
