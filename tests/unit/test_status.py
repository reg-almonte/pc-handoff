"""Unit tests for ``pc_handoff.domain.status`` (TASK-003 AC-1; REQ AC-2, AC-7, AC-24)."""

from datetime import datetime, timedelta, timezone

import pytest

from pc_handoff.detection.provider import ConnectionType
from pc_handoff.domain.models import PcStatus, SessionState, TrackedSession, UsagePeriod
from pc_handoff.domain.status import deriveStatus

TZ = timezone(timedelta(hours=9))
T0 = datetime(2026, 10, 21, 10, 0, 0, tzinfo=TZ)


def session(
    sessionId: int, state: SessionState, connectionType: ConnectionType = ConnectionType.RDP
) -> TrackedSession:
    openPeriod = None
    if state is SessionState.ACTIVE:
        openPeriod = UsagePeriod(
            usagePeriodId=f"00000000-0000-4000-8000-{sessionId:012d}",
            usagePeriodStart=T0,
            durationApproximate=False,
            startEventTimestamp=T0,
            connectTimeAtStart=T0,
            connectionTypeAtStart=connectionType,
        )
    return TrackedSession(
        sessionId=sessionId,
        userName=f"user{sessionId}",
        domain="TEAM",
        state=state,
        connectionType=connectionType,
        clientName=None,
        clientAddress=None,
        logonTime=T0,
        firstDetectedAt=T0,
        connectTime=T0,
        osDisconnectTime=None,
        disconnectedSince=None if state is SessionState.ACTIVE else T0,
        openPeriod=openPeriod,
        consecutiveTransitionalPolls=0,
    )


ACTIVE = SessionState.ACTIVE
DISC = SessionState.DISCONNECTED


@pytest.mark.parametrize(
    ("label", "sessions", "expected"),
    [
        ("none", [], PcStatus.AVAILABLE),
        ("one Disconnected", [session(2, DISC)], PcStatus.AVAILABLE),
        (
            "Active RDP + Disconnected",
            [session(2, ACTIVE), session(3, DISC)],
            PcStatus.IN_USE,
        ),
        ("two Disconnected", [session(2, DISC), session(3, DISC)], PcStatus.AVAILABLE),
        (
            "one Active Console",
            [session(1, ACTIVE, ConnectionType.CONSOLE)],
            PcStatus.IN_USE,
        ),
    ],
)
def test_ac01_status_from_active_sessions_only(
    label: str, sessions: list[TrackedSession], expected: PcStatus
) -> None:
    assert deriveStatus(sessions) is expected, label


def test_derive_status_accepts_any_iterable() -> None:
    gen = (s for s in [session(3, DISC), session(2, ACTIVE)])
    assert deriveStatus(gen) is PcStatus.IN_USE


def test_pc_status_values_are_requirement_strings() -> None:
    assert [s.value for s in PcStatus] == ["Available", "In Use", "Unknown"]
    assert isinstance(PcStatus.IN_USE, str)
    assert PcStatus("In Use") is PcStatus.IN_USE
