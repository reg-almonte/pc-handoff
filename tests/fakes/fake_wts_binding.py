"""In-memory ``WtsBinding`` with canned raw results, plus raw-struct packing helpers."""

import struct
from datetime import datetime

from pc_handoff.detection.wts_decode import (
    CLIENT_ADDRESS_FORMAT,
    FILETIME_EPOCH_OFFSET,
    UNIX_EPOCH,
    WTS_CLIENT_ADDRESS,
    WTS_CLIENT_NAME,
    WTS_CLIENT_PROTOCOL_TYPE,
    WTS_DOMAIN_NAME,
    WTS_SESSION_INFO,
    WTS_USER_NAME,
    WTSINFOW_FORMAT,
)

Response = bytes | str | int | BaseException


def winError(code: int, message: str = "fake WTS failure") -> OSError:
    """An ``OSError`` carrying ``winerror`` on any OS (POSIX ignores the constructor arg)."""
    error = OSError(code, message)
    error.winerror = code
    return error


def toFiletime(dt: datetime) -> int:
    """Convert an aware datetime to a FILETIME integer (100 ns units since 1601 UTC)."""
    delta = dt - UNIX_EPOCH
    micros = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    return FILETIME_EPOCH_OFFSET + micros * 10


def wchars(value: str, length: int) -> bytes:
    """Encode ``value`` into a NUL-padded ``WCHAR[length]`` (UTF-16-LE) array."""
    encoded = value.encode("utf-16-le")[: (length - 1) * 2]
    return encoded.ljust(length * 2, b"\x00")


def packWtsInfo(
    state: int,
    sessionId: int,
    winStationName: str = "",
    domain: str = "",
    userName: str = "",
    connectTime: int = 0,
    disconnectTime: int = 0,
    lastInputTime: int = 0,
    logonTime: int = 0,
    currentTime: int = 0,
) -> bytes:
    """Pack a 216-byte ``WTSINFOW`` buffer; times are FILETIME integers (0 = unavailable)."""
    return struct.pack(
        WTSINFOW_FORMAT,
        state,
        sessionId,
        0,
        0,
        0,
        0,
        0,
        0,
        wchars(winStationName, 32),
        wchars(domain, 17),
        wchars(userName, 21),
        connectTime,
        disconnectTime,
        lastInputTime,
        logonTime,
        currentTime,
    )


def packClientAddress(family: int, address: bytes) -> bytes:
    """Pack a 24-byte ``WTS_CLIENT_ADDRESS``; ``address`` is padded to 20 bytes."""
    return struct.pack(CLIENT_ADDRESS_FORMAT, family, address.ljust(20, b"\x00"))


class FakeWtsBinding:
    """``WtsBinding`` whose results are configured per session and per info class.

    A configured response that is an exception instance is raised; an info class that was
    never configured raises ``LookupError`` so a test setup mistake is not mistaken for an
    OS failure.
    """

    def __init__(self) -> None:
        self.entries: list[tuple[int, str, int]] = []
        self.responses: dict[tuple[int, int], Response] = {}
        self.enumerateError: BaseException | None = None
        self.calls: list[tuple[str, int, int]] = []

    def failEnumerate(self, error: BaseException) -> None:
        self.enumerateError = error

    def setResponse(self, sessionId: int, infoClass: int, value: Response) -> None:
        self.responses[(sessionId, infoClass)] = value

    def addSession(
        self,
        sessionId: int,
        stationName: str,
        state: int,
        *,
        userName: str = "",
        domain: str = "",
        clientName: str = "",
        clientAddress: bytes | None = None,
        protocolType: int = 2,
        connectTime: int = 0,
        disconnectTime: int = 0,
        logonTime: int = 0,
        infoState: int | None = None,
    ) -> None:
        """Add an enumeration entry and successful responses for every info class.

        ``clientAddress`` is a packed ``WTS_CLIENT_ADDRESS`` (default: family 0, unspecified).
        ``infoState`` overrides the state inside ``WTSINFOW`` (default: ``state``).
        """
        self.entries.append((sessionId, stationName, state))
        self.setResponse(
            sessionId,
            WTS_SESSION_INFO,
            packWtsInfo(
                state if infoState is None else infoState,
                sessionId,
                winStationName=stationName,
                domain=domain,
                userName=userName,
                connectTime=connectTime,
                disconnectTime=disconnectTime,
                logonTime=logonTime,
            ),
        )
        self.setResponse(sessionId, WTS_USER_NAME, userName)
        self.setResponse(sessionId, WTS_DOMAIN_NAME, domain)
        self.setResponse(sessionId, WTS_CLIENT_NAME, clientName)
        self.setResponse(
            sessionId,
            WTS_CLIENT_ADDRESS,
            clientAddress if clientAddress is not None else packClientAddress(0, b""),
        )
        self.setResponse(sessionId, WTS_CLIENT_PROTOCOL_TYPE, protocolType)

    def enumerate(self) -> list[tuple[int, str, int]]:
        self.calls.append(("enumerate", -1, -1))
        if self.enumerateError is not None:
            raise self.enumerateError
        return list(self.entries)

    def respond(self, kind: str, sessionId: int, infoClass: int) -> Response:
        self.calls.append((kind, sessionId, infoClass))
        try:
            value = self.responses[(sessionId, infoClass)]
        except KeyError:
            raise LookupError(
                f"no fake response for session {sessionId} class {infoClass}"
            ) from None
        if isinstance(value, BaseException):
            raise value
        return value

    def queryBytes(self, sessionId: int, infoClass: int) -> bytes:
        value = self.respond("queryBytes", sessionId, infoClass)
        assert isinstance(value, bytes)
        return value

    def queryString(self, sessionId: int, infoClass: int) -> str:
        value = self.respond("queryString", sessionId, infoClass)
        assert isinstance(value, str)
        return value

    def queryUShort(self, sessionId: int, infoClass: int) -> int:
        value = self.respond("queryUShort", sessionId, infoClass)
        assert isinstance(value, int)
        return value
