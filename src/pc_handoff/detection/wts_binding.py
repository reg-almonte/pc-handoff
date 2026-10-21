"""Thin ctypes binding to ``wtsapi32.dll`` (ADR-001). Windows only.

This is the only module that touches the Windows DLLs. It is imported lazily by
``createDefaultProvider`` and is omitted from coverage (ADR-006); it is exercised by
``tests/windows/test_wts_binding_smoke.py``. It contains call plumbing only: every result is
returned as Python primitives and decoded in ``wts_decode``. Every buffer the API allocates
is released with ``WTSFreeMemory`` in a ``finally`` block. Nothing here changes session state.
"""

import ctypes
from ctypes import POINTER, Structure, byref, c_int, c_uint32, c_void_p, c_wchar_p

WTS_CURRENT_SERVER_HANDLE = 0


class WTS_SESSION_INFOW(Structure):  # name and fields mirror the Win32 declaration
    _fields_ = [
        ("SessionId", c_uint32),
        ("pWinStationName", c_wchar_p),
        ("State", c_int),
    ]


class RealWtsBinding:
    """``WtsBinding`` over the real WTS API of the local server."""

    def __init__(self) -> None:
        dll = ctypes.WinDLL("wtsapi32", use_last_error=True)

        self.enumerateFn = dll.WTSEnumerateSessionsW
        self.enumerateFn.argtypes = [
            c_void_p,  # hServer
            c_uint32,  # Reserved
            c_uint32,  # Version
            POINTER(POINTER(WTS_SESSION_INFOW)),  # ppSessionInfo
            POINTER(c_uint32),  # pCount
        ]
        self.enumerateFn.restype = c_int  # BOOL

        self.queryFn = dll.WTSQuerySessionInformationW
        self.queryFn.argtypes = [
            c_void_p,  # hServer
            c_uint32,  # SessionId
            c_int,  # WTSInfoClass
            POINTER(c_void_p),  # ppBuffer
            POINTER(c_uint32),  # pBytesReturned
        ]
        self.queryFn.restype = c_int  # BOOL

        self.freeFn = dll.WTSFreeMemory
        self.freeFn.argtypes = [c_void_p]
        self.freeFn.restype = None

    @staticmethod
    def lastError() -> OSError:
        return ctypes.WinError(ctypes.get_last_error())

    def enumerate(self) -> list[tuple[int, str, int]]:
        sessions = POINTER(WTS_SESSION_INFOW)()
        count = c_uint32(0)
        try:
            if not self.enumerateFn(WTS_CURRENT_SERVER_HANDLE, 0, 1, byref(sessions), byref(count)):
                raise self.lastError()
            return [
                (sessions[i].SessionId, sessions[i].pWinStationName or "", sessions[i].State)
                for i in range(count.value)
            ]
        finally:
            if sessions:
                self.freeFn(ctypes.cast(sessions, c_void_p))

    def queryBytes(self, sessionId: int, infoClass: int) -> bytes:
        buffer = c_void_p()
        size = c_uint32(0)
        try:
            ok = self.queryFn(
                WTS_CURRENT_SERVER_HANDLE, sessionId, infoClass, byref(buffer), byref(size)
            )
            if not ok:
                raise self.lastError()
            return ctypes.string_at(buffer.value, size.value) if buffer.value else b""
        finally:
            if buffer.value:
                self.freeFn(buffer)

    def queryString(self, sessionId: int, infoClass: int) -> str:
        raw = self.queryBytes(sessionId, infoClass)
        return raw.decode("utf-16-le", errors="replace").split("\x00", 1)[0]

    def queryUShort(self, sessionId: int, infoClass: int) -> int:
        raw = self.queryBytes(sessionId, infoClass)
        if len(raw) < 2:
            raise ValueError(f"USHORT info class {infoClass} returned {len(raw)} bytes")
        return int.from_bytes(raw[:2], "little")
