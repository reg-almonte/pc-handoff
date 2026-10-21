"""Scriptable ``SessionProvider`` for tests, plus the ``makeSession`` builder."""

import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

from pc_handoff.detection.provider import (
    ConnectionType,
    OsConnectState,
    OsSession,
    SessionEnumerationError,
)

DEFAULT_TZ = timezone(timedelta(hours=9))
DEFAULT_LOGON_TIME = datetime(2026, 10, 21, 8, 0, 0, tzinfo=DEFAULT_TZ)

RAW_STATE_NAMES = {
    OsConnectState.ACTIVE: "WTSActive",
    OsConnectState.DISCONNECTED: "WTSDisconnected",
    OsConnectState.TRANSITIONAL: "WTSConnected",
    OsConnectState.IGNORED: "WTSListen",
}


def makeSession(
    sessionId: int,
    user: str | None,
    state: str | OsConnectState = "Active",
    connectionType: str | ConnectionType = "RDP",
    **overrides: Any,
) -> OsSession:
    """Build an ``OsSession`` with plausible defaults; any field can be overridden by keyword.

    ``state`` and ``connectionType`` accept the enum or its value (``"Active"``, ``"Console"``).
    RDP defaults to station ``RDP-Tcp#<id>``, protocol 2 and a client name and address;
    Console defaults to station ``Console``, protocol 0 and no client information.
    """
    rawState = OsConnectState(state)
    connType = ConnectionType(connectionType)
    isConsole = connType is ConnectionType.CONSOLE
    fields: dict[str, Any] = {
        "sessionId": sessionId,
        "winStationName": "Console" if isConsole else f"RDP-Tcp#{sessionId}",
        "userName": user,
        "domain": "CORP" if user else None,
        "rawState": rawState,
        "rawStateName": RAW_STATE_NAMES[rawState],
        "connectionType": connType,
        "protocolType": 0 if isConsole else 2,
        "clientName": None if isConsole else "CLIENT-PC",
        "clientAddress": None if isConsole else "192.168.1.20",
        "logonTime": DEFAULT_LOGON_TIME,
        "connectTime": DEFAULT_LOGON_TIME,
        "disconnectTime": None,
    }
    unknown = set(overrides) - set(fields)
    if unknown:
        raise TypeError(f"unknown OsSession fields: {sorted(unknown)}")
    fields.update(overrides)
    return OsSession(**fields)


class FakeSessionProvider:
    """``SessionProvider`` returning scripted snapshots, failures and hung calls.

    - ``setSessions(sessions)``: the snapshot returned by later calls.
    - ``failNext(kind, times=1)``: the next ``times`` calls raise
      ``SessionEnumerationError(kind)``; ``kind`` may also be an exception instance to raise.
    - ``blockNext(event)``: the next call blocks until ``event`` is set, then proceeds
      (returning the sessions current at that moment, or a scripted failure).
    - ``callCount``: number of ``enumerateSessions`` calls started.
    """

    def __init__(self, sessions: list[OsSession] | None = None) -> None:
        self.lock = threading.Lock()
        self.sessions: list[OsSession] = list(sessions or [])
        self.failures: deque[str | BaseException] = deque()
        self.blockEvents: deque[threading.Event] = deque()
        self.callCount = 0

    def setSessions(self, sessions: list[OsSession]) -> None:
        with self.lock:
            self.sessions = list(sessions)

    def failNext(self, kind: str | BaseException, times: int = 1) -> None:
        with self.lock:
            self.failures.extend([kind] * times)

    def blockNext(self, event: threading.Event) -> None:
        with self.lock:
            self.blockEvents.append(event)

    def enumerateSessions(self) -> list[OsSession]:
        with self.lock:
            self.callCount += 1
            event = self.blockEvents.popleft() if self.blockEvents else None
            failure = self.failures.popleft() if self.failures else None
        if event is not None:
            event.wait()
        if isinstance(failure, BaseException):
            raise failure
        if failure is not None:
            raise SessionEnumerationError(failure, f"fake {failure}")
        with self.lock:
            return list(self.sessions)
