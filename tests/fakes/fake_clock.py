"""Deterministic ``Clock`` for tests."""

from datetime import datetime, timedelta

from pc_handoff.clock import isAware


class FakeClock:
    """Manually driven clock.

    ``advance(seconds)`` moves wall-clock and monotonic time forward together. ``set(dt)``
    jumps the wall clock (forwards or backwards) without moving monotonic time, as a real
    wall-clock step would.
    """

    def __init__(self, start: datetime, monotonicStart: float = 1000.0) -> None:
        self.current = self.requireAware(start)
        self.monotonicValue = float(monotonicStart)

    @staticmethod
    def requireAware(dt: datetime) -> datetime:
        if not isAware(dt):
            raise ValueError(f"FakeClock needs an offset-aware datetime, got {dt!r}")
        return dt

    def now(self) -> datetime:
        return self.current.replace(microsecond=0)

    def monotonic(self) -> float:
        return self.monotonicValue

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("advance() takes a non-negative number of seconds; use set()")
        self.current += timedelta(seconds=seconds)
        self.monotonicValue += seconds

    def set(self, dt: datetime) -> None:
        self.current = self.requireAware(dt)
