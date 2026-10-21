"""Clock abstraction and ISO 8601 timestamp helpers.

All persisted and logged timestamps are host-local, offset-aware and truncated to whole
seconds (ADR-005). Time-dependent logic takes a ``Clock`` so tests can inject a fake.
"""

import time
from datetime import datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Source of wall-clock and monotonic time."""

    def now(self) -> datetime:
        """Return the current host-local time, offset-aware, truncated to whole seconds."""

    def monotonic(self) -> float:
        """Return a monotonic time in seconds, for measuring intervals."""


class SystemClock:
    """``Clock`` backed by the operating system."""

    def now(self) -> datetime:
        return datetime.now().astimezone().replace(microsecond=0)

    def monotonic(self) -> float:
        return time.monotonic()


def isAware(dt: datetime) -> bool:
    """Return True if ``dt`` carries a usable UTC offset."""
    return dt.tzinfo is not None and dt.utcoffset() is not None


def toIso(dt: datetime) -> str:
    """Format an offset-aware datetime as ISO 8601 with offset and seconds precision.

    Fractional seconds are truncated. Raises ``ValueError`` for a naive datetime.
    """
    if not isAware(dt):
        raise ValueError(f"naive datetime has no UTC offset: {dt!r}")
    return dt.isoformat(timespec="seconds")


def parseIso(value: str) -> datetime:
    """Parse an ISO 8601 timestamp that carries an explicit offset (or ``Z``).

    Raises ``ValueError`` if the string is not ISO 8601 or has no offset.
    """
    dt = datetime.fromisoformat(value)
    if not isAware(dt):
        raise ValueError(f"timestamp has no UTC offset: {value!r}")
    return dt
