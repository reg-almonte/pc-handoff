"""Tests for the detection fakes and the provider model (TASK-002)."""

import threading

import pytest

from pc_handoff.detection.provider import (
    ConnectionType,
    OsConnectState,
    SessionEnumerationError,
    SessionProvider,
)
from tests.fakes import FakeSessionProvider, FakeWtsBinding, makeSession, winError
from tests.fakes.fake_session_provider import DEFAULT_LOGON_TIME


class TestSessionEnumerationError:
    def test_fields_and_str(self) -> None:
        error = SessionEnumerationError("access_denied", "denied", winerror=5)
        assert (error.kind, error.message, error.winerror) == ("access_denied", "denied", 5)
        assert str(error) == "access_denied: denied"

    def test_message_defaults_to_kind(self) -> None:
        assert str(SessionEnumerationError("timeout")) == "timeout: timeout"

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown enumeration error kind"):
            SessionEnumerationError("bogus")


class TestMakeSession:
    def test_rdp_defaults(self) -> None:
        session = makeSession(2, "alice")
        assert session.rawState is OsConnectState.ACTIVE
        assert session.rawStateName == "WTSActive"
        assert session.connectionType is ConnectionType.RDP
        assert (session.winStationName, session.protocolType) == ("RDP-Tcp#2", 2)
        assert (session.clientName, session.clientAddress) == ("CLIENT-PC", "192.168.1.20")
        assert session.logonTime == session.connectTime == DEFAULT_LOGON_TIME
        assert session.disconnectTime is None
        assert session.domain == "CORP"

    def test_console_and_enum_arguments(self) -> None:
        session = makeSession(1, "bob", OsConnectState.DISCONNECTED, ConnectionType.CONSOLE)
        assert session.rawState is OsConnectState.DISCONNECTED
        assert (session.winStationName, session.protocolType) == ("Console", 0)
        assert (session.clientName, session.clientAddress) == (None, None)

    def test_overrides_and_no_user(self) -> None:
        session = makeSession(
            0, None, "Transitional", clientName=None, connectTime=None, rawStateName="WTSInit"
        )
        assert session.userName is None and session.domain is None
        assert session.rawStateName == "WTSInit"
        assert (session.clientName, session.connectTime) == (None, None)

    def test_unknown_override_rejected(self) -> None:
        with pytest.raises(TypeError, match="unknown OsSession fields"):
            makeSession(2, "alice", colour="red")


class TestFakeSessionProvider:
    def test_is_a_session_provider(self) -> None:
        assert isinstance(FakeSessionProvider(), SessionProvider)

    def test_set_sessions_and_copy(self) -> None:
        provider = FakeSessionProvider()
        assert provider.enumerateSessions() == []
        provider.setSessions([makeSession(2, "alice")])
        result = provider.enumerateSessions()
        result.clear()
        assert len(provider.enumerateSessions()) == 1
        assert provider.callCount == 3

    def test_blocked_call_returns_sessions_current_at_release(self) -> None:
        provider = FakeSessionProvider([makeSession(2, "alice")])
        release = threading.Event()
        provider.blockNext(release)
        results: list = []
        worker = threading.Thread(target=lambda: results.append(provider.enumerateSessions()))
        worker.start()
        worker.join(0.1)
        assert worker.is_alive()
        provider.setSessions([makeSession(3, "bob")])
        release.set()
        worker.join(5)
        assert [s.userName for s in results[0]] == ["bob"]


class TestFakeWtsBinding:
    def test_unconfigured_query_is_a_setup_error(self) -> None:
        with pytest.raises(LookupError, match="no fake response"):
            FakeWtsBinding().queryString(1, 5)

    def test_win_error_carries_code(self) -> None:
        error = winError(5)
        assert isinstance(error, OSError) and error.winerror == 5

    def test_records_calls(self) -> None:
        binding = FakeWtsBinding()
        binding.addSession(1, "Console", 0, userName="bob")
        binding.enumerate()
        assert binding.queryUShort(1, 16) == 2
        assert binding.calls == [("enumerate", -1, -1), ("queryUShort", 1, 16)]
