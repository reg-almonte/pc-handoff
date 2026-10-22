"""Single-instance lock on ``<dataDir>/pc-handoff.lock`` (ADR-004, TASK-007).

``InstanceLock`` takes a non-blocking exclusive OS byte-range lock on byte 0 of the lock file
(``msvcrt.locking`` on Windows, ``fcntl.flock`` elsewhere) and keeps the handle open for the
life of the process. The lock lives in the kernel, not in the file contents, so the OS drops it
when the process dies (crash, ``TerminateProcess``, SIGKILL, power loss): a leftover lock file
never blocks the next start, and the file is never deleted.

``runSecondInstanceGuard`` is the second-instance path: it reports one JSON error line to
stderr and ``<dataDir>/logs/app.log`` and returns ``EXIT_ALREADY_RUNNING`` (3), or
``EXIT_LOCK_UNAVAILABLE`` (4) when the lock file cannot be opened. It never opens
``config.json``, ``status.json`` or ``activity_log.json``.
"""

import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Any

from pc_handoff.app_logging import traceIdVar
from pc_handoff.clock import Clock, toIso
from pc_handoff.persistence.paths import DataPaths

EXIT_ALREADY_RUNNING = 3
EXIT_LOCK_UNAVAILABLE = 4

DEFAULT_ATTEMPTS = 3
DEFAULT_RETRY_DELAY_SECONDS = 0.2
LOCK_FILE_MODE = 0o644
DIAGNOSTIC_OFFSET = 1  # byte 0 is the locked byte; the diagnostic JSON starts after it
# O_BINARY (Windows only): without it the CRT opens in text mode and stores "\n" as "\r\n".
LOCK_OPEN_FLAGS = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)

if sys.platform == "win32":
    import msvcrt

    LOCK_BACKEND = "msvcrt"

    def lockByte(fd: int) -> None:
        """Lock 1 byte from the current position without waiting (raises ``OSError``)."""
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def unlockByte(fd: int) -> None:
        """Unlock 1 byte from the current position."""
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:  # pragma: no cover - exercised by the same tests when run off Windows
    import fcntl

    LOCK_BACKEND = "fcntl"

    def lockByte(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def unlockByte(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


class LockHeldError(Exception):
    """Another live process holds the instance lock for this data directory."""


class LockUnavailableError(Exception):
    """The instance lock file (or its data directory) cannot be created or opened."""


class InstanceLock:
    """Exclusive, process-lifetime lock on ``<dataDir>/pc-handoff.lock``.

    ``acquire()`` raises ``LockHeldError`` or ``LockUnavailableError``; ``release()`` is
    idempotent and never deletes the file. Usable as a context manager.
    """

    def __init__(
        self,
        dataDir: str | Path,
        clock: Clock,
        attempts: int = DEFAULT_ATTEMPTS,
        retryDelaySeconds: float = DEFAULT_RETRY_DELAY_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if attempts < 1:
            raise ValueError(f"attempts must be at least 1, got {attempts}")
        self.paths = DataPaths(dataDir)
        self.lockPath = self.paths.lock
        self.clock = clock
        self.attempts = attempts
        self.retryDelaySeconds = retryDelaySeconds
        self.sleep = sleep
        self.fd: int | None = None

    @property
    def isHeld(self) -> bool:
        return self.fd is not None

    def acquire(self) -> None:
        """Take the lock or raise. Creates the data directory and the lock file if missing."""
        if self.fd is not None:
            raise RuntimeError(f"instance lock already acquired: {self.lockPath}")
        try:
            self.paths.dataDir.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.lockPath, LOCK_OPEN_FLAGS, LOCK_FILE_MODE)
        except OSError as exc:
            raise LockUnavailableError(
                f"cannot open the PC Handoff instance lock {self.lockPath}: {exc}"
            ) from exc
        if not self.tryLock(fd):
            os.close(fd)
            raise LockHeldError(f"another PC Handoff instance holds {self.lockPath}")
        self.fd = fd
        self.writeDiagnostic(fd)

    def tryLock(self, fd: int) -> bool:
        for attempt in range(self.attempts):
            if attempt:
                self.sleep(self.retryDelaySeconds)
            try:
                # msvcrt.locking locks from the current position, so always start at byte 0.
                os.lseek(fd, 0, os.SEEK_SET)
                lockByte(fd)
            except OSError:
                continue  # a failed seek counts as a failed attempt; acquire() closes fd
            return True
        return False

    def writeDiagnostic(self, fd: int) -> None:
        """Best effort: ``{"pid", "startedAt"}`` after byte 0, replacing older content."""
        content = json.dumps({"pid": os.getpid(), "startedAt": toIso(self.clock.now())})
        data = content.encode("utf-8")
        try:
            os.lseek(fd, DIAGNOSTIC_OFFSET, os.SEEK_SET)
            os.write(fd, data)
            os.ftruncate(fd, DIAGNOSTIC_OFFSET + len(data))
        except OSError:
            pass  # diagnostic only; the lock is held either way

    def release(self) -> None:
        """Unlock and close the handle. Safe to call more than once; never deletes the file."""
        fd = self.fd
        if fd is None:
            return
        self.fd = None
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            unlockByte(fd)
        except OSError:
            pass  # closing the handle releases the lock anyway
        finally:
            os.close(fd)

    def __enter__(self) -> "InstanceLock":
        self.acquire()
        return self

    def __exit__(
        self,
        excType: type[BaseException] | None,
        excValue: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


def exitCodeFor(error: Exception) -> int:
    return (
        EXIT_LOCK_UNAVAILABLE if isinstance(error, LockUnavailableError) else EXIT_ALREADY_RUNNING
    )


def reportSecondInstance(dataDir: str | Path, error: Exception, clock: Clock) -> None:
    """Write one JSON error line to stderr and append the same line to ``logs/app.log``.

    Bypasses the main logging configuration (the lock is taken before logging is set up) and
    writes nothing else. Both writes are best effort; ``app.log`` is written first, so a broken
    stderr can neither lose the log line nor change the guard's exit code.
    """
    paths = DataPaths(dataDir)
    payload: dict[str, Any] = {
        "timestamp": toIso(clock.now()),
        "level": "ERROR",
        "traceId": traceIdVar.get(),
        "message": str(error),
        "detail": {"lockPath": str(paths.lock), "exitCode": exitCodeFor(error)},
    }
    line = json.dumps(payload, ensure_ascii=False)
    try:
        paths.logsDir.mkdir(parents=True, exist_ok=True)
        with open(paths.appLog, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass  # stderr still carries the error
    writeStderrLine(line, payload)


def writeStderrLine(line: str, payload: dict[str, Any]) -> None:
    stream = sys.stderr
    if stream is None:  # e.g. pythonw.exe
        return
    try:
        try:
            stream.write(line + "\n")
        except UnicodeEncodeError:
            # A console code page that cannot show the path: same JSON value, ASCII-escaped.
            stream.write(json.dumps(payload, ensure_ascii=True) + "\n")
        stream.flush()
    except (OSError, ValueError):
        pass  # broken pipe, invalid console handle or closed stream; app.log has the line


def runSecondInstanceGuard(dataDir: str | Path, clock: Clock) -> int:
    """Return 0 if the lock is free (taken and released again), else report and return 3 or 4.

    TASK-009's ``--check-lock-only`` returns this function's result.
    """
    lock = InstanceLock(dataDir, clock)
    try:
        lock.acquire()
    except (LockHeldError, LockUnavailableError) as error:
        reportSecondInstance(dataDir, error, clock)
        return exitCodeFor(error)
    lock.release()
    return 0
