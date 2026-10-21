"""Shared pytest fixtures."""

import io
import json
import logging
from collections.abc import Callable, Iterator
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
