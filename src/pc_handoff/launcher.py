"""Application launcher: ``python -m pc_handoff`` (ADR-002, ADR-004, ADR-006; TASK-009).

Startup order: Python version check -> arguments -> single-instance lock -> ``app.log`` ->
``config.json`` (read once) -> dashboard port check -> the other data files, loaded once ->
runtime (published pre-poll snapshot, registered) -> poller thread -> Streamlit server on
``0.0.0.0:dashboardPort`` (blocks on the main thread). On the way out the poller is stopped
and the lock released, whatever happened; if the poller does not stop in time the lock is
left to the OS, which releases it when the process exits (Critic TASK-009 S3).

Exit codes: 0 clean stop; 1 startup or server failure; 3 another instance holds the lock;
4 the lock (or the data directory) is unavailable; 5 Python older than 3.11; 6 the dashboard
port is in use (user decision 2026-10-22; checked before status.json and activity_log.json
are touched and before any poll; skipped by --no-server).

Only the standard library is imported at module level, and the module parses on old
interpreters, so the version check can report exit 5 where the rest of the package (which
needs 3.11) could not even be imported. Application modules are imported inside functions.
"""

from __future__ import annotations

import argparse
import errno
import logging
import os
import signal
import socket
import sys
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pc_handoff.clock import Clock
    from pc_handoff.detection.provider import SessionProvider
    from pc_handoff.persistence.config_store import ConfigLoadResult
    from pc_handoff.persistence.instance_lock import InstanceLock
    from pc_handoff.runtime.app_runtime import AppRuntime

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_PYTHON_TOO_OLD = 5
EXIT_PORT_IN_USE = 6
MIN_PYTHON = (3, 11)

DATA_DIR_ENV = "PC_HANDOFF_DATA_DIR"
TEST_HOOKS_ENV = "PC_HANDOFF_ALLOW_TEST_HOOKS"
TEST_SESSIONS_FILE_ENV = "PC_HANDOFF_TEST_SESSIONS_FILE"
# <external>/data for a source or editable install: launcher.py is <external>/src/pc_handoff/.
DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
DASHBOARD_SCRIPT = Path(__file__).resolve().parent / "ui" / "dashboard.py"
SHUTDOWN_SIGNAL_NAMES = ("SIGINT", "SIGTERM", "SIGBREAK")
SHUTDOWN_WAKE_SECONDS = 0.5  # a bounded wait lets signal handlers run promptly on Windows
# Windows: a plain wildcard bind succeeds next to a listener on one specific address (for
# example 127.0.0.1:port); SO_EXCLUSIVEADDRUSE makes the probe fail whenever any socket holds
# the port. Elsewhere a plain bind (no SO_REUSEADDR) already fails for a taken port.
PORT_PROBE_OPTIONS = (
    ((socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1),) if sys.platform == "win32" else ()
)
# Errors meaning "this host cannot make the IPv6 dual-mode probe", so the IPv4 probe is used
# instead. Windows reports socket errors as WSA codes: a rejected bind or option is WSAEINVAL
# (10022), not errno.EINVAL (22), so both spellings are listed. "In use" errors
# (EADDRINUSE / WSAEADDRINUSE 10048, and WSAEACCES 10013 from an exclusive-bind conflict)
# are deliberately absent: they mean the port is taken.
IPV6_UNAVAILABLE_ERRNOS = frozenset(
    code
    for code in (
        getattr(errno, name, None)
        for name in (
            "EAFNOSUPPORT",
            "EADDRNOTAVAIL",
            "EINVAL",
            "ENOPROTOOPT",
            "EPROTONOSUPPORT",
            "WSAEAFNOSUPPORT",
            "WSAEADDRNOTAVAIL",
            "WSAEINVAL",
            "WSAENOPROTOOPT",
            "WSAEPROTONOSUPPORT",
        )
    )
    if code is not None
)

LOGGER_NAME = "pc_handoff"


def pythonVersion() -> tuple[int, ...]:
    return tuple(sys.version_info[:3])


def parseArgs(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m pc_handoff",
        description="PC Handoff: shared PC status monitoring and dashboard.",
    )
    parser.add_argument(
        "--data-dir",
        dest="dataDir",
        default=None,
        help=f"data directory (default: ${DATA_DIR_ENV}, else {DEFAULT_DATA_DIR})",
    )
    parser.add_argument(
        "--check-lock-only",
        dest="checkLockOnly",
        action="store_true",
        help="test only: exit 0 if the instance lock is free, 3 if held, 4 if unavailable",
    )
    parser.add_argument(
        "--no-server",
        dest="noServer",
        action="store_true",
        help="test only: run the poller without the dashboard server until stopped",
    )
    return parser.parse_args(argv)


def resolveDataDir(option: str | None) -> Path:
    """``--data-dir``, else ``$PC_HANDOFF_DATA_DIR``, else ``<external>/data``."""
    chosen = option or os.environ.get(DATA_DIR_ENV) or DEFAULT_DATA_DIR
    return Path(chosen)


def main(argv: Sequence[str] | None = None, *, shutdownEvent: threading.Event | None = None) -> int:
    """Run the application and return its exit code.

    ``shutdownEvent`` (tests) stops a ``--no-server`` run when set; signals do the same when
    running on the main thread.
    """
    found = pythonVersion()
    if found < MIN_PYTHON:
        required = ".".join(str(part) for part in MIN_PYTHON)
        actual = ".".join(str(part) for part in found)
        writeStderr(f"PC Handoff requires Python {required} or later; this is Python {actual}")
        return EXIT_PYTHON_TOO_OLD
    args = parseArgs(argv)
    dataDir = resolveDataDir(args.dataDir)

    from pc_handoff.clock import SystemClock
    from pc_handoff.persistence.instance_lock import (
        InstanceLock,
        LockHeldError,
        LockUnavailableError,
        exitCodeFor,
        reportSecondInstance,
        runSecondInstanceGuard,
    )

    clock = SystemClock()
    if args.checkLockOnly:
        return runSecondInstanceGuard(dataDir, clock)
    lock = InstanceLock(dataDir, clock)
    try:
        lock.acquire()
    except (LockHeldError, LockUnavailableError) as error:
        reportSecondInstance(dataDir, error, clock)
        return exitCodeFor(error)
    return runWithLock(dataDir, clock, lock, noServer=args.noServer, shutdownEvent=shutdownEvent)


def runWithLock(
    dataDir: Path,
    clock: Clock,
    lock: InstanceLock,
    *,
    noServer: bool,
    shutdownEvent: threading.Event | None,
) -> int:
    """Everything after the lock: logging, load, runtime, port check, poller, server; then
    shutdown. ``lock`` is released on the way out unless the poller is still running."""
    from pc_handoff import server_launcher
    from pc_handoff.app_logging import configureAppLogging
    from pc_handoff.persistence.config_store import loadConfig
    from pc_handoff.persistence.paths import DataPaths
    from pc_handoff.runtime.poller import Poller
    from pc_handoff.runtime.registry import clearRuntime, registerRuntime

    logger: logging.Logger | None = None
    poller: Poller | None = None
    try:
        try:
            logger = configureAppLogging(dataDir)
            logger.info(
                "PC Handoff starting",
                extra={"detail": {"dataDir": str(Path(dataDir).resolve()), "pid": os.getpid()}},
            )
            # config.json alone first: the port check must run before status.json and
            # activity_log.json are read, backed up or created (S8), and the loaded config is
            # handed on, so config.json is still read once (AC-14). A corrupt config.json is
            # reset by loadConfig itself, before the port is known (accepted limitation).
            configResult = loadConfig(
                DataPaths(dataDir), clock, logging.getLogger(f"{LOGGER_NAME}.persistence")
            )
            port = configResult.config.dashboardPort
            if not noServer:
                problem = dashboardPortProblem(server_launcher.BIND_ALL_INTERFACES, port)
                if problem is not None:
                    logger.error(
                        "dashboard port in use", extra={"detail": {"port": port, "error": problem}}
                    )
                    writeStderr(f"PC Handoff: dashboard port {port} is in use ({problem})")
                    return EXIT_PORT_IN_USE
            runtime = buildRuntime(dataDir, buildProvider(logger), clock, configResult=configResult)
            registerRuntime(runtime)
            poller = Poller(
                runtime,
                runtime.source,
                runtime.config.sessionPollIntervalSeconds,
                clock,
                logging.getLogger(f"{LOGGER_NAME}.runtime.poller"),
            )
            poller.start()
        except Exception as exc:
            reportFailure(logger, "startup failed", exc)
            return EXIT_FAILED

        if noServer:
            waitForShutdown(shutdownEvent or threading.Event(), logger)
            return EXIT_OK
        logger.info("dashboard server starting", extra={"detail": {"port": port}})
        try:
            server_launcher.runStreamlitServer(DASHBOARD_SCRIPT, port)
        except SystemExit as exc:
            if exc.code in (0, None):
                raise  # a clean stop keeps its meaning
            # Streamlit reports a bind failure (a port taken after the check) with sys.exit.
            reason = "the server exited; for example the port was taken after the check"
            logger.error(
                "dashboard server failed",
                extra={"detail": {"port": port, "code": exc.code, "reason": reason}},
            )
            writeStderr(
                f"PC Handoff: dashboard server failed on port {port} (exit code {exc.code})"
            )
            return EXIT_FAILED
        except Exception as exc:
            reportFailure(logger, "dashboard server failed", exc)
            return EXIT_FAILED
        return EXIT_OK
    finally:
        pollerStopped = poller is None or poller.stop()
        clearRuntime()
        if pollerStopped:
            lock.release()
        if logger is not None:
            if not pollerStopped:
                # Releasing now would let a new instance start while this poll may still
                # write; the OS releases the lock when this process exits (ADR-004).
                logger.warning("poller still running; the instance lock is left to the OS")
            logger.info("PC Handoff stopped")


def dashboardPortProblem(address: str, port: int) -> str | None:
    """Why the dashboard port cannot be used right now, or None when it is free.

    One exclusive dual-mode bind of ``[::]:port`` (``IPV6_V6ONLY`` off) conflicts with any
    listener on the port in either family: IPv4 ``0.0.0.0`` or ``127.0.0.1``, IPv6-only
    ``[::]``, dual-stack ``[::]`` (a default-address Streamlit) and ``[::1]``. An IPv4 bind
    alone misses the IPv6 kinds on Windows (Critic TASK-009 iteration 2). On a host without
    IPv6 the exclusive IPv4 bind of ``address:port`` is used instead.

    The probe sockets are closed at once and never listen, so they leave nothing behind for
    the server's own bind a moment later.
    """
    if socket.has_ipv6 and hasattr(socket, "AF_INET6"):
        dualMode = ((socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0),)
        try:
            probeBind(socket.AF_INET6, "::", port, dualMode)
            return None
        except OSError as exc:
            if exc.errno not in IPV6_UNAVAILABLE_ERRNOS:
                return str(exc)
    try:
        probeBind(socket.AF_INET, address, port, ())
    except OSError as exc:
        return str(exc)
    return None


def probeBind(family: int, host: str, port: int, options: tuple[tuple[int, int, int], ...]) -> None:
    """Bind (never listen) an exclusive probe socket, then close it; raises ``OSError``."""
    probe = socket.socket(family, socket.SOCK_STREAM)
    try:
        for level, option, value in options + PORT_PROBE_OPTIONS:
            probe.setsockopt(level, option, value)
        probe.bind((host, port))
    finally:
        probe.close()


def buildRuntime(
    dataDir: str | Path,
    provider: SessionProvider,
    clock: Clock,
    *,
    enumerationTimeoutSeconds: float | None = None,
    configResult: ConfigLoadResult | None = None,
) -> AppRuntime:
    """Load the data files once and assemble the runtime around ``provider``.

    ``configResult`` is an already loaded ``config.json`` (the launcher's, S8); without it the
    config is loaded here.

    Reuses the stores that loaded the files (``StartupContext``), so the activity log keeps
    its validated canonical cache (TASK-008 S6). The returned runtime has published its
    pre-poll snapshot but has not polled.
    """
    from pc_handoff.detection.timed_source import ENUMERATION_TIMEOUT_SECONDS, TimedSessionSource
    from pc_handoff.domain.tracker import SessionTracker
    from pc_handoff.persistence.paths import DataPaths
    from pc_handoff.persistence.startup_loader import loadStartup
    from pc_handoff.runtime.app_runtime import AppRuntime
    from pc_handoff.runtime.state_store import StateStore

    ctx = loadStartup(
        DataPaths(dataDir),
        clock,
        logging.getLogger(f"{LOGGER_NAME}.persistence"),
        configResult=configResult,
    )
    timeout = (
        ENUMERATION_TIMEOUT_SECONDS
        if enumerationTimeoutSeconds is None
        else enumerationTimeoutSeconds
    )
    return AppRuntime(
        config=ctx.config,
        configWarnings=ctx.configWarnings,
        startupContext=ctx,
        source=TimedSessionSource(provider, timeout),
        tracker=SessionTracker(clock, logging.getLogger(f"{LOGGER_NAME}.domain.tracker")),
        statusStore=ctx.statusStore,
        activityStore=ctx.activityStore,
        stateStore=StateStore(),
        clock=clock,
        logger=logging.getLogger(f"{LOGGER_NAME}.runtime"),
    )


def buildProvider(logger: logging.Logger) -> SessionProvider:
    """The WTS provider, or the static test provider when both test-hook variables are set."""
    sessionsFile = os.environ.get(TEST_SESSIONS_FILE_ENV)
    if os.environ.get(TEST_HOOKS_ENV) == "1" and sessionsFile:
        from pc_handoff.detection.static_provider import StaticSessionProvider

        logger.warning(
            "test session provider active", extra={"detail": {"sessionsFile": sessionsFile}}
        )
        return StaticSessionProvider(sessionsFile)
    from pc_handoff.detection.wts_provider import createDefaultProvider

    return createDefaultProvider()


def waitForShutdown(event: threading.Event, logger: logging.Logger) -> None:
    """Block until ``event`` is set; on the main thread SIGINT/SIGTERM/SIGBREAK set it."""
    previous = installShutdownHandlers(event)
    try:
        while not event.wait(SHUTDOWN_WAKE_SECONDS):
            pass
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    logger.info("shutdown requested")


def installShutdownHandlers(event: threading.Event) -> dict[int, object]:
    """Point the shutdown signals at ``event``; returns the handlers to restore.

    Python delivers signals to the main thread only, so nothing is installed elsewhere.
    """
    if threading.current_thread() is not threading.main_thread():
        return {}

    def onSignal(signum: int, frame: object) -> None:
        event.set()

    available = [getattr(signal, name) for name in SHUTDOWN_SIGNAL_NAMES if hasattr(signal, name)]
    return {signum: signal.signal(signum, onSignal) for signum in available}


def reportFailure(logger: logging.Logger | None, message: str, exc: Exception) -> None:
    """Log ``exc`` to ``app.log`` when logging is up; always write one line to stderr."""
    if logger is not None:
        logger.error(message, exc_info=(type(exc), exc, exc.__traceback__))
    writeStderr(f"PC Handoff: {message}: {type(exc).__name__}: {exc}")


def writeStderr(line: str) -> None:
    stream = sys.stderr
    if stream is None:  # pythonw.exe
        return
    try:
        stream.write(line + "\n")
        stream.flush()
    except (OSError, ValueError):
        pass
