"""Tests for pc_handoff.clock and the FakeClock test double (TASK-001 AC-4)."""

from datetime import UTC, datetime, timedelta, timezone, tzinfo

import pytest

from pc_handoff.clock import Clock, SystemClock, isAware, parseIso, toIso
from tests.fakes import FakeClock

JST = timezone(timedelta(hours=9))


class TestSystemClock:
    def test_ac04_now_is_aware_and_whole_seconds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Force an instant with a sub-second fraction regardless of when the test runs.
        realDatetime = datetime

        class FractionalDatetime(datetime):
            @classmethod
            def now(cls, tz: tzinfo | None = None) -> datetime:
                return realDatetime(2026, 10, 21, 9, 0, 0, 987654)

        monkeypatch.setattr("pc_handoff.clock.datetime", FractionalDatetime)
        now = SystemClock().now()
        assert isAware(now)
        assert now.microsecond == 0
        assert now.replace(tzinfo=None) == realDatetime(2026, 10, 21, 9, 0, 0)

    def test_ac04_real_now_round_trips_through_iso(self) -> None:
        now = SystemClock().now()
        text = toIso(now)
        assert "." not in text
        assert text[-6] in "+-" and text[-3] == ":"
        assert parseIso(text) == now

    def test_monotonic_does_not_go_backwards(self) -> None:
        clock = SystemClock()
        first = clock.monotonic()
        assert clock.monotonic() >= first

    def test_system_clock_satisfies_protocol(self) -> None:
        assert isinstance(SystemClock(), Clock)


class TestToIso:
    def test_formats_offset_and_seconds_precision(self) -> None:
        assert toIso(datetime(2026, 10, 21, 9, 5, 7, tzinfo=JST)) == "2026-10-21T09:05:07+09:00"

    def test_truncates_fractional_seconds(self) -> None:
        dt = datetime(2026, 10, 21, 9, 5, 7, 999999, tzinfo=JST)
        assert toIso(dt) == "2026-10-21T09:05:07+09:00"

    def test_utc_uses_numeric_offset(self) -> None:
        assert toIso(datetime(2026, 10, 21, 0, 0, 0, tzinfo=UTC)).endswith("+00:00")

    def test_rejects_naive(self) -> None:
        with pytest.raises(ValueError):
            toIso(datetime(2026, 10, 21, 9, 0, 0))


class TestParseIso:
    def test_round_trip(self) -> None:
        dt = datetime(2026, 10, 21, 9, 0, 0, tzinfo=JST)
        assert parseIso(toIso(dt)) == dt
        assert parseIso(toIso(dt)).utcoffset() == timedelta(hours=9)

    def test_accepts_z_suffix(self) -> None:
        assert parseIso("2026-10-21T00:00:00Z") == datetime(2026, 10, 21, tzinfo=UTC)

    def test_ac04_rejects_naive_timestamp(self) -> None:
        with pytest.raises(ValueError):
            parseIso("2026-10-21T09:00:00")

    def test_rejects_garbage(self) -> None:
        with pytest.raises(ValueError):
            parseIso("not a timestamp")


class TestIsAware:
    def test_naive_and_aware(self) -> None:
        assert not isAware(datetime(2026, 10, 21))
        assert isAware(datetime(2026, 10, 21, tzinfo=JST))

    def test_tzinfo_without_offset_is_not_aware(self) -> None:
        class NoOffset(tzinfo):
            def utcoffset(self, dt: datetime | None) -> None:
                return None

        assert not isAware(datetime(2026, 10, 21, tzinfo=NoOffset()))


class TestFakeClock:
    def test_starts_at_given_time(self, fakeClock: FakeClock) -> None:
        assert toIso(fakeClock.now()) == "2026-10-21T09:00:00+09:00"
        assert isinstance(fakeClock, Clock)

    def test_advance_moves_wall_and_monotonic_together(self, fakeClock: FakeClock) -> None:
        startMono = fakeClock.monotonic()
        fakeClock.advance(5.5)
        assert fakeClock.monotonic() == startMono + 5.5
        # now() keeps the Clock contract: whole seconds.
        assert toIso(fakeClock.now()) == "2026-10-21T09:00:05+09:00"
        fakeClock.advance(0.5)
        assert toIso(fakeClock.now()) == "2026-10-21T09:00:06+09:00"

    def test_set_jumps_wall_clock_only(self, fakeClock: FakeClock) -> None:
        startMono = fakeClock.monotonic()
        earlier = datetime(2026, 10, 21, 8, 0, 0, tzinfo=JST)
        fakeClock.set(earlier)
        assert fakeClock.now() == earlier
        assert fakeClock.monotonic() == startMono

    def test_rejects_naive_and_negative(self, fakeClock: FakeClock) -> None:
        with pytest.raises(ValueError):
            FakeClock(datetime(2026, 10, 21))
        with pytest.raises(ValueError):
            fakeClock.set(datetime(2026, 10, 21))
        with pytest.raises(ValueError):
            fakeClock.advance(-1)
