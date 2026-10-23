"""REQ-001 end to end: configuration (TASK-011; REQ AC-14, AC-15)."""

import json
import logging
import random
import time
from typing import Any

import pytest
import streamlit

from pc_handoff.app_logging import LOGGER_NAME
from pc_handoff.clock import SystemClock
from pc_handoff.domain.models import PcStatus
from pc_handoff.persistence import config_store
from pc_handoff.persistence.config_store import Config
from pc_handoff.runtime.poller import Poller
from tests.acceptance.conftest import AcceptanceApp
from tests.fakes.builders import osSession

# Deliberately not the default 10: a page or runtime that ignored config.json and used a
# hard-coded or default interval must not pass by coincidence (Critic TASK-010 BLOCKING-1).
REFRESH_SECONDS = 7
EDITED_REFRESH_SECONDS = 30


@pytest.fixture
def fragmentSpy(monkeypatch) -> list[Any]:
    """The ``run_every`` of every ``st.fragment`` the page registers."""
    calls: list[Any] = []
    realFragment = streamlit.fragment

    def spy(func=None, *, run_every=None):
        calls.append(run_every)
        return realFragment(func, run_every=run_every)

    monkeypatch.setattr(streamlit, "fragment", spy)
    return calls


@pytest.fixture
def configReads(monkeypatch) -> list[str]:
    """Every ``config.json`` read through the config store."""
    reads: list[str] = []
    realRead = config_store.readBytes

    def spy(path):
        reads.append(path.name)
        return realRead(path)

    monkeypatch.setattr(config_store, "readBytes", spy)
    return reads


def waitUntil(predicate, timeout: float, step: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


@pytest.mark.slow
def test_ac14_dashboard_refresh_interval_is_the_startup_value_by_composition(
    tmp_path, checkedDataDirs, fragmentSpy, configReads
):
    """REQ AC-14 is proved by composition (Critic MINOR-8; TASK-011 AC-6).

    The end-to-end bound, a change visible on an open dashboard within
    ``sessionPollIntervalSeconds`` + ``dashboardRefreshIntervalSeconds`` + 2 s (17 s with the
    defaults 5 + 10 + 2), is not observed in one test, because ``AppTest`` does not drive
    ``st.fragment(run_every=...)`` timers in real time. It is the sum of the parts asserted
    here, with the production poller on the real clock and a non-default refresh interval
    (7 s, so the bound in this configuration is 5 + 7 + 2 = 14 s):

    1. an OS change reaches the in-memory snapshot (which the page renders) within
       ``sessionPollIntervalSeconds`` + 2 s, with the real 5 s interval;
    2. the status fragment is registered with ``run_every`` equal to the startup
       ``dashboardRefreshIntervalSeconds`` and each of its runs renders the current snapshot,
       with a "Last updated" time that advances on every run;
    3. editing ``config.json`` while the application runs changes neither (2) nor the poll
       interval, and ``config.json`` is read once; only a restart applies the edit.

    (1) + (2) bound the latency by poll interval + refresh interval + 2 s. The real-time
    observation (an RDP logon appearing on an untouched dashboard within 17 s) is a step of
    ``docs/manual-acceptance.md``.
    """
    dataDir = tmp_path / "data"
    dataDir.mkdir()
    checkedDataDirs.append(dataDir)
    configPath = dataDir / "config.json"
    configPath.write_text(
        json.dumps({"schemaVersion": 1, "dashboardRefreshIntervalSeconds": REFRESH_SECONDS}),
        encoding="utf-8",
    )
    app = AcceptanceApp(dataDir, clock=SystemClock()).start()
    runtime = app.runtime
    assert runtime.config.dashboardRefreshIntervalSeconds == REFRESH_SECONDS
    assert runtime.config.sessionPollIntervalSeconds == 5  # the production default
    assert configReads == ["config.json"]
    logger = logging.getLogger(f"{LOGGER_NAME}.runtime.poller")
    poller = Poller(runtime, runtime.source, app.interval, SystemClock(), logger)
    poller.start()
    try:
        assert waitUntil(lambda: runtime.stateStore.current().status is PcStatus.AVAILABLE, 3)
        page = app.render()
        assert page.badge == "Available"
        assert fragmentSpy == [REFRESH_SECONDS]  # (2) the startup value reaches run_every
        firstUpdated = page.lastUpdated

        # (1) a user logs on via RDP at a random moment between two polls.
        time.sleep(random.uniform(1.1, 4.9))
        app.setSessions(osSession(3, "bob", connectTime=SystemClock().now()))
        changedAt = time.monotonic()
        assert waitUntil(lambda: runtime.stateStore.current().status is PcStatus.IN_USE, 8)
        assert time.monotonic() - changedAt <= app.interval + 2

        # (2) the fragment's next run shows the change, and "Last updated" has advanced.
        page = app.rerender()
        assert page.badge == "In Use" and page.activeRows[0][0] == "bob"
        assert page.lastUpdated != firstUpdated
        assert fragmentSpy == [REFRESH_SECONDS, REFRESH_SECONDS]

        # (3) editing config.json while running changes nothing until a restart.
        edited = {
            "schemaVersion": 1,
            "dashboardRefreshIntervalSeconds": EDITED_REFRESH_SECONDS,
            "sessionPollIntervalSeconds": 2,
        }
        configPath.write_text(json.dumps(edited), encoding="utf-8")
        app.rerender()
        app.render()  # a new browser session as well
        assert fragmentSpy[2:] == [REFRESH_SECONDS, REFRESH_SECONDS]
        assert runtime.config.dashboardRefreshIntervalSeconds == REFRESH_SECONDS
        assert poller.intervalSeconds == 5
        assert configReads == ["config.json"]
    finally:
        assert poller.stop()

    app.restart()  # only a restart applies the edit
    assert app.runtime.config.dashboardRefreshIntervalSeconds == EDITED_REFRESH_SECONDS
    app.render()
    assert fragmentSpy[-1] == EDITED_REFRESH_SECONDS


def test_ac15_invalid_config_values_fall_back_per_field(app):
    app.dataDir.mkdir(parents=True)
    document = {
        "schemaVersion": 1,
        "dashboardRefreshIntervalSeconds": 2,
        "sessionPollIntervalSeconds": "fast",
        "dashboardPort": 80,
        "foo": "bar",
    }
    raw = json.dumps(document, indent=2).encode("utf-8")
    app.path("config.json").write_bytes(raw)

    app.start()
    assert app.runtime.config == Config(
        dashboardRefreshIntervalSeconds=10, sessionPollIntervalSeconds=5, dashboardPort=8501
    )
    app.poll()
    app.tick()

    expected = [
        "dashboardRefreshIntervalSeconds: 2 out of range 5-300; default 10 applied",
        "sessionPollIntervalSeconds: wrong type (expected integer); default 5 applied",
        "dashboardPort: 80 out of range 1024-65535; default 8501 applied",
        "foo: unknown setting ignored",
    ]
    page = app.render()
    assert page.warnings == [f"Configuration (config.json): {w}" for w in expected]
    logged = [
        line["message"]
        for line in app.appLog()
        if line["level"] == "WARNING" and line["message"].startswith("config.json: ")
    ]
    assert logged == [f"config.json: {w}" for w in expected]

    app.restart()
    app.tick()
    assert app.path("config.json").read_bytes() == raw  # never renamed, backed up or rewritten
    assert app.backups("config.json") == []
