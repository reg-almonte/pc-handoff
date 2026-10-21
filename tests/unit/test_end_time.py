"""Unit tests for ``pc_handoff.domain.end_time`` (TASK-003 AC-2..AC-5, AC-7, AC-9).

Table-driven: each parametrize row is one AC scenario. All times are host-local +09:00.
"""

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import pytest

from pc_handoff.detection.provider import ConnectionType
from pc_handoff.domain.end_time import (
    ObservedState,
    acceptDisconnectTime,
    computeBlindEnd,
    computeDisconnectedSince,
    computeObservedEnd,
    deriveEndCause,
    durationSeconds,
    reconnectWindowUpper,
)
from pc_handoff.domain.models import EndCause, UsagePeriod

TZ = timezone(timedelta(hours=9))
PERIOD_ID = "8d0f8a43-5d0e-4a51-9a39-3f3c1f1c2b10"


def t(hhmmss: str | None) -> datetime | None:
    """10:30:05 -> 2026-10-21T10:30:05+09:00; None stays None."""
    if hhmmss is None:
        return None
    h, m, s = (int(part) for part in hhmmss.split(":"))
    return datetime(2026, 10, 21, h, m, s, tzinfo=TZ)


def period(start: str, startEvent: str | None = None, approximate: bool = False) -> UsagePeriod:
    return UsagePeriod(
        usagePeriodId=PERIOD_ID,
        usagePeriodStart=t(start),
        durationApproximate=approximate,
        startEventTimestamp=t(startEvent),
        connectTimeAtStart=t(start),
        connectionTypeAtStart=ConnectionType.RDP,
    )


# --- acceptDisconnectTime ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("osTime", "expected"),
    [
        ("10:00:00", "10:00:00"),  # lower bound is inclusive
        ("10:15:00", "10:15:00"),
        ("10:30:05", "10:30:05"),  # upper bound is inclusive
        ("09:59:59", None),
        ("10:30:06", None),
        (None, None),
    ],
)
def test_accept_disconnect_time_inclusive_window(osTime: str | None, expected: str | None) -> None:
    assert acceptDisconnectTime(t(osTime), t("10:00:00"), t("10:30:05")) == t(expected)


# --- AC-2: computeObservedEnd -----------------------------------------------------------


@pytest.mark.parametrize(
    ("osDisconnect", "expected"),
    [
        ("10:30:01", "10:30:01"),  # inside [C1, detection]
        ("09:00:00", "10:30:05"),  # stale, before C1
        (None, "10:30:05"),  # not reported
        ("10:31:00", "10:30:05"),  # after detection time
    ],
)
def test_ac02_observed_end_uses_window(osDisconnect: str | None, expected: str) -> None:
    result = computeObservedEnd(period("10:00:00"), t(osDisconnect), t("10:30:05"))
    assert result == t(expected)


# --- AC-3: computeBlindEnd and durationSeconds ------------------------------------------


@pytest.mark.parametrize(
    ("case", "osDisconnect", "windowUpper", "lastPoll", "startEvent", "expectedEnd", "expectedDur"),
    [
        ("a", "10:20:00", "10:40:00", "10:30:00", "10:00:03", "10:20:00", 1200),
        ("b", "10:45:00", "10:40:00", "10:30:00", "10:00:03", "10:30:00", 1800),
        ("c", None, "10:40:00", "09:59:00", "10:00:03", "10:00:03", 3),
        ("d", None, "10:40:00", None, "10:00:03", "10:00:03", 3),
        ("e", None, "10:40:00", None, None, None, None),
    ],
)
def test_ac03_blind_rule(
    case: str,
    osDisconnect: str | None,
    windowUpper: str,
    lastPoll: str | None,
    startEvent: str | None,
    expectedEnd: str | None,
    expectedDur: int | None,
) -> None:
    p = period("10:00:00", startEvent)
    end = computeBlindEnd(p, t(osDisconnect), t(windowUpper), t(lastPoll), t(startEvent))
    assert end == t(expectedEnd), case
    assert durationSeconds(p, end) == expectedDur, case


@pytest.mark.parametrize(
    ("osDisconnect", "expected"),
    [
        ("10:00:00", "10:00:00"),  # equal to usagePeriodStart: accepted
        ("10:40:00", "10:40:00"),  # equal to windowUpper: accepted
        ("09:59:59", "10:30:00"),  # before start: fallback max(P, S)
    ],
)
def test_blind_rule_window_bounds_inclusive(osDisconnect: str, expected: str) -> None:
    p = period("10:00:00", "10:00:03")
    end = computeBlindEnd(p, t(osDisconnect), t("10:40:00"), t("10:30:00"), t("10:00:03"))
    assert end == t(expected)


def test_blind_rule_uses_p_when_start_event_unknown() -> None:
    p = period("10:00:00", None)
    assert computeBlindEnd(p, None, t("10:40:00"), t("10:30:00"), None) == t("10:30:00")


# --- AC-4: reconnectWindowUpper ---------------------------------------------------------


@pytest.mark.parametrize(
    ("newConnect", "expected"),
    [("10:10:00", "10:10:00"), (None, "10:10:04")],
)
def test_ac04_reconnect_window_upper(newConnect: str | None, expected: str) -> None:
    assert reconnectWindowUpper(t(newConnect), t("10:10:04")) == t(expected)


# --- AC-5: deriveEndCause ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("observed", "expected"),
    [
        (ObservedState.DISCONNECTED, "disconnect"),
        (ObservedState.GONE, "logoff"),
        (ObservedState.IDENTITY_REPLACED, "logoff"),
        (ObservedState.ACTIVE_RECONNECTED, "reconnect_detected"),
        (None, "unknown"),
    ],
)
def test_ac05_end_cause(observed: ObservedState | None, expected: str) -> None:
    result = deriveEndCause(observed)
    assert isinstance(result, EndCause)
    assert result.value == expected


# --- AC-7: computeDisconnectedSince -----------------------------------------------------

AC7_OS_TIMES = ["10:30:01", "10:10:00", "09:00:00", "10:31:00", None]


@pytest.mark.parametrize(
    ("osDisconnect", "expected"),
    list(
        zip(AC7_OS_TIMES, ["10:30:01", "10:30:05", "10:30:05", "10:30:05", "10:30:05"], strict=True)
    ),
)
def test_ac07_disconnected_since_equals_observed_end(
    osDisconnect: str | None, expected: str
) -> None:
    # Type-change reconnect period: started at detection 10:20:00 while the OS still reports
    # the old connect time 10:00:00 (interpretation I1).
    p = period("10:20:00", "10:20:00", approximate=True)
    since = computeDisconnectedSince(t(osDisconnect), p, t("10:00:00"), t("10:30:05"))
    end = computeObservedEnd(p, t(osDisconnect), t("10:30:05"))
    assert since == end == t(expected)


@pytest.mark.parametrize(
    ("osDisconnect", "expected"),
    list(
        zip(AC7_OS_TIMES, ["10:30:01", "10:10:00", "10:30:05", "10:30:05", "10:30:05"], strict=True)
    ),
)
def test_ac07_disconnected_since_without_open_period(
    osDisconnect: str | None, expected: str
) -> None:
    since = computeDisconnectedSince(t(osDisconnect), None, t("10:00:00"), t("10:30:05"))
    assert since == t(expected)


@pytest.mark.parametrize(
    ("osDisconnect", "expected"),
    [("09:00:00", "09:00:00"), ("10:30:05", "10:30:05"), ("10:30:06", "10:30:05")],
)
def test_disconnected_since_without_open_period_and_unknown_connect_time(
    osDisconnect: str, expected: str
) -> None:
    # No reported connect time: only the "not later than detection time" bound applies.
    since = computeDisconnectedSince(t(osDisconnect), None, None, t("10:30:05"))
    assert since == t(expected)


def test_disconnected_since_lower_bound_is_inclusive() -> None:
    since = computeDisconnectedSince(t("10:00:00"), None, t("10:00:00"), t("10:30:05"))
    assert since == t("10:00:00")


# --- durationSeconds and AC-9 -----------------------------------------------------------


def test_duration_seconds_none_when_period_end_none(
    capturedAppLog: Callable[[], list[dict]],
) -> None:
    assert durationSeconds(period("10:00:00"), None) is None
    assert capturedAppLog() == []


def test_duration_seconds_whole_seconds(capturedAppLog: Callable[[], list[dict]]) -> None:
    assert durationSeconds(period("10:00:00"), t("10:00:00")) == 0
    assert durationSeconds(period("10:00:00"), t("11:02:03")) == 3723
    assert capturedAppLog() == []


def test_ac09_duration_guard_logs_error_when_bypassed(
    capturedAppLog: Callable[[], list[dict]],
) -> None:
    result = durationSeconds(period("10:00:00"), t("09:59:59"))
    assert result == 0
    lines = capturedAppLog()
    assert len(lines) == 1
    assert lines[0]["level"] == "ERROR"
    assert PERIOD_ID in lines[0]["message"]
    assert "newEndEvent" in lines[0]["message"]
