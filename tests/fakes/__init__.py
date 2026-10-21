"""Test doubles shared across the test suite."""

from tests.fakes.fake_clock import FakeClock
from tests.fakes.fake_session_provider import FakeSessionProvider, makeSession
from tests.fakes.fake_wts_binding import (
    FakeWtsBinding,
    packClientAddress,
    packWtsInfo,
    toFiletime,
    winError,
)

__all__ = [
    "FakeClock",
    "FakeSessionProvider",
    "FakeWtsBinding",
    "makeSession",
    "packClientAddress",
    "packWtsInfo",
    "toFiletime",
    "winError",
]
