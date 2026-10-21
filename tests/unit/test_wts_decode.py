"""Tests for pc_handoff.detection.wts_decode (TASK-002 AC-1, AC-2, AC-3)."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from pc_handoff.detection.provider import ConnectionType, OsConnectState
from pc_handoff.detection.wts_decode import (
    AF_INET,
    AF_INET6,
    FILETIME_EPOCH_OFFSET,
    WTSINFOW_SIZE,
    cleanString,
    decodeClientAddress,
    filetimeToDatetime,
    mapConnectionType,
    mapConnectState,
    parseClientAddress,
    parseWtsInfo,
)
from tests.fakes import packClientAddress, packWtsInfo, toFiletime

LOGON_UTC = datetime(2026, 10, 21, 0, 15, 30, 987654, tzinfo=UTC)
DISCONNECT_UTC = datetime(2026, 10, 21, 1, 2, 3, 500000, tzinfo=UTC)


def localOffset(dt: datetime) -> timedelta | None:
    return dt.astimezone().utcoffset()


class TestMapConnectState:
    @pytest.mark.parametrize(
        ("value", "expected", "name"),
        [
            (0, OsConnectState.ACTIVE, "WTSActive"),
            (1, OsConnectState.TRANSITIONAL, "WTSConnected"),
            (2, OsConnectState.TRANSITIONAL, "WTSConnectQuery"),
            (3, OsConnectState.TRANSITIONAL, "WTSShadow"),
            (4, OsConnectState.DISCONNECTED, "WTSDisconnected"),
            (5, OsConnectState.TRANSITIONAL, "WTSIdle"),
            (6, OsConnectState.IGNORED, "WTSListen"),
            (7, OsConnectState.TRANSITIONAL, "WTSReset"),
            (8, OsConnectState.TRANSITIONAL, "WTSDown"),
            (9, OsConnectState.TRANSITIONAL, "WTSInit"),
            (42, OsConnectState.TRANSITIONAL, "unknown(42)"),
        ],
    )
    def test_ac01_state_table(self, value: int, expected: OsConnectState, name: str) -> None:
        assert mapConnectState(value) == (expected, name)


class TestMapConnectionType:
    @pytest.mark.parametrize("station", ["Console", "RDP-Tcp#3", None])
    def test_ac02_protocol_zero_is_console(self, station: str | None) -> None:
        assert mapConnectionType(0, station) is ConnectionType.CONSOLE

    @pytest.mark.parametrize("station", ["Console", "RDP-Tcp#3", None])
    @pytest.mark.parametrize("protocol", [1, 2])
    def test_ac02_nonzero_protocol_is_rdp(self, protocol: int, station: str | None) -> None:
        assert mapConnectionType(protocol, station) is ConnectionType.RDP

    @pytest.mark.parametrize(
        ("station", "expected"),
        [
            ("Console", ConnectionType.CONSOLE),
            ("RDP-Tcp#3", ConnectionType.RDP),
            (None, ConnectionType.RDP),
        ],
    )
    def test_ac02_station_name_fallback(self, station: str | None, expected: ConnectionType):
        assert mapConnectionType(None, station) is expected

    def test_values_match_requirement_enumeration(self) -> None:
        assert ConnectionType.RDP.value == "RDP"
        assert ConnectionType.CONSOLE.value == "Console"


class TestFiletimeToDatetime:
    def test_ac03_zero_is_none(self) -> None:
        assert filetimeToDatetime(0) is None

    def test_negative_is_none(self) -> None:
        assert filetimeToDatetime(-1) is None

    def test_ac03_epoch_offset_is_unix_epoch(self) -> None:
        result = filetimeToDatetime(FILETIME_EPOCH_OFFSET)
        assert result == datetime(1970, 1, 1, tzinfo=UTC)

    def test_ac03_aware_local_and_truncated(self) -> None:
        result = filetimeToDatetime(toFiletime(LOGON_UTC))
        assert result is not None
        assert result.tzinfo is not None
        assert result.microsecond == 0
        assert result == LOGON_UTC.replace(microsecond=0)
        assert result.utcoffset() == localOffset(LOGON_UTC)

    def test_sub_second_fraction_is_truncated_not_rounded(self) -> None:
        value = toFiletime(datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)) + 9_999_999  # +0.9999999 s
        assert filetimeToDatetime(value) == datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

    def test_out_of_range_is_none(self) -> None:
        assert filetimeToDatetime(2**63 - 1) is None


class TestDecodeClientAddress:
    def test_ac03_ipv4(self) -> None:
        raw = bytes([0, 0, 192, 168, 1, 20]) + bytes(14)
        assert decodeClientAddress(AF_INET, raw) == "192.168.1.20"

    def test_ipv6(self) -> None:
        address = bytes.fromhex("fe800000000000000000000000000001")
        assert decodeClientAddress(AF_INET6, b"\x00\x00" + address + b"\x00\x00") == "fe80::1"

    def test_ac03_family_zero_is_none(self) -> None:
        assert decodeClientAddress(0, bytes([0, 0, 192, 168, 1, 20]) + bytes(14)) is None

    @pytest.mark.parametrize("family", [AF_INET, AF_INET6])
    def test_ac03_all_zero_is_none(self, family: int) -> None:
        assert decodeClientAddress(family, bytes(20)) is None

    @pytest.mark.parametrize("family", [AF_INET, AF_INET6])
    def test_short_buffer_is_none(self, family: int) -> None:
        assert decodeClientAddress(family, b"\x00\x00\x01") is None

    def test_parse_whole_struct(self) -> None:
        raw = packClientAddress(AF_INET, bytes([0, 0, 10, 0, 0, 7]))
        assert len(raw) == 24
        assert parseClientAddress(raw) == "10.0.0.7"

    def test_parse_short_struct_raises(self) -> None:
        with pytest.raises(ValueError, match="too short"):
            parseClientAddress(b"\x02\x00\x00\x00")


class TestCleanString:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, None),
            ("", None),
            ("  \x00\x00", None),
            ("alice\x00garbage", "alice"),
            (" bob \t", "bob"),
            ("Console", "Console"),
        ],
    )
    def test_clean(self, value: str | None, expected: str | None) -> None:
        assert cleanString(value) == expected


class TestParseWtsInfo:
    def test_ac03_times_and_strings(self) -> None:
        raw = packWtsInfo(
            4,
            2,
            winStationName="RDP-Tcp#3",
            domain="CORP",
            userName="alice",
            connectTime=0,
            disconnectTime=toFiletime(DISCONNECT_UTC),
            logonTime=toFiletime(LOGON_UTC),
        )
        assert len(raw) == WTSINFOW_SIZE == 216
        info = parseWtsInfo(raw)
        assert (info.state, info.sessionId) == (4, 2)
        assert (info.winStationName, info.domain, info.userName) == ("RDP-Tcp#3", "CORP", "alice")
        assert info.connectTime is None
        assert info.logonTime == datetime(2026, 10, 21, 0, 15, 30, tzinfo=UTC)
        assert info.disconnectTime == datetime(2026, 10, 21, 1, 2, 3, tzinfo=UTC)
        for value in (info.logonTime, info.disconnectTime):
            assert value is not None and value.tzinfo is not None and value.microsecond == 0
        assert info.lastInputTime is None and info.currentTime is None

    def test_empty_strings_are_none(self) -> None:
        info = parseWtsInfo(packWtsInfo(6, 65536))
        assert (info.winStationName, info.domain, info.userName) == (None, None, None)

    def test_non_ascii_names(self) -> None:
        info = parseWtsInfo(packWtsInfo(0, 1, userName="山田", domain="ドメイン"))
        assert (info.userName, info.domain) == ("山田", "ドメイン")

    def test_trailing_bytes_are_ignored(self) -> None:
        info = parseWtsInfo(packWtsInfo(0, 3, userName="bob") + b"\xff" * 8)
        assert info.userName == "bob"

    def test_short_buffer_raises(self) -> None:
        with pytest.raises(ValueError, match="too short"):
            parseWtsInfo(bytes(215))


def test_local_conversion_uses_host_offset() -> None:
    """The result is expressed in host-local time, not in the source's offset."""
    source = datetime(2026, 10, 21, 9, 0, 0, tzinfo=timezone(timedelta(hours=-5)))
    result = filetimeToDatetime(toFiletime(source))
    assert result == source
    assert result is not None and result.utcoffset() == localOffset(source)
