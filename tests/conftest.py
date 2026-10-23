"""Shared pytest fixtures."""

import io
import json
import logging
import signal
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pc_handoff.app_logging import LOGGER_NAME, JsonLineFormatter
from tests.fakes import FakeClock

# A fixed, non-UTC offset so tests catch accidental UTC/naive handling.
FAKE_CLOCK_START = datetime(2026, 10, 21, 9, 0, 0, tzinfo=timezone(timedelta(hours=9)))


@pytest.fixture
def tmpDataDir(tmp_path: Path) -> Path:
    """A per-test data directory path (not created; code under test creates what it needs)."""
    return tmp_path / "data"


@pytest.fixture
def fakeClock() -> FakeClock:
    """A ``FakeClock`` starting at 2026-10-21T09:00:00+09:00."""
    return FakeClock(FAKE_CLOCK_START)


@pytest.fixture
def capturedAppLog() -> Iterator[Callable[[], list[dict]]]:
    """Capture ``pc_handoff`` log lines at DEBUG, formatted exactly as in ``app.log``.

    Yields a function returning the parsed JSON lines emitted so far. Only message, level,
    traceId and detail should be asserted; timestamps are wall-clock, not ``FakeClock``.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLineFormatter())
    logger = logging.getLogger(LOGGER_NAME)
    originalLevel = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)

    def lines() -> list[dict]:
        handler.flush()
        return [json.loads(line) for line in stream.getvalue().splitlines()]

    try:
        yield lines
    finally:
        logger.removeHandler(handler)
        logger.setLevel(originalLevel)


@pytest.fixture
def tracker(fakeClock: FakeClock):
    """A ``SessionTracker`` on ``fakeClock`` logging under ``pc_handoff.domain.tracker``."""
    from pc_handoff.domain.tracker import SessionTracker

    return SessionTracker(fakeClock, logging.getLogger(f"{LOGGER_NAME}.domain.tracker"))


def installWakeupFd(fd: int) -> int:
    """Install ``fd`` as the signal wakeup fd; returns what is installed afterwards.

    An fd the OS rejects (a socket already closed with its asyncio loop: WinError 6 or
    10038, or ``ValueError``) is replaced by -1, "none". Such an fd can never deliver a
    wakeup, so dropping it hides nothing; a live fd is kept exactly as found.
    """
    try:
        signal.set_wakeup_fd(fd)
        return fd
    except (OSError, ValueError):
        signal.set_wakeup_fd(-1)
        return -1


@contextmanager
def wakeupFdGuard() -> Iterator[None]:
    """Keep the wakeup fd as it was before the block, minus any dead fd (Critic TASK-011
    BLOCKING-1, N5). Main thread only, as ``signal.set_wakeup_fd`` is."""
    before = installWakeupFd(signal.set_wakeup_fd(-1))
    try:
        yield
    finally:
        installWakeupFd(before)


@pytest.fixture(autouse=True)
def restoreSignalWakeupFd() -> Iterator[None]:
    """Undo the signal wakeup fd that an ``AppTest`` run's asyncio loop leaves behind.

    On Windows that fd is a socket closed with its loop. A later test that raises a signal
    on the main thread (``tests/integration/test_launcher.py``) would then get an
    unraisable "not a socket" warning, and reinstalling it raises. Any earlier test (for
    example ``tests/ui``) may have left it, so setup and teardown both tolerate it. It
    lives in the root conftest so that every test order is protected (Critic TASK-011
    N5), not only orders that pass through ``tests/acceptance``.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    with wakeupFdGuard():
        yield
