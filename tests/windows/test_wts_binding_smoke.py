"""Real-WTS smoke test (TASK-002 AC-8). Windows only; read-only enumeration.

Exercises ``wts_binding.py`` (omitted from coverage) against the local server and checks the
struct layouts decode to plausible values for the test runner's own session.
"""

import ctypes
import os
import sys
from datetime import UTC, datetime, timedelta

import pytest

from pc_handoff.detection.provider import ConnectionType, OsConnectState
from pc_handoff.detection.wts_decode import WTS_SESSION_INFO, WTSINFOW_SIZE

pytestmark = [
    pytest.mark.windows,
    pytest.mark.skipif(sys.platform != "win32", reason="WTS API requires Windows"),
]

PLAUSIBLE_EARLIEST = datetime(2000, 1, 1, tzinfo=UTC)


def currentSessionId() -> int:
    sessionId = ctypes.c_uint32()
    ok = ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sessionId))
    assert ok, "ProcessIdToSessionId failed"
    return sessionId.value


def test_ac08_real_enumeration() -> None:
    from pc_handoff.detection.wts_provider import createDefaultProvider

    sessions = createDefaultProvider().enumerateSessions()  # no exception
    now = datetime.now().astimezone() + timedelta(seconds=1)
    assert sessions

    withUser = [s for s in sessions if s.userName]
    assert withUser, "expected at least the test runner's own session to have a user name"
    assert any(s.logonTime is not None and PLAUSIBLE_EARLIEST < s.logonTime < now for s in withUser)

    for session in sessions:
        for value in (session.logonTime, session.connectTime, session.disconnectTime):
            assert value is None or (value.tzinfo is not None and value.microsecond == 0)

    own = next(s for s in sessions if s.sessionId == currentSessionId())
    assert own.userName
    assert own.connectionType in (ConnectionType.RDP, ConnectionType.CONSOLE)
    assert own.logonTime is not None and PLAUSIBLE_EARLIEST < own.logonTime < now

    services = [s for s in sessions if s.sessionId == 0]
    for session in services:
        assert session.rawState in (
            OsConnectState.IGNORED,
            OsConnectState.TRANSITIONAL,
            OsConnectState.DISCONNECTED,
        )
        assert session.userName is None


def test_session_info_buffer_matches_declared_layout() -> None:
    from pc_handoff.detection.wts_binding import RealWtsBinding
    from pc_handoff.detection.wts_decode import parseWtsInfo

    binding = RealWtsBinding()
    sessionId = currentSessionId()
    raw = binding.queryBytes(sessionId, WTS_SESSION_INFO)
    assert len(raw) == WTSINFOW_SIZE
    info = parseWtsInfo(raw)
    assert info.sessionId == sessionId
    assert info.userName
