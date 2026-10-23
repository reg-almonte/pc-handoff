"""Real server smoke test: ``python -m pc_handoff`` in a subprocess (TASK-009 AC-9, REQ AC-13).

The app runs with the static test provider on a free port and is stopped with CTRL_BREAK
(Windows) or SIGINT, which Streamlit's handlers turn into a clean server stop. Whatever
happens, the process tree is killed in ``finally`` so no server is left behind.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow

HEALTH_TIMEOUT_SECONDS = 15
STOP_TIMEOUT_SECONDS = 20
EXTERNAL_DIR = Path(__file__).resolve().parents[2]
SESSIONS = [
    {
        "sessionId": 2,
        "userName": "alice",
        "domain": "CORP",
        "state": "Active",
        "connectionType": "RDP",
        "clientName": "ALICE-LAPTOP",
        "clientAddress": "192.168.1.20",
        "connectTime": "2026-10-22T08:00:00+09:00",
    }
]


def freePort() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("0.0.0.0", 0))
        return probe.getsockname()[1]


def nonLoopbackIpv4() -> str | None:
    """This host's outbound IPv4 address (a UDP connect sends no packet), if any."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))  # TEST-NET-1
            address = probe.getsockname()[0]
    except OSError:
        return None
    return None if address.startswith("127.") or address == "0.0.0.0" else address


def get(url: str, timeout: float = 2.0) -> tuple[int, bytes]:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - local URL
        return response.status, response.read()


def waitForHealth(host: str, port: int, process: subprocess.Popen, timeout: float) -> float:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if process.poll() is not None:
            pytest.fail(f"the app exited early with code {process.returncode}")
        try:
            status, body = get(f"http://{host}:{port}/_stcore/health", timeout=1)
            if status == 200:
                return time.monotonic() - started
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.2)
    pytest.fail(f"/_stcore/health on {host}:{port} did not answer 200 within {timeout} s")


def killTree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True)
    else:  # pragma: no cover - exercised off Windows
        process.kill()
    process.wait(10)


def requestStop(process: subprocess.Popen) -> None:
    if sys.platform == "win32":
        process.send_signal(signal.CTRL_BREAK_EVENT)
    else:  # pragma: no cover - exercised off Windows
        process.send_signal(signal.SIGINT)


def appEnv(sessionsFile: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PC_HANDOFF_ALLOW_TEST_HOOKS"] = "1"
    env["PC_HANDOFF_TEST_SESSIONS_FILE"] = str(sessionsFile)
    env.pop("PC_HANDOFF_DATA_DIR", None)
    return env


def test_server_binds_all_interfaces_on_the_configured_port_and_stops_cleanly(tmp_path):
    dataDir = tmp_path / "data"
    dataDir.mkdir()
    port = freePort()
    (dataDir / "config.json").write_text(
        json.dumps({"schemaVersion": 1, "dashboardPort": port, "sessionPollIntervalSeconds": 2}),
        encoding="utf-8",
    )
    sessionsFile = tmp_path / "sessions.json"
    sessionsFile.write_text(json.dumps(SESSIONS), encoding="utf-8")
    output = tmp_path / "output.txt"
    creationFlags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0

    with open(output, "wb") as sink:
        process = subprocess.Popen(
            [sys.executable, "-m", "pc_handoff", "--data-dir", str(dataDir)],
            cwd=EXTERNAL_DIR,
            env=appEnv(sessionsFile),
            stdout=sink,
            stderr=subprocess.STDOUT,
            creationflags=creationFlags,
        )
        try:
            waitForHealth("127.0.0.1", port, process, HEALTH_TIMEOUT_SECONDS)
            status, page = get(f"http://127.0.0.1:{port}/")
            assert status == 200 and b"<html" in page.lower()
            address = nonLoopbackIpv4()
            if address is not None:  # bound to 0.0.0.0, not only loopback
                assert get(f"http://{address}:{port}/_stcore/health") == (200, b"ok")

            doc = json.loads((dataDir / "status.json").read_text(encoding="utf-8"))
            assert doc["status"] == "In Use"
            assert [s["userName"] for s in doc["sessions"]] == ["alice"]

            requestStop(process)
            exitCode = process.wait(STOP_TIMEOUT_SECONDS)
        finally:
            killTree(process)

    log = output.read_text(encoding="utf-8", errors="replace")
    assert exitCode == 0, log
    appLog = (dataDir / "logs" / "app.log").read_text(encoding="utf-8").splitlines()
    assert json.loads(appLog[-1])["message"] == "PC Handoff stopped"

    # The lock was released: a new start can take it.
    check = subprocess.run(
        [sys.executable, "-m", "pc_handoff", "--data-dir", str(dataDir), "--check-lock-only"],
        cwd=EXTERNAL_DIR,
        env=appEnv(sessionsFile),
        capture_output=True,
        timeout=60,
    )
    assert check.returncode == 0, check.stderr


def test_a_dual_stack_listener_on_the_port_refuses_the_start_with_exit_6(tmp_path):
    # Critic TASK-009 iteration 2: a default-address Streamlit 1.66 (the app on 8501 on the
    # dev PC) holds one dual-stack [::] socket, which an IPv4-only probe does not see.
    holder = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    holder.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    holder.bind(("::", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    assert port != 8501
    dataDir = tmp_path / "data"
    dataDir.mkdir()
    (dataDir / "config.json").write_text(
        json.dumps({"schemaVersion": 1, "dashboardPort": port}), encoding="utf-8"
    )
    sessionsFile = tmp_path / "sessions.json"
    sessionsFile.write_text(json.dumps(SESSIONS), encoding="utf-8")
    creationFlags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    process = subprocess.Popen(
        [sys.executable, "-m", "pc_handoff", "--data-dir", str(dataDir)],
        cwd=EXTERNAL_DIR,
        env=appEnv(sessionsFile),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creationFlags,
    )
    try:
        _, stderr = process.communicate(timeout=30)
    finally:
        killTree(process)
        holder.close()

    assert process.returncode == 6
    assert f"dashboard port {port} is in use" in stderr.decode("utf-8", "replace")
    lines = [
        json.loads(line) for line in (dataDir / "logs" / "app.log").read_text("utf-8").splitlines()
    ]
    [error] = [line for line in lines if line["level"] == "ERROR"]
    assert (error["message"], error["detail"]["port"]) == ("dashboard port in use", port)
    assert "poll completed" not in [line["message"] for line in lines]
    assert sorted(p.name for p in dataDir.iterdir()) == ["config.json", "logs", "pc-handoff.lock"]
