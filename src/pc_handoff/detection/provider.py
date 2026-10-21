"""Session provider abstraction: the OS session model and the ``SessionProvider`` protocol.

A provider returns a faithful, complete snapshot of the OS sessions or raises
``SessionEnumerationError`` (ADR-001). Filtering into user sessions happens in the tracker
(TASK-004), not here.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable

ENUMERATION_ERROR_KINDS = ("error", "access_denied", "timeout")


class SessionEnumerationError(Exception):
    """The session enumeration failed as a whole; the poll fails (REQ AC-11).

    ``kind`` is ``"error"``, ``"access_denied"`` or ``"timeout"``. ``winerror`` carries the
    Windows error code when one is known.
    """

    def __init__(self, kind: str, message: str = "", winerror: int | None = None) -> None:
        if kind not in ENUMERATION_ERROR_KINDS:
            raise ValueError(f"unknown enumeration error kind: {kind!r}")
        self.kind = kind
        self.winerror = winerror
        self.message = message or kind
        super().__init__(self.message)

    def __str__(self) -> str:
        return f"{self.kind}: {self.message}"


class OsConnectState(Enum):
    """Connection state class after mapping the raw WTS value (ADR-001 table)."""

    ACTIVE = "Active"
    DISCONNECTED = "Disconnected"
    TRANSITIONAL = "Transitional"
    IGNORED = "Ignored"


class ConnectionType(Enum):
    """How a session is attached: over Remote Desktop or at the PC's own console."""

    RDP = "RDP"
    CONSOLE = "Console"


@dataclass(frozen=True)
class OsSession:
    """One OS session as reported by the provider.

    ``rawStateName`` is the WTS state name (e.g. ``"WTSShadow"``, or ``"unknown(42)"``), kept
    for logging. Times are offset-aware host-local datetimes truncated to whole seconds, or
    None when the OS does not supply them (REQ AC-10). Client fields are None when the OS does
    not supply them (REQ AC-9).
    """

    sessionId: int
    winStationName: str | None
    userName: str | None
    domain: str | None
    rawState: OsConnectState
    rawStateName: str
    connectionType: ConnectionType
    protocolType: int | None
    clientName: str | None
    clientAddress: str | None
    logonTime: datetime | None
    connectTime: datetime | None
    disconnectTime: datetime | None


@runtime_checkable
class SessionProvider(Protocol):
    """Source of OS session snapshots."""

    def enumerateSessions(self) -> list[OsSession]:
        """Return every OS session, or raise ``SessionEnumerationError``."""
