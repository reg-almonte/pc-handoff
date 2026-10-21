"""``SessionProvider`` backed by the Windows WTS API through an injected ``WtsBinding``.

The binding performs the raw calls; all decoding is in ``wts_decode``. A failed enumeration
fails the whole snapshot; a failed per-session query nulls only the affected fields, except
that a failed user-name or domain query falls back to the untruncated WTSINFOW copy
(ADR-001, REQ AC-9, AC-10).
"""

import logging
import sys
from collections.abc import Callable
from typing import Protocol, TypeVar

from pc_handoff.detection.provider import OsSession, SessionEnumerationError
from pc_handoff.detection.wts_decode import (
    ERROR_ACCESS_DENIED,
    UNKNOWN_STATE_PREFIX,
    WTS_CLIENT_ADDRESS,
    WTS_CLIENT_NAME,
    WTS_CLIENT_PROTOCOL_TYPE,
    WTS_DOMAIN_NAME,
    WTS_SESSION_INFO,
    WTS_USER_NAME,
    WTSINFOW_USER_NAME_MAX_CHARS,
    cleanString,
    mapConnectionType,
    mapConnectState,
    parseClientAddress,
    parseWtsInfo,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


class WtsBinding(Protocol):
    """Raw WTS calls returning Python primitives. Failures raise ``OSError`` with winerror."""

    def enumerate(self) -> list[tuple[int, str, int]]:
        """Return ``(sessionId, winStationName, state)`` for every session."""

    def queryBytes(self, sessionId: int, infoClass: int) -> bytes:
        """Return the raw buffer of ``WTSQuerySessionInformationW``."""

    def queryString(self, sessionId: int, infoClass: int) -> str:
        """Return a string info class."""

    def queryUShort(self, sessionId: int, infoClass: int) -> int:
        """Return a USHORT info class."""


class WtsSessionProvider:
    """Enumerate sessions through a ``WtsBinding`` and decode them into ``OsSession``."""

    def __init__(self, binding: WtsBinding) -> None:
        self.binding = binding

    def enumerateSessions(self) -> list[OsSession]:
        try:
            entries = self.binding.enumerate()
        except OSError as exc:
            winerror = getattr(exc, "winerror", None)
            kind = "access_denied" if winerror == ERROR_ACCESS_DENIED else "error"
            raise SessionEnumerationError(
                kind, f"WTSEnumerateSessionsW failed: {exc}", winerror=winerror
            ) from exc
        return [self.buildSession(*entry) for entry in entries]

    def buildSession(self, sessionId: int, stationName: str | None, enumState: int) -> OsSession:
        binding = self.binding
        info = self.query(
            sessionId,
            WTS_SESSION_INFO,
            lambda: parseWtsInfo(binding.queryBytes(sessionId, WTS_SESSION_INFO)),
        )
        # Names prefer the full-length queries. If one fails, the WTSINFOW copy is used when it
        # cannot be truncated, so a one-off failure does not end the session identity
        # (ADR-007 R1/R2; Critic TASK-002 S1). Without WTSINFOW there is no fallback.
        infoUserName = info.userName if info is not None else None
        if infoUserName is not None and len(infoUserName) >= WTSINFOW_USER_NAME_MAX_CHARS:
            infoUserName = None  # may be truncated
        userName = self.queryName(sessionId, WTS_USER_NAME, infoUserName)
        domain = self.queryName(
            sessionId, WTS_DOMAIN_NAME, info.domain if info is not None else None
        )
        clientName = self.queryText(sessionId, WTS_CLIENT_NAME)
        clientAddress = self.query(
            sessionId,
            WTS_CLIENT_ADDRESS,
            lambda: parseClientAddress(binding.queryBytes(sessionId, WTS_CLIENT_ADDRESS)),
        )
        protocolType = self.query(
            sessionId,
            WTS_CLIENT_PROTOCOL_TYPE,
            lambda: binding.queryUShort(sessionId, WTS_CLIENT_PROTOCOL_TYPE),
        )

        stateValue = info.state if info is not None else enumState
        state, stateName = mapConnectState(stateValue)
        if stateName.startswith(UNKNOWN_STATE_PREFIX):
            logger.debug(
                "unknown WTS connect state mapped to Transitional",
                extra={"detail": {"sessionId": sessionId, "state": stateValue}},
            )
        winStationName = cleanString(stationName)
        return OsSession(
            sessionId=sessionId,
            winStationName=winStationName,
            userName=userName,
            domain=domain,
            rawState=state,
            rawStateName=stateName,
            connectionType=mapConnectionType(protocolType, winStationName),
            protocolType=protocolType,
            clientName=clientName,
            clientAddress=clientAddress,
            logonTime=info.logonTime if info is not None else None,
            connectTime=info.connectTime if info is not None else None,
            disconnectTime=info.disconnectTime if info is not None else None,
        )

    def queryText(self, sessionId: int, infoClass: int) -> str | None:
        return self.query(
            sessionId,
            infoClass,
            lambda: cleanString(self.binding.queryString(sessionId, infoClass)),
        )

    def queryName(self, sessionId: int, infoClass: int, fallback: str | None) -> str | None:
        """Query a name; only if the query fails, return ``fallback`` (None = no fallback).

        A query that succeeds with an empty name stays None and is never overridden.
        """
        try:
            return cleanString(self.binding.queryString(sessionId, infoClass))
        except (OSError, ValueError) as exc:
            self.logQueryFailure(sessionId, infoClass, exc)
            if fallback is not None:
                logger.debug(
                    "WTS name recovered from WTSINFOW",
                    extra={
                        "detail": {
                            "sessionId": sessionId,
                            "infoClass": infoClass,
                            "winerror": getattr(exc, "winerror", None),
                        }
                    },
                )
            return fallback

    def query(self, sessionId: int, infoClass: int, call: Callable[[], T]) -> T | None:
        """Run one per-session query; a failure yields None for that field only."""
        try:
            return call()
        except (OSError, ValueError) as exc:
            self.logQueryFailure(sessionId, infoClass, exc)
            return None

    @staticmethod
    def logQueryFailure(sessionId: int, infoClass: int, exc: Exception) -> None:
        logger.debug(
            "WTS session query failed",
            extra={
                "detail": {
                    "sessionId": sessionId,
                    "infoClass": infoClass,
                    "winerror": getattr(exc, "winerror", None),
                    "error": str(exc),
                }
            },
        )


def createDefaultProvider() -> WtsSessionProvider:
    """Return the real WTS provider. Raises ``RuntimeError`` when not running on Windows."""
    if sys.platform != "win32":
        raise RuntimeError("WTS provider requires Windows")
    from pc_handoff.detection.wts_binding import RealWtsBinding

    return WtsSessionProvider(RealWtsBinding())
