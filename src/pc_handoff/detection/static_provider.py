"""``SessionProvider`` reading the sessions from a JSON file: a test hook (TASK-009, MINOR-4).

The launcher selects it only when both ``PC_HANDOFF_ALLOW_TEST_HOOKS=1`` and
``PC_HANDOFF_TEST_SESSIONS_FILE=<path>`` are set, and logs a WARNING when it does. It lives in
the package, not under ``tests/``, so a ``python -m pc_handoff`` subprocess can import it.

The file is re-read on every enumeration, so a test can change the sessions while the app
runs. It holds either a JSON array of session objects or ``{"fail": "<kind>"}``, which makes
the enumeration fail with that ``SessionEnumerationError`` kind. A session object has:

- ``sessionId`` (integer) and ``state`` (``Active``, ``Disconnected``, ``Transitional`` or
  ``Ignored``), both required;
- optional ``userName``, ``domain``, ``winStationName``, ``clientName``, ``clientAddress``
  (string or null), ``connectionType`` (``RDP`` by default, or ``Console``),
  ``protocolType`` (integer; 0 for Console and 2 for RDP by default), ``rawStateName``, and
  ``logonTime``, ``connectTime``, ``disconnectTime`` (ISO 8601 with offset, or null; truncated
  to whole seconds like the WTS provider).

Any other key, a wrong type, or an unreadable file fails the enumeration with kind
``"error"``, so a broken fixture is never mistaken for an empty PC.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from pc_handoff.clock import parseIso
from pc_handoff.detection.provider import (
    ConnectionType,
    OsConnectState,
    OsSession,
    SessionEnumerationError,
)

RAW_STATE_NAMES = {
    OsConnectState.ACTIVE: "WTSActive",
    OsConnectState.DISCONNECTED: "WTSDisconnected",
    OsConnectState.TRANSITIONAL: "WTSConnected",
    OsConnectState.IGNORED: "WTSListen",
}
TEXT_FIELDS = ("userName", "domain", "winStationName", "clientName", "clientAddress")
TIME_FIELDS = ("logonTime", "connectTime", "disconnectTime")
KNOWN_FIELDS = frozenset(
    {"sessionId", "state", "connectionType", "protocolType", "rawStateName"}
    | set(TEXT_FIELDS)
    | set(TIME_FIELDS)
)


class InvalidSessionsFile(ValueError):
    """The sessions file does not describe a snapshot."""


class StaticSessionProvider:
    """Returns the ``OsSession`` list described in ``sessionsFile`` at each call."""

    def __init__(self, sessionsFile: str | Path) -> None:
        self.sessionsFile = Path(sessionsFile)

    def enumerateSessions(self) -> list[OsSession]:
        try:
            doc = json.loads(self.sessionsFile.read_bytes().decode("utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise self.invalid(f"cannot be read ({exc})") from exc
        if isinstance(doc, dict) and set(doc) == {"fail"}:
            try:
                raise SessionEnumerationError(doc["fail"], "test sessions file says fail")
            except ValueError as exc:  # an unknown kind
                raise self.invalid(str(exc)) from exc
        if not isinstance(doc, list):
            raise self.invalid('must be a JSON array of sessions or {"fail": "<kind>"}')
        try:
            return [parseSession(index, entry) for index, entry in enumerate(doc)]
        except InvalidSessionsFile as exc:
            raise self.invalid(str(exc)) from exc

    def invalid(self, reason: str) -> SessionEnumerationError:
        return SessionEnumerationError("error", f"test sessions file {self.sessionsFile}: {reason}")


def parseSession(index: int, entry: Any) -> OsSession:
    where = f"session {index}"
    if not isinstance(entry, dict):
        raise InvalidSessionsFile(f"{where}: must be a JSON object")
    unknown = sorted(set(entry) - KNOWN_FIELDS)
    if unknown:
        raise InvalidSessionsFile(f"{where}: unknown field(s) {unknown}")
    sessionId = entry.get("sessionId")
    if not isStrictInt(sessionId):
        raise InvalidSessionsFile(f"{where}: sessionId must be an integer")
    rawState = enumValue(OsConnectState, entry.get("state"), f"{where}: state")
    connectionType = enumValue(
        ConnectionType, entry.get("connectionType", "RDP"), f"{where}: connectionType"
    )
    isConsole = connectionType is ConnectionType.CONSOLE
    protocolType = entry.get("protocolType", 0 if isConsole else 2)
    if not isStrictInt(protocolType):
        raise InvalidSessionsFile(f"{where}: protocolType must be an integer")
    text = {name: optionalText(entry, name, where) for name in TEXT_FIELDS}
    times = {name: optionalTime(entry, name, where) for name in TIME_FIELDS}
    rawStateName = entry.get("rawStateName", RAW_STATE_NAMES[rawState])
    if not isinstance(rawStateName, str):
        raise InvalidSessionsFile(f"{where}: rawStateName must be a string")
    return OsSession(
        sessionId=sessionId,
        rawState=rawState,
        rawStateName=rawStateName,
        connectionType=connectionType,
        protocolType=protocolType,
        **text,
        **times,
    )


def isStrictInt(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def enumValue(enumType: Any, value: Any, where: str) -> Any:
    try:
        return enumType(value)
    except ValueError as exc:
        names = ", ".join(member.value for member in enumType)
        raise InvalidSessionsFile(f"{where} must be one of {names}, got {value!r}") from exc


def optionalText(entry: dict[str, Any], name: str, where: str) -> str | None:
    value = entry.get(name)
    if value is not None and not isinstance(value, str):
        raise InvalidSessionsFile(f"{where}: {name} must be a string or null")
    return value


def optionalTime(entry: dict[str, Any], name: str, where: str) -> datetime | None:
    value = entry.get(name)
    if value is None:
        return None
    try:
        if not isinstance(value, str):
            raise ValueError("not a string")
        return parseIso(value).replace(microsecond=0)
    except ValueError as exc:
        raise InvalidSessionsFile(
            f"{where}: {name} must be ISO 8601 with an offset, or null ({exc})"
        ) from exc
