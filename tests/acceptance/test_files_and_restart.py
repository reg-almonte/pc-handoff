"""REQ-001 end to end: data files, corruption, restarts and the single-instance lock (TASK-011).

REQ AC-16 to AC-20 and AC-27. AC-16 runs the real launcher (``launcher.main`` with
``--no-server``, the static test-hook provider and the system clock), so the lock, ``app.log``
and file creation are the production path. The others drive ``buildRuntime`` through the
``app`` fixture, seeding or damaging files between runs exactly as an operator or a crash
would leave them.

The last test re-checks the single-instance lock and the stale-lock outcome end to end with
real ``python -m pc_handoff`` processes (pending requirement amendment 1, decisions.log; it is
covered at task level by TASK-007 AC-1/AC-2 and is not a REQ-001 AC).
"""

import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from pc_handoff import launcher
from pc_handoff.clock import parseIso
from pc_handoff.persistence.config_store import DEFAULT_CONFIG_DOCUMENT
from pc_handoff.runtime import registry
from pc_handoff.runtime.app_runtime import POLL_ENUMERATION_FAILED, POLL_OK
from tests.acceptance.conftest import (
    APPTEST_TIMEOUT_SECONDS,
    DASHBOARD_SCRIPT,
    EXTERNAL_DIR,
    T0,
    hms,
    iso,
    pageFrom,
    readJson,
)
from tests.fakes.builders import osSession
from tests.fakes.log_factory import startEvent

LOGON = T0 - timedelta(hours=1)
C1 = T0 - timedelta(minutes=30)
STAMP_T0 = "20261021T090000"  # T0 as a backup suffix (yyyyMMddTHHmmss)
EMPTY_LOG = {"schemaVersion": 1, "events": []}


def alice(state="Active", **overrides):
    fields = {"logonTime": LOGON, "connectTime": C1, "clientName": "ALICE-LAPTOP"}
    fields.update(overrides)
    return osSession(2, "alice", state, **fields)


def waitUntil(predicate, timeout: float, step: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


def statusOrNone(dataDir: Path) -> str | None:
    try:
        return readJson(dataDir / "status.json")["status"]
    except (OSError, ValueError, KeyError):
        return None


# --- REQ AC-16 ------------------------------------------------------------------------------


ALICE_FILE_ENTRY = {
    "sessionId": 2,
    "userName": "alice",
    "domain": "CORP",
    "state": "Active",
    "clientName": "ALICE-LAPTOP",
    "clientAddress": "192.168.1.20",
    "connectTime": "2026-10-21T08:30:00+09:00",
}


@pytest.mark.parametrize("variant", ["no-directory", "empty-directory", "partial-config"])
def test_ac16_missing_data_files_are_created_with_defaults(
    tmp_path, monkeypatch, checkedDataDirs, variant
):
    dataDir = tmp_path / "data"
    checkedDataDirs.append(dataDir)
    partial = b'{"schemaVersion": 1, "sessionPollIntervalSeconds": 2}'
    if variant != "no-directory":
        dataDir.mkdir()
    if variant == "partial-config":
        (dataDir / "config.json").write_bytes(partial)
    sessionsFile = tmp_path / "sessions.json"
    sessionsFile.write_text(json.dumps([ALICE_FILE_ENTRY]), encoding="utf-8")
    monkeypatch.setenv(launcher.TEST_HOOKS_ENV, "1")
    monkeypatch.setenv(launcher.TEST_SESSIONS_FILE_ENV, str(sessionsFile))

    stop, result = threading.Event(), []
    thread = threading.Thread(
        target=lambda: result.append(
            launcher.main(["--data-dir", str(dataDir), "--no-server"], shutdownEvent=stop)
        )
    )
    thread.start()
    try:
        assert waitUntil(lambda: statusOrNone(dataDir) == "In Use", 10)
        page = pageFrom(
            AppTest.from_file(DASHBOARD_SCRIPT, default_timeout=APPTEST_TIMEOUT_SECONDS).run()
        )
        runtime = registry.getRuntime()
        assert runtime is not None
        config = runtime.config
    finally:
        stop.set()
        thread.join(15)
    assert result == [launcher.EXIT_OK]

    names = sorted(p.name for p in dataDir.iterdir())
    assert names == ["activity_log.json", "config.json", "logs", "pc-handoff.lock", "status.json"]
    assert (dataDir / "logs" / "app.log").is_file()
    assert page.badge == "In Use — alice (192.168.1.20)"
    if variant == "partial-config":
        assert (dataDir / "config.json").read_bytes() == partial  # not missing, not corrupt
        assert (config.sessionPollIntervalSeconds, config.dashboardRefreshIntervalSeconds) == (
            2,
            10,
        )
        assert config.dashboardPort == 8501
        assert page.warnings == [
            f"Configuration (config.json): {warning}"
            for warning in (
                "dashboardRefreshIntervalSeconds: missing; default 10 applied",
                "dashboardPort: missing; default 8501 applied",
            )
        ]
    else:
        assert readJson(dataDir / "config.json") == {
            "schemaVersion": 1,
            "dashboardRefreshIntervalSeconds": 10,
            "sessionPollIntervalSeconds": 5,
            "dashboardPort": 8501,
        }
        assert page.warnings == []  # no error banner, no notice
    log = readJson(dataDir / "activity_log.json")
    assert list(log) == ["schemaVersion", "events"] and log["schemaVersion"] == 1
    assert [(e["eventType"], e["userName"], e["reason"]) for e in log["events"]] == [
        ("session_start", "alice", "detected_at_startup")
    ]
    assert readJson(dataDir / "status.json")["status"] == "In Use"


# --- REQ AC-17 ------------------------------------------------------------------------------


CORRUPT_STATUS = {
    "unparseable": b'{"schemaVersion": 1, "status": "In Use", ',
    "status-maybe": json.dumps(
        {
            "schemaVersion": 1,
            "status": "Maybe",
            "lastKnownStatus": "In Use",
            "lastKnownStatusAt": "2026-10-21T08:00:00+09:00",
            "lastSuccessfulPollAt": "2026-10-21T08:00:00+09:00",
            "updatedAt": "2026-10-21T08:00:00+09:00",
            "detectionHealthy": True,
            "sessions": [],
        }
    ).encode("utf-8"),
    "schema-version-2": b'{"schemaVersion": 2, "status": "In Use", "sessions": []}',
}


@pytest.mark.parametrize("corruption", list(CORRUPT_STATUS))
def test_ac17_corrupt_status_json_is_backed_up_and_periods_rebuilt_from_the_log(app, corruption):
    startA = C1 + timedelta(seconds=3)  # timestamp of U1's session_start
    connectB = T0 - timedelta(minutes=20)
    startB = connectB + timedelta(seconds=4)  # timestamp of U2's session_start
    u1 = startEvent(
        timestamp=startA,
        usagePeriodStart=C1,
        sessionId=2,
        userName="alice",
        logonTime=LOGON,
        clientName="ALICE-LAPTOP",
    ).toJson()
    u2 = startEvent(
        timestamp=startB,
        usagePeriodStart=connectB,
        sessionId=3,
        userName="bob",
        logonTime=LOGON,
    ).toJson()
    app.dataDir.mkdir(parents=True)
    log = {"schemaVersion": 1, "events": [u1, u2]}
    app.path("activity_log.json").write_text(json.dumps(log), encoding="utf-8")
    damaged = CORRUPT_STATUS[corruption]
    app.path("status.json").write_bytes(damaged)

    app.start()
    app.setSessions(alice())  # alice still Active with C1; bob has logged off
    assert app.poll() == POLL_OK

    [backup] = app.backups("status.json")
    assert backup.name == f"status.json.corrupt-{STAMP_T0}"
    assert backup.read_bytes() == damaged
    [entry] = app.statusJson()["sessions"]
    assert (entry["usagePeriodId"], entry["usagePeriodStart"]) == (u1["usagePeriodId"], iso(C1))
    assert app.starts() == [u1, u2]  # no new session_start for alice
    [end] = app.ends()
    assert end["usagePeriodId"] == u2["usagePeriodId"]
    assert (end["reason"], end["endCause"], end["durationApproximate"]) == (
        "detected_at_startup",
        "logoff",
        True,
    )
    assert end["periodEnd"] == iso(startB)  # no lastSuccessfulPollAt survived
    assert end["durationSeconds"] == (startB - connectB).total_seconds() == 4

    notice = f"Status data was reset (backup: status.json.corrupt-{STAMP_T0})"
    page = app.render()
    assert notice in page.warnings and page.badge == "In Use — alice (192.168.1.20)"
    assert app.tick() == POLL_OK  # it keeps running
    app.restart()
    app.tick()
    assert notice not in app.render().warnings  # one-time


# --- REQ AC-18 ------------------------------------------------------------------------------


CORRUPT_CONFIG = {
    "unparseable": b'{"schemaVersion": 1, "dashboardPort": ',
    "root-not-object": b'[{"schemaVersion": 1}]',
    "schema-version-missing": b'{"dashboardPort": 9000}',
    "schema-version-2": b'{"schemaVersion": 2, "dashboardPort": 9000}',
}


def damageLog(path: Path, corruption: str) -> bytes:
    doc = readJson(path)
    if corruption == "unparseable":
        damaged = path.read_bytes()[:-20]
    elif corruption == "fails-schema":
        doc["events"][0]["reason"] = "maybe"
        damaged = json.dumps(doc).encode("utf-8")
    else:
        doc["schemaVersion"] = 2
        damaged = json.dumps(doc).encode("utf-8")
    path.write_bytes(damaged)
    return damaged


@pytest.mark.parametrize(
    ("fileName", "corruption"),
    [("config.json", c) for c in CORRUPT_CONFIG]
    + [("activity_log.json", c) for c in ("unparseable", "fails-schema", "schema-version-2")],
)
def test_ac18_corrupt_config_or_activity_log_is_backed_up_and_pairing_kept(
    app, fileName, corruption
):
    if fileName == "config.json":
        app.dataDir.mkdir(parents=True)
        damaged = CORRUPT_CONFIG[corruption]
        app.path("config.json").write_bytes(damaged)
        app.start()
        app.setSessions(alice())
        app.poll()
        stamp = STAMP_T0
        assert readJson(app.path("config.json")) == DEFAULT_CONFIG_DOCUMENT
        assert app.runtime.config.dashboardPort == 8501
    else:
        app.start()  # a first run leaves a valid status.json with alice's open period U1
        app.setSessions(alice())
        app.poll()
        app.tick()
        [u1] = app.starts()
        app.stop()
        damaged = damageLog(app.path("activity_log.json"), corruption)
        app.clock.advance(60)
        stamp = app.now().strftime("%Y%m%dT%H%M%S")
        app.start()
        assert app.activityJson() == EMPTY_LOG  # a fresh valid file
        app.poll()  # alice still Active with the same connect time
        [entry] = app.statusJson()["sessions"]
        assert entry["usagePeriodId"] == u1["usagePeriodId"]  # rebuilt from status.json
        assert app.events() == []  # no new session_start

    [backup] = app.backups(fileName)
    assert backup.name == f"{fileName}.corrupt-{stamp}"
    assert backup.read_bytes() == damaged
    notice = f"{fileName} was corrupt and has been reset (backup: {backup.name})"
    assert notice in app.render().warnings

    if fileName == "activity_log.json":
        disconnectA = app.now() + timedelta(seconds=2)
        app.setSessions(alice("Disconnected", disconnectTime=disconnectA))
        app.tick()
        [end] = app.events()  # written to the fresh file; its start stays in the backup
        assert (end["eventType"], end["usagePeriodId"]) == ("session_end", u1["usagePeriodId"])
        assert end["usagePeriodStart"] == u1["usagePeriodStart"] == iso(C1)


# --- REQ AC-19 ------------------------------------------------------------------------------


@pytest.mark.parametrize("period", ["exact", "approximate"])
def test_ac19_an_ongoing_usage_period_survives_a_restart(app, period):
    connectA = T0 + timedelta(seconds=2) if period == "exact" else None
    app.start()
    app.poll()
    app.setSessions(alice(connectTime=connectA))
    app.tick()
    app.tick()
    [before] = app.statusJson()["sessions"]
    eventsBefore = app.events()
    rawBefore = app.path("activity_log.json").read_bytes()

    app.stop()
    app.clock.advance(600)  # she stays connected while the app is down
    app.start()
    app.poll()

    [after] = app.statusJson()["sessions"]
    for key in ("usagePeriodId", "usagePeriodStart", "durationApproximate"):
        assert after[key] == before[key], key
    assert before["durationApproximate"] is (period == "approximate")
    assert app.events() == eventsBefore  # no duplicate start, prior events unchanged
    assert app.path("activity_log.json").read_bytes() == rawBefore
    elapsed = (app.now() - parseIso(before["usagePeriodStart"])).total_seconds()
    suffix = " (approx.)" if period == "approximate" else ""
    assert app.render().activeRow("alice")[5] == hms(elapsed) + suffix


# --- REQ AC-20 ------------------------------------------------------------------------------


@pytest.mark.parametrize("daveDisconnectTime", ["in-window", "not-reported", "outside-window"])
def test_ac20_changes_during_downtime_are_reconciled_under_the_blind_rule(app, daveDisconnectTime):
    connectB, connectD = T0 + timedelta(seconds=1), T0 + timedelta(seconds=2)  # Cb, Cd
    bob = osSession(3, "bob", logonTime=LOGON, connectTime=connectB)
    dave = {"logonTime": LOGON, "connectTime": connectD}
    app.start()
    app.poll()
    app.setSessions(bob, osSession(4, "dave", **dave))
    app.tick()
    starts = {e["userName"]: e for e in app.starts()}
    app.tick()
    app.tick()
    lastPoll = app.now()  # P
    assert app.statusJson()["lastSuccessfulPollAt"] == iso(lastPoll)

    app.stop()
    app.clock.advance(3600)
    reported = {
        "in-window": lastPoll + timedelta(minutes=10),
        "not-reported": None,
        "outside-window": connectD - timedelta(minutes=1),
    }[daveDisconnectTime]
    carolConnect = lastPoll + timedelta(minutes=30)
    app.setSessions(  # bob logged off, dave disconnected, carol logged on
        osSession(4, "dave", "Disconnected", disconnectTime=reported, **dave),
        osSession(5, "carol", logonTime=carolConnect, connectTime=carolConnect),
    )
    app.start()
    app.poll()

    ends = {e["userName"]: e for e in app.ends()}
    assert sorted(ends) == ["bob", "dave"]
    for user, cause in (("bob", "logoff"), ("dave", "disconnect")):
        end = ends[user]
        assert end["usagePeriodId"] == starts[user]["usagePeriodId"]
        assert (end["reason"], end["endCause"], end["durationApproximate"]) == (
            "detected_at_startup",
            cause,
            True,
        )
    fallbackB = max(lastPoll, parseIso(starts["bob"]["timestamp"]))
    assert ends["bob"]["periodEnd"] == iso(fallbackB)
    assert ends["bob"]["durationSeconds"] == (fallbackB - connectB).total_seconds()
    fallbackD = max(lastPoll, parseIso(starts["dave"]["timestamp"]))
    daveEnd = reported if daveDisconnectTime == "in-window" else fallbackD
    assert ends["dave"]["periodEnd"] == iso(daveEnd)
    assert ends["dave"]["durationSeconds"] == (daveEnd - connectD).total_seconds()
    newStarts = [e for e in app.starts() if e["userName"] == "carol"]
    assert [(e["reason"], e["usagePeriodStart"]) for e in newStarts] == [
        ("detected_at_startup", iso(carolConnect))
    ]
    page = app.render()
    assert page.badge == "In Use — carol (192.168.1.20)"
    assert [r[0] for r in page.activeRows] == ["carol"]
    assert [r[0] for r in page.disconnectedRows] == ["dave"]


# --- REQ AC-27 ------------------------------------------------------------------------------


def test_ac27_first_poll_fails_on_a_fresh_install(app):
    app.provider.failNext("error", times=3)
    app.start()
    assert app.activityJson() == EMPTY_LOG
    assert app.poll() == POLL_ENUMERATION_FAILED

    doc = app.statusJson()
    assert {k: doc[k] for k in doc if k not in ("updatedAt",)} == {
        "schemaVersion": 1,
        "status": "Unknown",
        "lastKnownStatus": None,
        "lastKnownStatusAt": None,
        "lastSuccessfulPollAt": None,
        "detectionHealthy": False,
        "sessions": [],
    }
    page = app.render()
    assert page.badge == "Unknown"
    assert page.warnings == [
        "Session information is currently unavailable",
        "Last known status: none",
    ]
    app.setSessions(alice())
    app.tick()
    app.tick()
    assert app.activityJson() == EMPTY_LOG  # nothing until the first successful poll

    assert app.tick() == POLL_OK  # reconciliation runs now
    [start] = app.events()
    assert (start["eventType"], start["userName"], start["reason"]) == (
        "session_start",
        "alice",
        "detected_at_startup",
    )
    assert app.statusJson()["status"] == "In Use"


# --- single-instance lock, end to end (pending requirement amendment 1) ---------------------


LOCK_ALICE = dict(ALICE_FILE_ENTRY, logonTime="2026-10-21T08:00:00+09:00")
DATA_FILES = ("config.json", "status.json", "activity_log.json")


def appEnv(sessionsFile: Path) -> dict[str, str]:
    env = dict(os.environ)
    env[launcher.TEST_HOOKS_ENV] = "1"
    env[launcher.TEST_SESSIONS_FILE_ENV] = str(sessionsFile)
    env.pop(launcher.DATA_DIR_ENV, None)
    return env


def appCommand(dataDir: Path, *extra: str) -> list[str]:
    return [sys.executable, "-m", "pc_handoff", "--data-dir", str(dataDir), *extra]


def startApp(dataDir: Path, sessionsFile: Path, children: list) -> subprocess.Popen:
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    process = subprocess.Popen(
        appCommand(dataDir, "--no-server"),
        cwd=EXTERNAL_DIR,
        env=appEnv(sessionsFile),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=flags,
    )
    children.append(process)
    return process


def hardKill(process: subprocess.Popen) -> None:
    """TerminateProcess the whole tree (a venv python.exe may be a launcher with a child)."""
    if process.poll() is None:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                timeout=30,
            )
        else:  # pragma: no cover - exercised off Windows
            process.kill()
    process.wait(15)


def pollsCompleted(dataDir: Path) -> int:
    try:
        text = (dataDir / "logs" / "app.log").read_text(encoding="utf-8")
    except OSError:
        return 0
    return sum('"message": "poll completed"' in line for line in text.splitlines())


def fingerprint(dataDir: Path) -> dict[str, str]:
    return {n: hashlib.sha256((dataDir / n).read_bytes()).hexdigest() for n in DATA_FILES}


@pytest.fixture
def children():
    """Every child started by the test; whatever happens, each process tree is killed."""
    started: list[subprocess.Popen] = []
    yield started
    for process in started:
        hardKill(process)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()


CHILD_TIMEOUT_SECONDS = 60
# Runs launcher.main in a child and reports how long main() itself took, so interpreter
# start-up and imports are excluded (Critic TASK-011 S6). Prints "MAIN_SECONDS <s>" last.
TIMED_MAIN = "; ".join(
    (
        "import sys, time",
        "from pc_handoff import launcher",
        "started = time.monotonic()",
        "code = launcher.main(sys.argv[1:])",
        "print(f'MAIN_SECONDS {time.monotonic() - started:.3f}', file=sys.stderr)",
        "sys.exit(code)",
    )
)


def runChild(command: list[str], env: dict[str, str], children: list) -> tuple[int, bytes]:
    """Run ``command`` to completion; on a hang the whole tree is killed (Critic TASK-011
    S7: ``subprocess.run(timeout=...)`` would kill only a venv launcher, not its child)."""
    process = subprocess.Popen(
        command, cwd=EXTERNAL_DIR, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    children.append(process)
    try:
        _, stderr = process.communicate(timeout=CHILD_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        hardKill(process)
        pytest.fail(f"{command} did not finish within {CHILD_TIMEOUT_SECONDS} s")
    return process.returncode, stderr


def mainSeconds(stderr: bytes) -> float:
    [line] = [x for x in stderr.decode("utf-8", "replace").splitlines() if x.startswith("MAIN_")]
    return float(line.split()[1])


@pytest.mark.slow
def test_lock_second_instance_exits_3_and_a_stale_lock_never_blocks_the_next_start(
    tmp_path, children, checkedDataDirs
):
    dataDir = tmp_path / "data"
    dataDir.mkdir()
    checkedDataDirs.append(dataDir)
    # A long poll interval: after its first poll the running instance does not rewrite
    # status.json, so "data files byte-identical" is meaningful while it runs.
    config = {"schemaVersion": 1, "sessionPollIntervalSeconds": 60}
    (dataDir / "config.json").write_text(json.dumps(config), encoding="utf-8")
    sessionsFile = tmp_path / "sessions.json"
    sessionsFile.write_text(json.dumps([LOCK_ALICE]), encoding="utf-8")
    env = appEnv(sessionsFile)

    first = startApp(dataDir, sessionsFile, children)
    assert waitUntil(lambda: pollsCompleted(dataDir) >= 1, 30), "the first instance never polled"
    assert statusOrNone(dataDir) == "In Use"
    before = fingerprint(dataDir)

    # A second live instance: the lock check and a full start both exit 3 and touch nothing.
    started = time.monotonic()
    code, stderr = runChild(appCommand(dataDir, "--check-lock-only"), env, children)
    checkSeconds = time.monotonic() - started
    assert code == 3, stderr
    # This wall time includes starting an interpreter (about 0.3 s idle, 2.3 s once under
    # full-suite load), so it gets a flake-free 5 s; the 2 s of amendment 1 is asserted on
    # the second instance's own main() below.
    assert checkSeconds < 5, checkSeconds
    code, stderr = runChild(appCommand(dataDir, "--no-server"), env, children)
    assert code == 3, stderr
    assert b"another PC Handoff instance holds" in stderr
    # Amendment 1 / ADR-004 outcome: a second instance gives up within 2 s, measured inside
    # the child from the start of launcher.main() to its exit code (3 lock attempts 200 ms
    # apart, the app.log line and the stderr line), for a full start and for the probe.
    for option in ("--no-server", "--check-lock-only"):
        timed = [sys.executable, "-c", TIMED_MAIN, "--data-dir", str(dataDir), option]
        code, stderr = runChild(timed, env, children)
        assert code == 3, stderr
        assert mainSeconds(stderr) < 2, (option, stderr)
    assert fingerprint(dataDir) == before
    assert first.poll() is None  # the first instance is unaffected

    # A hard kill leaves the lock file behind, but never a stale lock.
    hardKill(first)
    assert (dataDir / "pc-handoff.lock").exists()
    code, stderr = runChild(appCommand(dataDir, "--check-lock-only"), env, children)
    assert code == 0, stderr

    polls = pollsCompleted(dataDir)
    third = startApp(dataDir, sessionsFile, children)
    assert waitUntil(lambda: pollsCompleted(dataDir) > polls, 30), "the restart never polled"
    [start] = [e for e in readJson(dataDir / "activity_log.json")["events"]]
    [entry] = readJson(dataDir / "status.json")["sessions"]
    assert entry["usagePeriodId"] == start["usagePeriodId"]  # continued after the crash
    if sys.platform == "win32":
        third.send_signal(signal.CTRL_BREAK_EVENT)
    else:  # pragma: no cover - exercised off Windows
        third.send_signal(signal.SIGINT)
    assert third.wait(30) == 0
