"""``StaticSessionProvider``: sessions described in a JSON file, re-read per call (TASK-009)."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pc_handoff.detection.provider import (
    ConnectionType,
    OsConnectState,
    SessionEnumerationError,
    SessionProvider,
)
from pc_handoff.detection.static_provider import StaticSessionProvider

JST = timezone(timedelta(hours=9))


@pytest.fixture
def sessionsFile(tmp_path: Path) -> Path:
    return tmp_path / "sessions.json"


def write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_is_a_session_provider(sessionsFile):
    assert isinstance(StaticSessionProvider(sessionsFile), SessionProvider)


def test_full_session_round_trip(sessionsFile):
    write(
        sessionsFile,
        [
            {
                "sessionId": 2,
                "winStationName": "RDP-Tcp#3",
                "userName": "alice",
                "domain": "CORP",
                "state": "Active",
                "connectionType": "RDP",
                "protocolType": 2,
                "clientName": "ALICE-LAPTOP",
                "clientAddress": "192.168.1.20",
                "logonTime": "2026-10-21T08:00:00+09:00",
                "connectTime": "2026-10-21T08:30:00.750+09:00",
                "disconnectTime": None,
            }
        ],
    )
    [session] = StaticSessionProvider(sessionsFile).enumerateSessions()
    assert session.sessionId == 2
    assert session.winStationName == "RDP-Tcp#3"
    assert (session.userName, session.domain) == ("alice", "CORP")
    assert session.rawState is OsConnectState.ACTIVE
    assert session.rawStateName == "WTSActive"
    assert session.connectionType is ConnectionType.RDP
    assert session.protocolType == 2
    assert (session.clientName, session.clientAddress) == ("ALICE-LAPTOP", "192.168.1.20")
    assert session.logonTime == datetime(2026, 10, 21, 8, 0, tzinfo=JST)
    # Whole seconds, like the WTS provider.
    assert session.connectTime == datetime(2026, 10, 21, 8, 30, tzinfo=JST)
    assert session.disconnectTime is None


def test_defaults_for_omitted_fields(sessionsFile):
    write(
        sessionsFile,
        [
            {"sessionId": 1, "userName": "erin", "state": "Active", "connectionType": "Console"},
            {"sessionId": 3, "userName": "bob", "state": "Disconnected"},
            {"sessionId": 4, "userName": "carol", "state": "Transitional"},
            {"sessionId": 65536, "userName": None, "state": "Ignored"},
        ],
    )
    console, bob, carol, listener = StaticSessionProvider(sessionsFile).enumerateSessions()
    assert console.connectionType is ConnectionType.CONSOLE
    assert console.protocolType == 0
    assert (console.domain, console.clientName, console.clientAddress) == (None, None, None)
    assert (console.logonTime, console.connectTime, console.disconnectTime) == (None, None, None)
    assert console.winStationName is None
    assert bob.connectionType is ConnectionType.RDP and bob.protocolType == 2
    assert (bob.rawState, bob.rawStateName) == (OsConnectState.DISCONNECTED, "WTSDisconnected")
    assert (carol.rawState, carol.rawStateName) == (OsConnectState.TRANSITIONAL, "WTSConnected")
    assert (listener.rawState, listener.rawStateName) == (OsConnectState.IGNORED, "WTSListen")
    assert listener.userName is None


def test_raw_state_name_can_be_given(sessionsFile):
    entry = {"sessionId": 2, "userName": "a", "state": "Transitional", "rawStateName": "WTSShadow"}
    write(sessionsFile, [entry])
    [session] = StaticSessionProvider(sessionsFile).enumerateSessions()
    assert session.rawStateName == "WTSShadow"


def test_the_file_is_reread_on_every_enumeration(sessionsFile):
    provider = StaticSessionProvider(sessionsFile)
    write(sessionsFile, [{"sessionId": 2, "userName": "alice", "state": "Active"}])
    assert [s.userName for s in provider.enumerateSessions()] == ["alice"]
    write(sessionsFile, [])
    assert provider.enumerateSessions() == []


def test_a_bom_is_tolerated(sessionsFile):
    sessionsFile.write_bytes(b"\xef\xbb\xbf[]")
    assert StaticSessionProvider(sessionsFile).enumerateSessions() == []


@pytest.mark.parametrize("kind", ["error", "access_denied", "timeout"])
def test_a_fail_document_fails_the_enumeration(sessionsFile, kind):
    write(sessionsFile, {"fail": kind})
    with pytest.raises(SessionEnumerationError) as info:
        StaticSessionProvider(sessionsFile).enumerateSessions()
    assert info.value.kind == kind


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (None, "cannot be read"),
        (b"not json", "cannot be read"),
        (b"\xff\xfe", "cannot be read"),
        (b'{"sessions": []}', "must be a JSON array"),
        (b'{"fail": "nope"}', "unknown enumeration error kind"),
        (b"[1]", "session 0: must be a JSON object"),
        (b'[{"userName": "a", "state": "Active"}]', "session 0: sessionId"),
        (b'[{"sessionId": true, "state": "Active"}]', "session 0: sessionId"),
        (b'[{"sessionId": 2, "userName": "a"}]', "session 0: state"),
        (b'[{"sessionId": 2, "state": "Sleeping"}]', "session 0: state"),
        (b'[{"sessionId": 2, "state": "Active", "connectionType": "VNC"}]', "connectionType"),
        (b'[{"sessionId": 2, "state": "Active", "userName": 7}]', "userName"),
        (b'[{"sessionId": 2, "state": "Active", "protocolType": "2"}]', "protocolType"),
        (b'[{"sessionId": 2, "state": "Active", "logonTime": "yesterday"}]', "logonTime"),
        (b'[{"sessionId": 2, "state": "Active", "logonTime": 1729465200}]', "logonTime"),
        (b'[{"sessionId": 2, "state": "Active", "rawStateName": 5}]', "rawStateName"),
        (b'[{"sessionId": 2, "state": "Active", "connectTime": "2026-10-21T08:00"}]', "connect"),
        (b'[{"sessionId": 2, "state": "Active", "colour": "red"}]', "unknown field"),
    ],
)
def test_invalid_files_fail_the_enumeration_as_kind_error(sessionsFile, content, reason):
    if content is not None:
        sessionsFile.write_bytes(content)
    with pytest.raises(SessionEnumerationError) as info:
        StaticSessionProvider(sessionsFile).enumerateSessions()
    assert info.value.kind == "error"
    assert reason in info.value.message
