"""Tests for pc_handoff.detection.wts_provider with FakeWtsBinding (TASK-002 AC-4, AC-5)."""

import sys
from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from pc_handoff.detection import wts_provider
from pc_handoff.detection.provider import (
    ConnectionType,
    OsConnectState,
    SessionEnumerationError,
    SessionProvider,
)
from pc_handoff.detection.wts_decode import (
    AF_INET,
    WTS_CLIENT_ADDRESS,
    WTS_CLIENT_NAME,
    WTS_CLIENT_PROTOCOL_TYPE,
    WTS_DOMAIN_NAME,
    WTS_SESSION_INFO,
    WTS_USER_NAME,
)
from pc_handoff.detection.wts_provider import WtsSessionProvider, createDefaultProvider
from tests.fakes import FakeWtsBinding, packClientAddress, packWtsInfo, toFiletime, winError

LOGON = datetime(2026, 10, 21, 0, 0, 5, tzinfo=UTC)
CONNECT = datetime(2026, 10, 21, 0, 10, 0, 250000, tzinfo=UTC)
DISCONNECT = datetime(2026, 10, 20, 23, 59, 59, tzinfo=UTC)
ALICE_ADDRESS = packClientAddress(AF_INET, bytes([0, 0, 192, 168, 1, 20]))


def standardBinding() -> FakeWtsBinding:
    """Services session 0, the RDP listener, alice on RDP (2) and bob at the console (1)."""
    binding = FakeWtsBinding()
    binding.addSession(0, "Services", 4, protocolType=0)
    binding.addSession(65536, "RDP-Tcp", 6, protocolType=2)
    binding.addSession(
        2,
        "RDP-Tcp#3",
        0,
        userName="alice",
        domain="CORP",
        clientName="ALICE-LAPTOP",
        clientAddress=ALICE_ADDRESS,
        protocolType=2,
        connectTime=toFiletime(CONNECT),
        disconnectTime=toFiletime(DISCONNECT),
        logonTime=toFiletime(LOGON),
    )
    binding.addSession(
        1,
        "Console",
        4,
        userName="bob",
        domain="CORP",
        protocolType=0,
        logonTime=toFiletime(LOGON),
    )
    return binding


def byId(binding: FakeWtsBinding) -> dict:
    return {s.sessionId: s for s in WtsSessionProvider(binding).enumerateSessions()}


class TestEnumerateSessions:
    def test_is_a_session_provider(self) -> None:
        assert isinstance(WtsSessionProvider(FakeWtsBinding()), SessionProvider)

    def test_full_decode(self) -> None:
        sessions = byId(standardBinding())
        assert list(sessions) == [0, 65536, 2, 1]  # OS order kept, nothing filtered

        alice = sessions[2]
        assert alice.winStationName == "RDP-Tcp#3"
        assert (alice.userName, alice.domain) == ("alice", "CORP")
        assert (alice.rawState, alice.rawStateName) == (OsConnectState.ACTIVE, "WTSActive")
        assert (alice.connectionType, alice.protocolType) == (ConnectionType.RDP, 2)
        assert (alice.clientName, alice.clientAddress) == ("ALICE-LAPTOP", "192.168.1.20")
        assert alice.logonTime == LOGON
        assert alice.connectTime == CONNECT.replace(microsecond=0)
        assert alice.disconnectTime == DISCONNECT

        bob = sessions[1]
        assert bob.rawState is OsConnectState.DISCONNECTED
        assert bob.connectionType is ConnectionType.CONSOLE
        assert (bob.clientName, bob.clientAddress) == (None, None)  # empty -> None
        assert (bob.connectTime, bob.disconnectTime) == (None, None)  # FILETIME 0 -> None

        listener = sessions[65536]
        assert (listener.rawState, listener.rawStateName) == (OsConnectState.IGNORED, "WTSListen")
        assert listener.userName is None

        assert sessions[0].userName is None

    def test_empty_enumeration(self) -> None:
        assert WtsSessionProvider(FakeWtsBinding()).enumerateSessions() == []

    def test_state_comes_from_session_info_when_available(self) -> None:
        binding = FakeWtsBinding()
        binding.addSession(3, "RDP-Tcp#1", 0, userName="carol", infoState=4)
        assert byId(binding)[3].rawState is OsConnectState.DISCONNECTED

    def test_unknown_state_is_transitional_and_logged(
        self, capturedAppLog: Callable[[], list[dict]]
    ) -> None:
        binding = FakeWtsBinding()
        binding.addSession(3, "RDP-Tcp#1", 42, userName="carol")
        session = byId(binding)[3]
        assert (session.rawState, session.rawStateName) == (
            OsConnectState.TRANSITIONAL,
            "unknown(42)",
        )
        lines = [
            line for line in capturedAppLog() if "unknown WTS connect state" in line["message"]
        ]
        assert lines and lines[0]["level"] == "DEBUG"
        assert lines[0]["detail"] == {"sessionId": 3, "state": 42}


class TestPerFieldFallback:
    def test_ac04_client_fields_fail_only_those_fields(
        self, capturedAppLog: Callable[[], list[dict]]
    ) -> None:
        binding = standardBinding()
        binding.setResponse(2, WTS_CLIENT_NAME, winError(5))
        binding.setResponse(2, WTS_CLIENT_ADDRESS, winError(5))

        alice = byId(binding)[2]  # no exception

        assert (alice.clientName, alice.clientAddress) == (None, None)
        assert alice.userName == "alice"
        assert alice.rawState is OsConnectState.ACTIVE
        assert alice.protocolType == 2
        assert alice.logonTime == LOGON and alice.connectTime is not None
        assert alice.disconnectTime == DISCONNECT

        failures = [
            line for line in capturedAppLog() if line["message"] == "WTS session query failed"
        ]
        assert [
            (f["level"], f["detail"]["sessionId"], f["detail"]["winerror"]) for f in failures
        ] == [
            ("DEBUG", 2, 5),
            ("DEBUG", 2, 5),
        ]
        assert [f["detail"]["infoClass"] for f in failures] == [WTS_CLIENT_NAME, WTS_CLIENT_ADDRESS]

    def test_session_info_failure_nulls_times_and_uses_enumeration_state(self) -> None:
        binding = standardBinding()
        binding.setResponse(2, WTS_SESSION_INFO, winError(5))
        alice = byId(binding)[2]
        assert (alice.logonTime, alice.connectTime, alice.disconnectTime) == (None, None, None)
        assert alice.rawState is OsConnectState.ACTIVE  # from the enumeration tuple
        assert (alice.userName, alice.clientName) == ("alice", "ALICE-LAPTOP")

    def test_truncated_session_info_is_treated_as_failure(self) -> None:
        binding = standardBinding()
        binding.setResponse(2, WTS_SESSION_INFO, b"\x00" * 10)
        alice = byId(binding)[2]
        assert alice.logonTime is None and alice.rawState is OsConnectState.ACTIVE

    def test_protocol_failure_falls_back_to_station_name(self) -> None:
        binding = standardBinding()
        binding.setResponse(1, WTS_CLIENT_PROTOCOL_TYPE, winError(5))
        binding.setResponse(2, WTS_CLIENT_PROTOCOL_TYPE, winError(5))
        sessions = byId(binding)
        assert sessions[1].protocolType is None
        assert sessions[1].connectionType is ConnectionType.CONSOLE  # "Console" station
        assert sessions[2].connectionType is ConnectionType.RDP  # "RDP-Tcp#3" station

    def test_short_client_address_buffer_is_none(self) -> None:
        binding = standardBinding()
        binding.setResponse(2, WTS_CLIENT_ADDRESS, b"\x02\x00")
        assert byId(binding)[2].clientAddress is None

    def test_failure_without_winerror_is_logged_with_null(
        self, capturedAppLog: Callable[[], list[dict]]
    ) -> None:
        binding = standardBinding()
        binding.setResponse(2, WTS_CLIENT_NAME, OSError("no code"))
        assert byId(binding)[2].clientName is None
        failure = next(x for x in capturedAppLog() if x["message"] == "WTS session query failed")
        assert failure["detail"]["winerror"] is None

    def test_unexpected_exception_propagates(self) -> None:
        binding = standardBinding()
        binding.setResponse(2, WTS_CLIENT_NAME, RuntimeError("bug"))
        with pytest.raises(RuntimeError, match="bug"):
            WtsSessionProvider(binding).enumerateSessions()


def withInfoNames(binding: FakeWtsBinding, userName: str = "", domain: str = "") -> None:
    """Replace session 2's WTSINFOW with one carrying the given (possibly truncated) names."""
    binding.setResponse(
        2,
        WTS_SESSION_INFO,
        packWtsInfo(
            0,
            2,
            winStationName="RDP-Tcp#3",
            domain=domain,
            userName=userName,
            logonTime=toFiletime(LOGON),
        ),
    )


def fallbackLines(capturedAppLog: Callable[[], list[dict]]) -> list[dict]:
    return [x for x in capturedAppLog() if x["message"] == "WTS name recovered from WTSINFOW"]


class TestNameFallbackToSessionInfo:
    """Critic TASK-002 S1: a failed name query falls back to an untruncated WTSINFOW copy."""

    def test_user_query_fails_short_info_name_is_recovered(
        self, capturedAppLog: Callable[[], list[dict]]
    ) -> None:
        binding = standardBinding()
        binding.setResponse(2, WTS_USER_NAME, winError(1722))
        alice = byId(binding)[2]
        assert alice.userName == "alice"
        assert (alice.domain, alice.clientName) == ("CORP", "ALICE-LAPTOP")
        [line] = fallbackLines(capturedAppLog)
        assert line["level"] == "DEBUG"
        assert line["detail"] == {"sessionId": 2, "infoClass": WTS_USER_NAME, "winerror": 1722}

    def test_user_name_of_19_characters_is_recovered(self) -> None:
        binding = standardBinding()
        withInfoNames(binding, userName="u" * 19, domain="CORP")
        binding.setResponse(2, WTS_USER_NAME, winError(5))
        assert byId(binding)[2].userName == "u" * 19

    @pytest.mark.parametrize("fullName", ["a" * 20, "averyveryverylongusername25"])
    def test_user_name_at_truncation_length_is_not_used(
        self, fullName: str, capturedAppLog: Callable[[], list[dict]]
    ) -> None:
        binding = standardBinding()
        withInfoNames(binding, userName=fullName, domain="CORP")  # WCHAR[21] keeps 20 chars
        binding.setResponse(2, WTS_USER_NAME, winError(5))
        alice = byId(binding)[2]
        assert alice.userName is None
        assert alice.domain == "CORP"
        assert fallbackLines(capturedAppLog) == []

    def test_domain_query_fails_info_domain_is_recovered(
        self, capturedAppLog: Callable[[], list[dict]]
    ) -> None:
        binding = standardBinding()
        withInfoNames(binding, userName="alice", domain="D" * 16)  # WCHAR[17] holds 16 chars
        binding.setResponse(2, WTS_DOMAIN_NAME, winError(1722))
        alice = byId(binding)[2]
        assert alice.domain == "D" * 16
        assert alice.userName == "alice"
        [line] = fallbackLines(capturedAppLog)
        assert line["level"] == "DEBUG"
        assert line["detail"] == {"sessionId": 2, "infoClass": WTS_DOMAIN_NAME, "winerror": 1722}

    @pytest.mark.parametrize("infoFailure", [winError(5), b"\x00" * 10])
    def test_name_and_session_info_both_fail(
        self, infoFailure: object, capturedAppLog: Callable[[], list[dict]]
    ) -> None:
        binding = standardBinding()
        binding.setResponse(2, WTS_SESSION_INFO, infoFailure)
        binding.setResponse(2, WTS_USER_NAME, winError(5))
        binding.setResponse(2, WTS_DOMAIN_NAME, winError(5))
        alice = byId(binding)[2]
        assert (alice.userName, alice.domain) == (None, None)
        assert fallbackLines(capturedAppLog) == []

    def test_empty_info_names_stay_none(self, capturedAppLog: Callable[[], list[dict]]) -> None:
        binding = standardBinding()
        withInfoNames(binding)
        binding.setResponse(2, WTS_USER_NAME, winError(5))
        binding.setResponse(2, WTS_DOMAIN_NAME, winError(5))
        alice = byId(binding)[2]
        assert (alice.userName, alice.domain) == (None, None)
        assert fallbackLines(capturedAppLog) == []

    def test_successful_empty_query_is_not_overridden(
        self, capturedAppLog: Callable[[], list[dict]]
    ) -> None:
        binding = standardBinding()
        binding.setResponse(2, WTS_USER_NAME, "")
        binding.setResponse(2, WTS_DOMAIN_NAME, "")
        alice = byId(binding)[2]
        assert (alice.userName, alice.domain) == (None, None)
        assert fallbackLines(capturedAppLog) == []


class TestEnumerationFailure:
    @pytest.mark.parametrize(
        ("winerror", "kind"), [(5, "access_denied"), (1722, "error"), (None, "error")]
    )
    def test_ac05_kind_from_winerror(self, winerror: int | None, kind: str) -> None:
        binding = standardBinding()
        error = winError(winerror) if winerror is not None else OSError("no code")
        binding.failEnumerate(error)
        with pytest.raises(SessionEnumerationError) as info:
            WtsSessionProvider(binding).enumerateSessions()
        assert info.value.kind == kind
        assert info.value.winerror == winerror
        assert info.value.__cause__ is error
        assert "WTSEnumerateSessionsW failed" in str(info.value)


class TestCreateDefaultProvider:
    def test_non_windows_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        with pytest.raises(RuntimeError, match="WTS provider requires Windows"):
            createDefaultProvider()

    def test_windows_uses_real_binding(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pc_handoff.detection import wts_binding

        class StubBinding(FakeWtsBinding):
            pass

        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(wts_binding, "RealWtsBinding", StubBinding)
        provider = createDefaultProvider()
        assert isinstance(provider, wts_provider.WtsSessionProvider)
        assert isinstance(provider.binding, StubBinding)
