"""``launcher.main``: startup order, config once, lock exit codes, shutdown (TASK-009 AC-8).

``main`` runs in-process with ``--no-server`` (or with the server adapter monkeypatched) and
the static test provider selected through the two test-hook environment variables. The real
server runs only in ``test_server_smoke.py``.
"""

import ast
import builtins
import errno
import io
import json
import logging
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from pc_handoff import app_logging, launcher, server_launcher
from pc_handoff.app_logging import LOGGER_NAME, AppLogFileHandler
from pc_handoff.clock import SystemClock
from pc_handoff.detection import wts_provider
from pc_handoff.detection.static_provider import StaticSessionProvider
from pc_handoff.persistence.instance_lock import InstanceLock
from pc_handoff.runtime import poller as pollerModule
from pc_handoff.runtime import registry
from pc_handoff.runtime.app_runtime import AppRuntime
from pc_handoff.runtime.poller import THREAD_NAME

DATA_FILES = ("config.json", "status.json", "activity_log.json")
ALICE = {
    "sessionId": 2,
    "userName": "alice",
    "domain": "CORP",
    "state": "Active",
    "connectionType": "RDP",
    "clientName": "ALICE-LAPTOP",
    "clientAddress": "192.168.1.20",
    "connectTime": "2026-10-22T08:00:00+09:00",
}


@pytest.fixture(autouse=True)
def isolation() -> Iterator[None]:
    """Close the app.log handlers main() attaches and forget any registered runtime."""
    registry.clearRuntime()
    yield
    registry.clearRuntime()
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        if isinstance(handler, AppLogFileHandler):
            logger.removeHandler(handler)
            handler.close()


@pytest.fixture
def dataDir(tmp_path: Path) -> Path:
    directory = tmp_path / "data"
    directory.mkdir()
    return directory


@pytest.fixture
def testHooks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Select the static provider; returns its sessions file (alice Active)."""
    sessionsFile = tmp_path / "sessions.json"
    sessionsFile.write_text(json.dumps([ALICE]), encoding="utf-8")
    monkeypatch.setenv(launcher.TEST_HOOKS_ENV, "1")
    monkeypatch.setenv(launcher.TEST_SESSIONS_FILE_ENV, str(sessionsFile))
    return sessionsFile


def writeConfig(dataDir: Path, **settings: int) -> None:
    doc = {"schemaVersion": 1, "sessionPollIntervalSeconds": 2, **settings}
    (dataDir / "config.json").write_text(json.dumps(doc), encoding="utf-8")


def appLogLines(dataDir: Path) -> list[dict[str, Any]]:
    path = dataDir / "logs" / "app.log"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def waitUntil(predicate: Callable[[], bool], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def lockIsFree(dataDir: Path) -> bool:
    return launcher.main(["--data-dir", str(dataDir), "--check-lock-only"]) == 0


def pollerThreads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == THREAD_NAME]


OTHER_APPS_PORT = 8501  # another project's Streamlit app owns it on the dev PC: never touch it


def freePort() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("0.0.0.0", 0))
        port = probe.getsockname()[1]
    assert port != OTHER_APPS_PORT
    return port


# Listener kinds another app may use on the dashboard port (Critic TASK-009 iteration 2):
# (family, address, IPV6_V6ONLY or None). "dual-any" is what a default-address Streamlit 1.66
# binds, and what holds 8501 on the dev PC.
LISTENER_KINDS = {
    "v4-any": (socket.AF_INET, "0.0.0.0", None),
    "v4-loopback": (socket.AF_INET, "127.0.0.1", None),
    "v6only-any": (socket.AF_INET6, "::", 1),
    "dual-any": (socket.AF_INET6, "::", 0),
    "v6-loopback": (socket.AF_INET6, "::1", 1),
}


@contextmanager
def listeningOn(kind: str) -> Iterator[int]:
    """A real listening socket of ``kind`` (see ``LISTENER_KINDS``) on a free port."""
    family, address, v6only = LISTENER_KINDS[kind]
    holder = socket.socket(family, socket.SOCK_STREAM)
    try:
        if v6only is not None:
            holder.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, v6only)
        holder.bind((address, 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        assert port != OTHER_APPS_PORT
        yield port
    finally:
        holder.close()


IPV6_FAMILY = socket.AF_INET6  # captured: one test deletes socket.AF_INET6


class FakeSocketFactory:
    """``socket.socket`` stand-in: AF_INET6 sockets fail as ``failures`` says, others are real.

    ``failures`` maps "create", "setsockopt" or "bind" to the errno raised for AF_INET6.
    """

    def __init__(self, failures: dict[str, int]) -> None:
        self.failures = failures
        self.realSocket = socket.socket
        self.families: list[int] = []

    def __call__(self, family=socket.AF_INET, kind=socket.SOCK_STREAM, *args):
        self.families.append(family)
        if family != IPV6_FAMILY:
            return self.realSocket(family, kind, *args)
        if "create" in self.failures:
            raise OSError(self.failures["create"], "IPv6 not supported (simulated)")
        return FailingV6Socket(self.failures, self.realSocket(family, kind, *args))


class FailingV6Socket:
    def __init__(self, failures: dict[str, int], real: socket.socket) -> None:
        self.failures = failures
        self.real = real

    def setsockopt(self, *args):
        if "setsockopt" in self.failures:
            raise OSError(self.failures["setsockopt"], "option rejected (simulated)")
        return self.real.setsockopt(*args)

    def bind(self, address):
        if "bind" in self.failures:
            raise OSError(self.failures["bind"], "bind rejected (simulated)")
        return self.real.bind(address)

    def close(self):
        self.real.close()


class RunningMain:
    """``main(["--data-dir", d, "--no-server"])`` on a background thread."""

    def __init__(self, dataDir: Path) -> None:
        self.stop = threading.Event()
        self.result: list[int] = []
        self.thread = threading.Thread(
            target=lambda: self.result.append(
                launcher.main(["--data-dir", str(dataDir), "--no-server"], shutdownEvent=self.stop)
            ),
            name="test-main",
        )

    def __enter__(self) -> "RunningMain":
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop.set()
        self.thread.join(10)
        assert not self.thread.is_alive()


class OpenSpy:
    """Records ``(thread ident, file name)`` for every ``open`` of a data file."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[int, str]] = []
        original = io.open

        def spy(file: Any, *args: Any, **kwargs: Any) -> Any:
            if isinstance(file, str | Path):
                self.calls.append((threading.get_ident(), Path(file).name))
            return original(file, *args, **kwargs)

        monkeypatch.setattr(io, "open", spy)
        monkeypatch.setattr(builtins, "open", spy)

    def names(self, *, exceptThread: int | None = None, onlyThread: int | None = None):
        return [
            name
            for ident, name in self.calls
            if (exceptThread is None or ident != exceptThread)
            and (onlyThread is None or ident == onlyThread)
        ]


# --- AC-8 ---------------------------------------------------------------------------------


class TestAc08StartupOrderAndConfigOnce:
    def test_config_is_read_once_and_a_second_instance_exits_3(
        self, dataDir, testHooks, monkeypatch
    ):
        writeConfig(dataDir, dashboardRefreshIntervalSeconds=10)
        spy = OpenSpy(monkeypatch)
        testThread = threading.get_ident()

        with RunningMain(dataDir) as first:
            assert waitUntil(lambda: registry.getRuntime() is not None, 10)
            runtime = registry.getRuntime()
            assert waitUntil(lambda: runtime.stateStore.current().detectionHealthy, 10)
            assert runtime.config.dashboardRefreshIntervalSeconds == 10
            assert len(pollerThreads()) == 1

            writeConfig(dataDir, dashboardRefreshIntervalSeconds=30)  # edited while running
            polled = runtime.stateStore.current()
            assert waitUntil(lambda: runtime.stateStore.current() is not polled, 5)
            assert runtime.config.dashboardRefreshIntervalSeconds == 10
            assert registry.getRuntime() is runtime

            kept = ("config.json", "activity_log.json")
            before = {name: (dataDir / name).read_bytes() for name in kept}
            second: list[int] = []
            secondThread = threading.Thread(
                target=lambda: second.append(
                    launcher.main(["--data-dir", str(dataDir), "--no-server"])
                )
            )
            started = time.monotonic()
            secondThread.start()
            secondThread.join(10)
            assert second == [3]
            assert time.monotonic() - started < 2.0
            # The second instance opened no data file (only app.log, for its error line).
            assert set(spy.names(onlyThread=secondThread.ident)) <= {"app.log"}
            after = {name: (dataDir / name).read_bytes() for name in before}
            assert after == before
            assert registry.getRuntime() is runtime  # untouched by the second main
            assert len(pollerThreads()) == 1

        assert first.result == [0]
        # config.json was opened exactly once by the app, at startup.
        assert spy.names(exceptThread=testThread).count("config.json") == 1
        assert pollerThreads() == []
        assert registry.getRuntime() is None
        assert lockIsFree(dataDir)

        messages = [line["message"] for line in appLogLines(dataDir)]
        assert messages[0] == "PC Handoff starting"
        assert "test session provider active" in messages
        assert any(m.startswith("another PC Handoff instance holds") for m in messages)
        assert "shutdown requested" in messages
        assert messages[-1] == "PC Handoff stopped"

    def test_startup_order_lock_then_logging_then_load_then_poller(
        self, dataDir, testHooks, monkeypatch
    ):
        order: list[str] = []
        realAcquire = InstanceLock.acquire

        def acquire(self):
            order.append("lock")
            return realAcquire(self)

        realConfigure = app_logging.configureAppLogging

        def configure(directory):
            order.append("logging")
            return realConfigure(directory)

        realBuild = launcher.buildRuntime

        def build(*args, **kwargs):
            order.append("load")
            assert not (dataDir / "status.json").exists()  # nothing written before the poller
            return realBuild(*args, **kwargs)

        def server(script, port):
            order.append("server")
            assert len(pollerThreads()) == 1

        writeConfig(dataDir, dashboardPort=freePort())
        monkeypatch.setattr(InstanceLock, "acquire", acquire)
        monkeypatch.setattr(app_logging, "configureAppLogging", configure)
        monkeypatch.setattr(launcher, "buildRuntime", build)
        monkeypatch.setattr(server_launcher, "runStreamlitServer", server)
        assert launcher.main(["--data-dir", str(dataDir)]) == 0
        assert order == ["lock", "logging", "load", "server"]


# --- lock exit codes and --check-lock-only (TASK-007) ------------------------------------


class TestLockExitCodes:
    def test_check_lock_only_reports_free_held_and_unavailable(self, tmp_path, dataDir):
        assert lockIsFree(dataDir)
        assert not any((dataDir / name).exists() for name in DATA_FILES)
        with InstanceLock(dataDir, SystemClock()):
            assert launcher.main(["--data-dir", str(dataDir), "--check-lock-only"]) == 3
        notADirectory = tmp_path / "file"
        notADirectory.write_text("x", encoding="utf-8")
        assert launcher.main(["--data-dir", str(notADirectory), "--check-lock-only"]) == 4

    def test_a_held_lock_exits_3_without_creating_data_files(self, dataDir, capsys):
        with InstanceLock(dataDir, SystemClock()):
            assert launcher.main(["--data-dir", str(dataDir), "--no-server"]) == 3
        assert not any((dataDir / name).exists() for name in DATA_FILES)
        assert "another PC Handoff instance holds" in capsys.readouterr().err
        [line] = appLogLines(dataDir)
        assert (line["level"], line["detail"]["exitCode"]) == ("ERROR", 3)

    def test_an_unusable_data_directory_exits_4(self, tmp_path):
        notADirectory = tmp_path / "file"
        notADirectory.write_text("x", encoding="utf-8")
        assert launcher.main(["--data-dir", str(notADirectory), "--no-server"]) == 4


# --- failures, Python version, server path -----------------------------------------------


class TestFailuresAndServer:
    def test_a_startup_failure_exits_1_with_the_lock_released(
        self, dataDir, testHooks, monkeypatch, capsys
    ):
        def failing(*args, **kwargs):
            raise RuntimeError("cannot build")

        monkeypatch.setattr(launcher, "buildRuntime", failing)
        assert launcher.main(["--data-dir", str(dataDir), "--no-server"]) == 1
        assert lockIsFree(dataDir)
        assert registry.getRuntime() is None
        assert "startup failed: RuntimeError: cannot build" in capsys.readouterr().err
        [error] = [line for line in appLogLines(dataDir) if line["message"] == "startup failed"]
        assert error["level"] == "ERROR" and "cannot build" in error["detail"]

    def test_a_logging_failure_exits_1_with_the_lock_released(self, dataDir, monkeypatch, capsys):
        def failing(directory):
            raise OSError("logs not writable")

        monkeypatch.setattr(app_logging, "configureAppLogging", failing)
        assert launcher.main(["--data-dir", str(dataDir), "--no-server"]) == 1
        assert lockIsFree(dataDir)
        assert "startup failed: OSError: logs not writable" in capsys.readouterr().err

    def test_the_server_runs_with_the_registered_runtime_then_everything_stops(
        self, dataDir, testHooks, monkeypatch
    ):
        port = freePort()
        writeConfig(dataDir, dashboardPort=port)
        seen: dict[str, Any] = {}

        def server(script, port):
            seen["script"], seen["port"] = Path(script), port
            seen["runtime"] = registry.getRuntime()
            seen["pollers"] = len(pollerThreads())
            seen["main"] = threading.current_thread() is threading.main_thread()

        monkeypatch.setattr(server_launcher, "runStreamlitServer", server)
        assert launcher.main(["--data-dir", str(dataDir)]) == 0
        assert seen["script"] == launcher.DASHBOARD_SCRIPT and seen["script"].is_file()
        assert seen["port"] == port
        assert seen["runtime"] is not None
        assert seen["pollers"] == 1
        assert seen["main"] is True  # bootstrap.run installs signal handlers
        assert pollerThreads() == []
        assert registry.getRuntime() is None
        assert lockIsFree(dataDir)
        messages = [line["message"] for line in appLogLines(dataDir)]
        assert "dashboard server starting" in messages
        assert messages[-1] == "PC Handoff stopped"

    def test_a_server_failure_exits_1_and_cleans_up(self, dataDir, testHooks, monkeypatch):
        writeConfig(dataDir, dashboardPort=freePort())

        def server(script, port):
            raise OSError("port in use")

        monkeypatch.setattr(server_launcher, "runStreamlitServer", server)
        assert launcher.main(["--data-dir", str(dataDir)]) == 1
        assert pollerThreads() == []
        assert lockIsFree(dataDir)
        assert any(line["message"] == "dashboard server failed" for line in appLogLines(dataDir))

    @pytest.mark.parametrize("code", [1, 3])
    def test_a_server_exit_with_a_failure_code_is_logged_and_returns_1(
        self, dataDir, testHooks, monkeypatch, capsys, code
    ):
        # Critic TASK-009 M1: Streamlit reports its own bind failure with sys.exit(1) (3 when
        # uvicorn never starts), which bypassed "except Exception" and left app.log silent.
        port = freePort()
        writeConfig(dataDir, dashboardPort=port)

        def server(script, p):
            raise SystemExit(code)

        monkeypatch.setattr(server_launcher, "runStreamlitServer", server)
        assert launcher.main(["--data-dir", str(dataDir)]) == 1
        [error] = [
            line for line in appLogLines(dataDir) if line["message"] == "dashboard server failed"
        ]
        assert error["level"] == "ERROR"
        assert (error["detail"]["port"], error["detail"]["code"]) == (port, code)
        assert (
            f"dashboard server failed on port {port} (exit code {code})" in capsys.readouterr().err
        )
        assert pollerThreads() == []
        assert registry.getRuntime() is None
        assert lockIsFree(dataDir)
        assert appLogLines(dataDir)[-1]["message"] == "PC Handoff stopped"

    @pytest.mark.parametrize("code", [0, None])
    def test_a_clean_server_system_exit_propagates_after_cleanup(
        self, dataDir, testHooks, monkeypatch, code
    ):
        writeConfig(dataDir, dashboardPort=freePort())

        def server(script, port):
            raise SystemExit(code)

        monkeypatch.setattr(server_launcher, "runStreamlitServer", server)
        with pytest.raises(SystemExit) as info:
            launcher.main(["--data-dir", str(dataDir)])
        assert info.value.code == code
        assert pollerThreads() == []
        assert lockIsFree(dataDir)
        messages = [line["message"] for line in appLogLines(dataDir)]
        assert "dashboard server failed" not in messages
        assert messages[-1] == "PC Handoff stopped"

    def test_python_older_than_3_11_exits_5_before_touching_anything(
        self, dataDir, monkeypatch, capsys
    ):
        monkeypatch.setattr(launcher, "pythonVersion", lambda: (3, 10, 12))
        assert launcher.main(["--data-dir", str(dataDir)]) == 5
        assert "requires Python 3.11 or later; this is Python 3.10.12" in capsys.readouterr().err
        assert list(dataDir.iterdir()) == []

    def test_the_real_python_version_passes(self):
        assert launcher.pythonVersion() >= launcher.MIN_PYTHON


# --- dashboard port in use (user decision 2026-10-22, Critic TASK-009 M1) ----------------


class TestDashboardPortInUse:
    @pytest.mark.parametrize("kind", list(LISTENER_KINDS))
    def test_a_port_in_use_exits_6_before_any_poll(
        self, dataDir, testHooks, monkeypatch, capsys, kind
    ):
        # Every listener kind on the port refuses the start: an IPv4 probe alone misses the
        # IPv6-only, dual-stack and [::1] kinds on Windows (Critic TASK-009 iteration 2).
        polls: list[object] = []
        monkeypatch.setattr(AppRuntime, "pollOnce", lambda self, traceId=None: polls.append(1))

        def server(script, port):
            pytest.fail("the server must not start")

        monkeypatch.setattr(server_launcher, "runStreamlitServer", server)
        with listeningOn(kind) as port:
            writeConfig(dataDir, dashboardPort=port)
            started = time.monotonic()
            code = launcher.main(["--data-dir", str(dataDir)])
            elapsed = time.monotonic() - started

        assert code == launcher.EXIT_PORT_IN_USE == 6
        assert elapsed < 3
        assert polls == []
        assert pollerThreads() == []
        lines = appLogLines(dataDir)
        [error] = [line for line in lines if line["level"] == "ERROR"]
        assert error["message"] == "dashboard port in use"
        assert error["detail"]["port"] == port
        messages = [line["message"] for line in lines]
        assert "poll completed" not in messages
        assert "dashboard server starting" not in messages
        assert messages[-1] == "PC Handoff stopped"
        # S8: checked before status.json and activity_log.json are touched; only config.json
        # (read once) and the lock and log exist.
        assert sorted(p.name for p in dataDir.iterdir()) == [
            "config.json",
            "logs",
            "pc-handoff.lock",
        ]
        assert registry.getRuntime() is None
        assert lockIsFree(dataDir)
        assert f"dashboard port {port} is in use" in capsys.readouterr().err

    def test_no_server_runs_even_when_the_dashboard_port_is_taken(self, dataDir, testHooks):
        with listeningOn("dual-any") as port:
            writeConfig(dataDir, dashboardPort=port)
            with RunningMain(dataDir) as running:
                assert waitUntil(lambda: registry.getRuntime() is not None, 10)
                runtime = registry.getRuntime()
                assert waitUntil(lambda: runtime.stateStore.current().detectionHealthy, 10)
        assert running.result == [0]
        messages = [line["message"] for line in appLogLines(dataDir)]
        assert "dashboard port in use" not in messages
        assert "poll completed" in messages

    def test_the_probe_leaves_a_free_port_free(self):
        port = freePort()
        assert launcher.dashboardPortProblem("0.0.0.0", port) is None
        # The probe never listens, so it leaves nothing behind: probing again works at once.
        assert launcher.dashboardPortProblem("0.0.0.0", port) is None

    @pytest.mark.parametrize("kind", list(LISTENER_KINDS))
    def test_the_probe_reports_every_listener_kind(self, kind):
        with listeningOn(kind) as taken:
            assert launcher.dashboardPortProblem("0.0.0.0", taken) is not None

    def test_a_port_with_time_wait_connections_is_free(self):
        # A restart right after a stop: our server closed its accepted connections first, so
        # they sit in TIME_WAIT on the port; that must not count as "in use".
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("0.0.0.0", 0))
        server.listen(5)
        port = server.getsockname()[1]
        assert port != OTHER_APPS_PORT
        for _ in range(3):
            client = socket.create_connection(("127.0.0.1", port))
            accepted, _ = server.accept()
            accepted.close()  # the server side closes first: TIME_WAIT on the port
            client.close()
        server.close()
        states = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True)
        timeWait = [
            line
            for line in states.stdout.splitlines()
            if f":{port} " in line and "TIME_WAIT" in line
        ]
        assert timeWait, "the test setup left no TIME_WAIT entry"
        assert launcher.dashboardPortProblem("0.0.0.0", port) is None

    @pytest.mark.parametrize(
        "failures",
        [
            {"create": errno.EAFNOSUPPORT},
            {"setsockopt": errno.WSAENOPROTOOPT},
            {"setsockopt": errno.WSAEINVAL},
            {"bind": errno.WSAEINVAL},  # what Windows reports, not errno.EINVAL (22)
            {"bind": errno.EINVAL},
            {"bind": errno.WSAEADDRNOTAVAIL},
            {"bind": errno.WSAEAFNOSUPPORT},
            {"bind": errno.WSAEPROTONOSUPPORT},
        ],
        ids=lambda f: f"{next(iter(f))}-{next(iter(f.values()))}",
    )
    def test_ipv6_unavailable_falls_back_to_the_ipv4_probe(self, monkeypatch, failures):
        free = freePort()
        with listeningOn("v4-any") as taken:
            factory = FakeSocketFactory(failures)
            monkeypatch.setattr(socket, "socket", factory)
            assert launcher.dashboardPortProblem("0.0.0.0", free) is None  # no false exit 6
            assert factory.families == [socket.AF_INET6, socket.AF_INET]
            assert launcher.dashboardPortProblem("0.0.0.0", taken) is not None

    @pytest.mark.parametrize("change", ["no-has-ipv6", "no-af-inet6"])
    def test_a_host_without_ipv6_uses_the_ipv4_probe(self, monkeypatch, change):
        free = freePort()
        with listeningOn("v4-any") as taken:
            factory = FakeSocketFactory({})
            monkeypatch.setattr(socket, "socket", factory)
            if change == "no-has-ipv6":
                monkeypatch.setattr(socket, "has_ipv6", False)
            else:
                monkeypatch.delattr(socket, "AF_INET6")
            assert launcher.dashboardPortProblem("0.0.0.0", free) is None
            assert launcher.dashboardPortProblem("0.0.0.0", taken) is not None
            assert factory.families == [socket.AF_INET] * 2
            monkeypatch.undo()

    @pytest.mark.parametrize("code", [errno.WSAEADDRINUSE, errno.WSAEACCES])
    def test_an_ipv6_conflict_is_not_mistaken_for_missing_ipv6(self, monkeypatch, code):
        # "In use" (or an exclusive-bind conflict reported as access denied) means taken.
        free = freePort()
        monkeypatch.setattr(socket, "socket", FakeSocketFactory({"bind": code}))
        assert launcher.dashboardPortProblem("0.0.0.0", free) is not None

    def test_a_corrupt_status_json_is_left_alone_when_the_port_is_taken(
        self, dataDir, testHooks, monkeypatch
    ):
        # S8: the check runs before status.json is read, so the one-time "status data was
        # reset" notice (REQ AC-17) is still shown by the next start that can serve.
        def server(script, port):
            pytest.fail("the server must not start")

        monkeypatch.setattr(server_launcher, "runStreamlitServer", server)
        corrupt = b'{"status": "Maybe"}'
        (dataDir / "status.json").write_bytes(corrupt)
        with listeningOn("dual-any") as port:
            writeConfig(dataDir, dashboardPort=port)
            assert launcher.main(["--data-dir", str(dataDir)]) == 6
        assert (dataDir / "status.json").read_bytes() == corrupt
        assert list(dataDir.glob("*.corrupt-*")) == []
        assert not (dataDir / "activity_log.json").exists()

        runtime = launcher.buildRuntime(dataDir, StaticSessionProvider(testHooks), SystemClock())
        texts = [n.text for n in runtime.stateStore.current().notices]
        assert len(texts) == 1 and texts[0].startswith("Status data was reset (backup: ")

    def test_config_json_is_still_read_exactly_once(self, dataDir, testHooks, monkeypatch):
        # S8 moved the config load ahead of the port check; it must not add a second read.
        writeConfig(dataDir, dashboardPort=freePort())
        spy = OpenSpy(monkeypatch)  # installed after the test wrote config.json
        monkeypatch.setattr(server_launcher, "runStreamlitServer", lambda script, port: None)
        assert launcher.main(["--data-dir", str(dataDir)]) == 0
        assert spy.names().count("config.json") == 1


# --- a poller that does not stop keeps the lock (Critic TASK-009 S3) ---------------------


class TestStuckPollerKeepsTheLock:
    def test_the_lock_is_left_to_process_exit_while_a_poll_still_runs(
        self, dataDir, testHooks, monkeypatch
    ):
        inPoll = threading.Event()
        release = threading.Event()
        realPollOnce = AppRuntime.pollOnce

        def stuckPoll(self, traceId=None):
            inPoll.set()
            release.wait(20)  # e.g. a write hung on an antivirus lock
            return realPollOnce(self, traceId)

        realStop = pollerModule.Poller.stop
        locks: list[InstanceLock] = []
        realAcquire = InstanceLock.acquire
        releases: list[InstanceLock] = []
        realRelease = InstanceLock.release

        def acquire(self):
            realAcquire(self)
            locks.append(self)

        def recordedRelease(self):
            releases.append(self)
            realRelease(self)

        monkeypatch.setattr(AppRuntime, "pollOnce", stuckPoll)
        monkeypatch.setattr(
            pollerModule.Poller, "stop", lambda self, timeout=5.0: realStop(self, 0.2)
        )
        monkeypatch.setattr(InstanceLock, "acquire", acquire)
        monkeypatch.setattr(InstanceLock, "release", recordedRelease)
        try:
            with RunningMain(dataDir) as running:
                assert inPoll.wait(10)
            assert running.result == [0]
            [mainLock] = locks
            assert mainLock.isHeld and mainLock not in releases
            assert len(pollerThreads()) == 1  # still inside its poll
            lines = appLogLines(dataDir)
            # The lock still guards the data directory while that poll may write.
            assert launcher.main(["--data-dir", str(dataDir), "--check-lock-only"]) == 3
            [warning] = [
                line
                for line in lines
                if line["message"] == "poller still running; the instance lock is left to the OS"
            ]
            assert warning["level"] == "WARNING"
            assert lines[-1]["message"] == "PC Handoff stopped"
        finally:
            release.set()
            for thread in pollerThreads():
                thread.join(10)
            for lock in locks:
                realRelease(lock)  # what process exit does for python -m pc_handoff
        assert pollerThreads() == []
        assert lockIsFree(dataDir)

    def test_a_stopped_poller_releases_the_lock(self, dataDir, testHooks, monkeypatch):
        releases: list[InstanceLock] = []
        realRelease = InstanceLock.release

        def recordedRelease(self):
            releases.append(self)
            realRelease(self)

        monkeypatch.setattr(InstanceLock, "release", recordedRelease)
        with RunningMain(dataDir) as running:
            assert waitUntil(lambda: registry.getRuntime() is not None, 10)
        assert running.result == [0]
        assert len(releases) == 1 and not releases[0].isHeld
        messages = [line["message"] for line in appLogLines(dataDir)]
        assert "poller still running; the instance lock is left to the OS" not in messages


# --- data directory, provider selection, shutdown, stderr --------------------------------


class TestHelpers:
    def test_data_dir_option_then_environment_then_default(self, tmp_path, monkeypatch):
        monkeypatch.delenv(launcher.DATA_DIR_ENV, raising=False)
        assert launcher.resolveDataDir(None) == launcher.DEFAULT_DATA_DIR
        assert launcher.DEFAULT_DATA_DIR.parent == Path(__file__).resolve().parents[2]
        monkeypatch.setenv(launcher.DATA_DIR_ENV, str(tmp_path / "env"))
        assert launcher.resolveDataDir(None) == tmp_path / "env"
        assert launcher.resolveDataDir(str(tmp_path / "opt")) == tmp_path / "opt"

    def test_main_uses_the_environment_data_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv(launcher.DATA_DIR_ENV, str(tmp_path / "env"))
        assert launcher.main(["--check-lock-only"]) == 0
        assert (tmp_path / "env" / "pc-handoff.lock").exists()

    def test_the_static_provider_needs_both_test_hook_variables(
        self, tmp_path, monkeypatch, capturedAppLog
    ):
        logger = logging.getLogger(f"{LOGGER_NAME}.test")
        sentinel = object()
        monkeypatch.setattr(wts_provider, "createDefaultProvider", lambda: sentinel)
        monkeypatch.delenv(launcher.TEST_HOOKS_ENV, raising=False)
        monkeypatch.setenv(launcher.TEST_SESSIONS_FILE_ENV, str(tmp_path / "s.json"))
        assert launcher.buildProvider(logger) is sentinel
        monkeypatch.setenv(launcher.TEST_HOOKS_ENV, "yes")
        assert launcher.buildProvider(logger) is sentinel
        monkeypatch.setenv(launcher.TEST_HOOKS_ENV, "1")
        monkeypatch.delenv(launcher.TEST_SESSIONS_FILE_ENV)
        assert launcher.buildProvider(logger) is sentinel
        assert capturedAppLog() == []

        monkeypatch.setenv(launcher.TEST_SESSIONS_FILE_ENV, str(tmp_path / "s.json"))
        provider = launcher.buildProvider(logger)
        assert isinstance(provider, StaticSessionProvider)
        assert provider.sessionsFile == tmp_path / "s.json"
        [warning] = capturedAppLog()
        assert (warning["level"], warning["message"]) == ("WARNING", "test session provider active")

    @pytest.mark.parametrize(
        "name", [n for n in ("SIGINT", "SIGBREAK", "SIGTERM") if hasattr(signal, n)]
    )
    def test_a_signal_on_the_main_thread_requests_shutdown(self, name):
        assert threading.current_thread() is threading.main_thread()
        signum = getattr(signal, name)
        before = signal.getsignal(signum)
        event = threading.Event()
        timer = threading.Timer(0.2, signal.raise_signal, args=(signum,))
        timer.start()
        started = time.monotonic()
        launcher.waitForShutdown(event, logging.getLogger(f"{LOGGER_NAME}.test"))
        timer.join()
        assert event.is_set()
        assert time.monotonic() - started < 3
        assert signal.getsignal(signum) == before  # restored

    def test_no_signal_handlers_are_installed_off_the_main_thread(self):
        result: list[dict] = []
        thread = threading.Thread(
            target=lambda: result.append(launcher.installShutdownHandlers(threading.Event()))
        )
        thread.start()
        thread.join()
        assert result == [{}]

    def test_write_stderr_survives_a_missing_or_broken_stream(self, monkeypatch):
        monkeypatch.setattr(sys, "stderr", None)
        launcher.writeStderr("x")

        class Broken:
            def write(self, text):
                raise OSError("broken pipe")

        monkeypatch.setattr(sys, "stderr", Broken())
        launcher.writeStderr("x")


class TestLauncherModuleStaysImportableOnOldPythons:
    """The version check (exit 5) only works if the module itself loads on old Pythons."""

    SOURCE = Path(launcher.__file__).read_text(encoding="utf-8")

    def test_it_parses_with_python_3_8_grammar(self):
        ast.parse(self.SOURCE, feature_version=(3, 8))

    def test_module_level_imports_are_standard_library_only(self):
        tree = ast.parse(self.SOURCE)
        names: list[str] = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.append(node.module or "")
        top = {name.split(".")[0] for name in names}
        assert "pc_handoff" not in top
        assert top <= set(sys.stdlib_module_names) | {"__future__"}
