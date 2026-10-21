"""Pure decoders for raw WTS API results (ADR-001). OS-independent; no ctypes calls.

Layouts follow the Win32 headers (wtsapi32.h):

- ``WTSINFOW``: 8 DWORDs (State, SessionId and six byte/frame counters),
  ``WCHAR WinStationName[32]``, ``WCHAR Domain[17]``, ``WCHAR UserName[21]``, 4 bytes of
  padding to align the following ``LARGE_INTEGER`` fields, then ConnectTime, DisconnectTime,
  LastInputTime, LogonTime and CurrentTime. 216 bytes in total.
- ``WTS_CLIENT_ADDRESS``: ``DWORD AddressFamily`` followed by ``BYTE Address[20]``.

WCHAR is decoded explicitly as UTF-16-LE so the result does not depend on the platform's
``wchar_t`` size.
"""

import ipaddress
import struct
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pc_handoff.detection.provider import ConnectionType, OsConnectState

# WTS_INFO_CLASS values used by the provider.
WTS_USER_NAME = 5
WTS_DOMAIN_NAME = 7
WTS_CLIENT_NAME = 10
WTS_CLIENT_ADDRESS = 14
WTS_CLIENT_PROTOCOL_TYPE = 16
WTS_SESSION_INFO = 24

# Address families as reported in WTS_CLIENT_ADDRESS.
AF_INET = 2
AF_INET6 = 23

ERROR_ACCESS_DENIED = 5

# 100 ns intervals between 1601-01-01 and 1970-01-01 (UTC).
FILETIME_EPOCH_OFFSET = 116444736000000000
UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# Characters (excluding the terminator) that WTSINFOW.UserName (WCHAR[21]) can hold. A
# decoded user name this long may have been truncated (USERNAME_LENGTH = 20).
WTSINFOW_USER_NAME_MAX_CHARS = 20

WTSINFOW_FORMAT = "<8I64s34s42s4x5q"
WTSINFOW_SIZE = struct.calcsize(WTSINFOW_FORMAT)  # 216
CLIENT_ADDRESS_FORMAT = "<I20s"
CLIENT_ADDRESS_SIZE = struct.calcsize(CLIENT_ADDRESS_FORMAT)  # 24

WTS_STATE_NAMES: dict[int, str] = {
    0: "WTSActive",
    1: "WTSConnected",
    2: "WTSConnectQuery",
    3: "WTSShadow",
    4: "WTSDisconnected",
    5: "WTSIdle",
    6: "WTSListen",
    7: "WTSReset",
    8: "WTSDown",
    9: "WTSInit",
}
UNKNOWN_STATE_PREFIX = "unknown"

STATE_MAPPING: dict[int, OsConnectState] = {
    0: OsConnectState.ACTIVE,
    4: OsConnectState.DISCONNECTED,
    6: OsConnectState.IGNORED,
}


@dataclass(frozen=True)
class WtsInfo:
    """Decoded ``WTSINFOW``. Strings are cleaned (None when empty); times may be None."""

    state: int
    sessionId: int
    winStationName: str | None
    domain: str | None
    userName: str | None
    connectTime: datetime | None
    disconnectTime: datetime | None
    lastInputTime: datetime | None
    logonTime: datetime | None
    currentTime: datetime | None


def cleanString(s: str | None) -> str | None:
    """Cut at the first NUL, strip whitespace, and map an empty result to None."""
    if s is None:
        return None
    cleaned = s.split("\x00", 1)[0].strip()
    return cleaned or None


def decodeWchars(raw: bytes) -> str | None:
    """Decode a fixed-size WCHAR array (UTF-16-LE) and clean it."""
    return cleanString(raw.decode("utf-16-le", errors="replace"))


def filetimeToDatetime(value: int) -> datetime | None:
    """Convert a FILETIME (100 ns units since 1601-01-01 UTC) to host-local aware time.

    0 (and any non-positive value) means "not available" and maps to None, as does a value
    outside the range ``datetime`` can represent. The result is truncated to whole seconds.
    """
    if value <= 0:
        return None
    try:
        utc = UNIX_EPOCH + timedelta(microseconds=(value - FILETIME_EPOCH_OFFSET) // 10)
        return utc.astimezone().replace(microsecond=0)
    except (OverflowError, OSError, ValueError):
        return None


def decodeClientAddress(family: int, raw: bytes) -> str | None:
    """Decode ``WTS_CLIENT_ADDRESS.Address`` for the given address family.

    AF_INET (2) uses bytes [2..5], AF_INET6 (23) bytes [2..17]. Any other family, a buffer
    too short for the family, or an all-zero address gives None.
    """
    if family == AF_INET:
        addressBytes = raw[2:6]
        expectedLength = 4
    elif family == AF_INET6:
        addressBytes = raw[2:18]
        expectedLength = 16
    else:
        return None
    if len(addressBytes) != expectedLength or not any(addressBytes):
        return None
    return str(ipaddress.ip_address(addressBytes))


def parseClientAddress(raw: bytes) -> str | None:
    """Decode a whole ``WTS_CLIENT_ADDRESS`` buffer (family + address bytes).

    Raises ``ValueError`` if the buffer is shorter than the structure.
    """
    if len(raw) < CLIENT_ADDRESS_SIZE:
        raise ValueError(f"WTS_CLIENT_ADDRESS buffer too short: {len(raw)} bytes")
    family, address = struct.unpack_from(CLIENT_ADDRESS_FORMAT, raw)
    return decodeClientAddress(family, address)


def mapConnectState(value: int) -> tuple[OsConnectState, str]:
    """Map a ``WTS_CONNECTSTATE_CLASS`` value per the ADR-001 table.

    Returns the mapped state and the raw state name for logging. Unknown values map to
    TRANSITIONAL with the name ``"unknown(<value>)"``.
    """
    name = WTS_STATE_NAMES.get(value, f"{UNKNOWN_STATE_PREFIX}({value})")
    return STATE_MAPPING.get(value, OsConnectState.TRANSITIONAL), name


def mapConnectionType(protocolType: int | None, winStationName: str | None) -> ConnectionType:
    """Derive the connection type: protocol 0 is Console, any other value RDP.

    Without a protocol type, a WinStation name starting with ``Console`` means Console and
    anything else (including no name) RDP.
    """
    if protocolType is not None:
        return ConnectionType.CONSOLE if protocolType == 0 else ConnectionType.RDP
    if winStationName is not None and winStationName.startswith("Console"):
        return ConnectionType.CONSOLE
    return ConnectionType.RDP


def parseWtsInfo(raw: bytes) -> WtsInfo:
    """Unpack a ``WTSINFOW`` buffer. Raises ``ValueError`` if it is shorter than 216 bytes."""
    if len(raw) < WTSINFOW_SIZE:
        raise ValueError(f"WTSINFOW buffer too short: {len(raw)} bytes")
    fields = struct.unpack_from(WTSINFOW_FORMAT, raw)
    state, sessionId = fields[0], fields[1]
    stationRaw, domainRaw, userRaw = fields[8], fields[9], fields[10]
    connect, disconnect, lastInput, logon, current = fields[11:16]
    return WtsInfo(
        state=state,
        sessionId=sessionId,
        winStationName=decodeWchars(stationRaw),
        domain=decodeWchars(domainRaw),
        userName=decodeWchars(userRaw),
        connectTime=filetimeToDatetime(connect),
        disconnectTime=filetimeToDatetime(disconnect),
        lastInputTime=filetimeToDatetime(lastInput),
        logonTime=filetimeToDatetime(logon),
        currentTime=filetimeToDatetime(current),
    )
