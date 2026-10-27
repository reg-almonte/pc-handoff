"""The dashboard page run headless with Streamlit's AppTest (TASK-010, ADR-008).

A stub runtime (startup config, a ``StateStore`` holding a crafted snapshot and a
``FakeClock``) is registered through ``registry.registerRuntime`` before ``at.run()``; a few
tests use the real runtime from ``launcher.buildRuntime``. Every test asserts element values.
"""

import json
import re
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import streamlit
from streamlit.testing.v1 import AppTest

from pc_handoff import launcher
from pc_handoff.detection.provider import ConnectionType
from pc_handoff.domain.models import PcStatus
from pc_handoff.persistence.config_store import Config
from pc_handoff.persistence.notices import StartupNotice
from pc_handoff.runtime import registry
from pc_handoff.runtime.state_store import MAX_NOTICES, Notice, NoticeOrigin, StateStore
from pc_handoff.ui.view_model import ACTIVE_COLUMNS, DISCONNECTED_COLUMNS
from tests.conftest import FAKE_CLOCK_START
from tests.fakes import FakeClock, FakeSessionProvider
from tests.fakes.builders import osSession, trackedActive, trackedDisconnected
from tests.fakes.dashboard_snapshots import healthySnapshot, notice, unknownSnapshot

SCRIPT = str(launcher.DASHBOARD_SCRIPT)
SRC_DIR = Path(launcher.__file__).resolve().parent
C = FAKE_CLOCK_START
TIMEOUT = 30


class StubStateStore:
    """Exposes only ``current()``: any other access fails the render (snapshot-only page)."""

    __slots__ = ("store",)

    def __init__(self, snapshot) -> None:
        self.store = StateStore()
        if snapshot is not None:
            self.store.publish(snapshot)

    def current(self):
        return self.store.current()

    def publish(self, snapshot) -> None:  # test-side only
        self.store.publish(snapshot)


# Deliberately not the Config() default (10): a page that ignored runtime.config and used a
# hard-coded or default interval must not pass by coincidence (Critic TASK-010 BLOCKING-1).
STUB_REFRESH_SECONDS = 23


class StubRuntime:
    """The three things the page may read: startup config, clock and state store."""

    __slots__ = ("config", "clock", "stateStore")

    def __init__(self, snapshot, now, *, refresh: int = STUB_REFRESH_SECONDS) -> None:
        self.config = Config(dashboardRefreshIntervalSeconds=refresh)
        self.clock = FakeClock(now)
        self.stateStore = StubStateStore(snapshot)


@pytest.fixture(autouse=True)
def cleanRegistry() -> Iterator[None]:
    registry.clearRuntime()
    yield
    registry.clearRuntime()


def run(runtime=None) -> AppTest:
    if runtime is not None:
        registry.registerRuntime(runtime)
    app = AppTest.from_file(SCRIPT, default_timeout=TIMEOUT).run()
    assert not app.exception, [e.value for e in app.exception]
    return app


def unescape(text: str) -> str:
    """Undo ``escapeMarkdown``: what the browser shows for a markdown element."""
    return re.sub(r"\\(.)", r"\1", text)


def shown(elements) -> list[str]:
    return [unescape(e.value) for e in elements]


def rows(app: AppTest, index: int = 0) -> list[list[str]]:
    frame = app.dataframe[index].value
    return frame.astype(str).values.tolist()


def local(dt) -> str:
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def lastUpdated(app: AppTest) -> str:
    [line] = [c for c in shown(app.caption) if c.startswith("Last updated: ")]
    return line


def alice(**overrides: Any):
    fields = {
        "connectTime": C,
        "startEventTimestamp": C,
        "clientName": "ALICE-LAPTOP",
        "clientAddress": "192.168.1.20",
    }
    fields.update(overrides)
    return trackedActive(2, "alice", **fields)


def walk(node) -> Iterator[Any]:
    yield node
    children = getattr(node, "children", None)
    if isinstance(children, dict):
        for child in children.values():
            yield from walk(child)


# --- no runtime / no snapshot ------------------------------------------------------------


def test_without_a_registered_runtime_the_page_says_how_to_start():
    app = run()
    [error] = app.error
    assert error.value == "Start PC Handoff with: python -m pc_handoff"
    assert not app.text and not app.dataframe and not app.success and not app.warning


def test_before_the_first_publish_the_page_waits():
    app = run(StubRuntime(None, C))
    assert shown(app.info) == ["Waiting for the first status update."]
    assert not app.error and not app.warning


# --- AC-1 .. AC-4 ------------------------------------------------------------------------


def test_ac1_in_use_with_full_session_details_and_a_live_duration():
    app = run(StubRuntime(healthySnapshot([alice()], C), C + timedelta(seconds=3725)))
    assert shown(app.success) == []
    assert shown(app.error) == ["[🔴 In Use — alice (192.168.1.20)](#active-sessions)"]
    assert shown(app.subheader) == ["Active sessions"]
    assert list(app.dataframe[0].value.columns) == list(ACTIVE_COLUMNS)
    assert rows(app) == [["alice", "RDP", "ALICE-LAPTOP", "192.168.1.20", local(C), "01:02:05"]]
    assert not app.warning and not app.text


def test_ac2_empty_state_shows_available_and_no_session_lists():
    app = run(StubRuntime(healthySnapshot([], C), C))
    assert shown(app.success) == ["Available"]
    assert app.success[0].icon == "🟢"
    assert [t.value for t in app.text] == ["No active user sessions"]
    assert not app.dataframe
    assert shown(app.subheader) == ["Active sessions"]


def test_ac3_disconnected_sessions_are_listed_separately():
    d = C + timedelta(minutes=30)
    sessions = [
        trackedDisconnected(2, "alice", disconnectedSince=d),
        trackedDisconnected(3, "bob", disconnectedSince=d),
    ]
    app = run(StubRuntime(healthySnapshot(sessions, d), d))
    assert shown(app.success) == ["Available"]
    assert [t.value for t in app.text] == ["No active user sessions"]
    assert shown(app.subheader) == [
        "Active sessions",
        "Logged on but disconnected (not in use)",
    ]
    [table] = app.dataframe
    assert list(table.value.columns) == list(DISCONNECTED_COLUMNS)
    assert rows(app) == [["alice", local(d)], ["bob", local(d)]]


def test_ac3_variant_carol_active_shows_in_use_and_only_carol_as_active():
    d = C + timedelta(minutes=30)
    carol = trackedActive(4, "carol", connectTime=C, startEventTimestamp=C)
    sessions = [
        trackedDisconnected(2, "alice", disconnectedSince=d),
        trackedDisconnected(3, "bob", disconnectedSince=d),
        carol,
    ]
    app = run(StubRuntime(healthySnapshot(sessions, d), d))
    assert shown(app.error) == ["[🔴 In Use — carol (192.168.1.20)](#active-sessions)"]
    assert [r[0] for r in rows(app, 0)] == ["carol"]
    assert [r[0] for r in rows(app, 1)] == ["alice", "bob"]
    assert not app.text


def test_a_null_disconnected_since_shows_unknown():
    bob = replace(trackedDisconnected(3, "bob", disconnectedSince=C), disconnectedSince=None)
    app = run(StubRuntime(healthySnapshot([bob], C), C))
    assert rows(app) == [["bob", "unknown"]]


def test_ac4_missing_client_fields_show_unknown_and_approximate_durations_are_marked():
    erin = trackedActive(
        1,
        "erin",
        connectTime=C,
        startEventTimestamp=C,
        connectionType=ConnectionType.CONSOLE,
        clientName=None,
        clientAddress=None,
    )
    t1 = C + timedelta(minutes=1)
    dave = trackedActive(3, "dave", connectTime=None, startEventTimestamp=t1)
    app = run(StubRuntime(healthySnapshot([erin, dave], t1), t1 + timedelta(seconds=30)))
    erinRow, daveRow = rows(app)
    assert erinRow[:4] == ["erin", "Console", "N/A", "N/A"]
    assert daveRow[4:] == [local(t1), "00:00:30 (approx.)"]


# --- AC-5 .. AC-7: Unknown, fresh install, recovery ---------------------------------------


P = C + timedelta(hours=1, minutes=2, seconds=3)


def unknownRuntime() -> StubRuntime:
    snapshot = unknownSnapshot(
        [alice()], P, lastKnownStatus=PcStatus.IN_USE, failedAt=P + timedelta(seconds=5)
    )
    return StubRuntime(snapshot, P + timedelta(seconds=600))


def test_ac5_unknown_shows_banner_last_known_status_and_frozen_durations():
    runtime = unknownRuntime()
    app = run(runtime)
    assert shown(app.info)[0] == "Unknown"
    assert shown(app.warning) == [
        "Session information is currently unavailable",
        f"Last known status: In Use at {local(P)}",
    ]
    assert shown(app.subheader) == [f"Last known sessions (as of {local(P)})"]
    assert rows(app)[0][5] == "01:02:03"
    runtime.clock.advance(300)  # now = P + 900 s
    app.run()
    assert rows(app)[0][5] == "01:02:03"
    assert lastUpdated(app).startswith(f"Last updated: {local(P + timedelta(seconds=900))}")


def test_ac6_fresh_install_unknown_shows_last_known_status_none():
    app = run(StubRuntime(unknownSnapshot([], None, failedAt=C), C))
    assert shown(app.info)[0] == "Unknown"
    assert shown(app.warning) == [
        "Session information is currently unavailable",
        "Last known status: none",
    ]
    assert not app.dataframe
    assert [t.value for t in app.text] == ["No active user sessions"]


def test_ac7_recovery_removes_the_banner_on_the_next_refresh():
    runtime = unknownRuntime()
    app = run(runtime)
    assert len(app.warning) == 2
    runtime.stateStore.publish(healthySnapshot([alice()], P + timedelta(seconds=600)))
    app.run()
    assert not app.warning
    assert shown(app.error) == ["[🔴 In Use — alice (192.168.1.20)](#active-sessions)"]
    assert shown(app.subheader) == ["Active sessions"]
    assert rows(app)[0][5] == "01:12:03"


# --- AC-8: config warnings and notices ----------------------------------------------------


WARNINGS = (
    "dashboardRefreshIntervalSeconds: 2 out of range 5-300; default 10 applied",
    "sessionPollIntervalSeconds: wrong type (expected integer); default 5 applied",
    "dashboardPort: 80 out of range 1024-65535; default 8501 applied",
)
STATUS_NOTICE = "Status data was reset (backup: status.json.corrupt-20261021T091502)"
LOG_NOTICE = (
    "activity_log.json was corrupt and has been reset "
    "(backup: activity_log.json.corrupt-20261021T091502)"
)


def test_ac8_warnings_and_notices_and_per_session_dismissal():
    first = notice(STATUS_NOTICE, backupName="status.json.corrupt-20261021T091502")
    second = notice(LOG_NOTICE, fileName="activity_log.json")
    snapshot = healthySnapshot([], C, configWarnings=WARNINGS, notices=[first, second])
    runtime = StubRuntime(snapshot, C)
    app = run(runtime)
    configLines = [f"Configuration (config.json): {w}" for w in WARNINGS]
    assert shown(app.warning) == [*configLines, STATUS_NOTICE, LOG_NOTICE]
    for field, default in [
        ("dashboardRefreshIntervalSeconds", 10),
        ("sessionPollIntervalSeconds", 5),
        ("dashboardPort", 8501),
    ]:
        assert any(field in w and f"default {default} applied" in w for w in shown(app.warning))
    assert [b.label for b in app.button if b.label == "Dismiss"] == ["Dismiss", "Dismiss"]

    app.button(key=first.noticeId).click().run()
    assert shown(app.warning) == [*configLines, LOG_NOTICE]
    assert app.session_state["dismissedNotices"] == {first.noticeId}
    app.run()  # stays dismissed across refreshes of this session
    assert shown(app.warning) == [*configLines, LOG_NOTICE]

    other = AppTest.from_file(SCRIPT, default_timeout=TIMEOUT).run()  # a new browser session
    assert shown(other.warning) == [*configLines, STATUS_NOTICE, LOG_NOTICE]


def test_the_page_shows_at_most_the_retained_notices(tmp_path):
    # The runtime keeps only the newest MAX_NOTICES (state_store), so the page is bounded.
    provider = FakeSessionProvider([osSession(2, "alice")])
    runtime = launcher.buildRuntime(tmp_path / "data", provider, FakeClock(C))
    for i in range(MAX_NOTICES + 3):
        storeNotice = StartupNotice("activity_log.json", None, f"notice {i}")
        runtime.notices.append(Notice.fromStoreNotice(storeNotice, NoticeOrigin.RUNTIME))
    runtime.publishState(runtime.trackerState)
    app = run(runtime)
    assert shown(app.warning)[-MAX_NOTICES:] == [f"notice {i}" for i in range(3, MAX_NOTICES + 3)]
    assert len([b for b in app.button if b.label == "Dismiss"]) == MAX_NOTICES


# --- AC-9: refresh ------------------------------------------------------------------------


@pytest.fixture
def fragmentSpy(monkeypatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    realFragment = streamlit.fragment

    def spy(func=None, *, run_every=None):
        calls.append({"run_every": run_every})
        return realFragment(func, run_every=run_every)

    monkeypatch.setattr(streamlit, "fragment", spy)
    return calls


def test_the_stub_interval_differs_from_the_config_default():
    assert Config().dashboardRefreshIntervalSeconds != STUB_REFRESH_SECONDS


def test_by_default_the_stub_interval_reaches_the_fragment(fragmentSpy):
    run(StubRuntime(healthySnapshot([], C), C))
    assert fragmentSpy == [{"run_every": STUB_REFRESH_SECONDS}]


@pytest.mark.parametrize("refresh", [5, 37, 300])
def test_ac9_the_fragment_uses_the_startup_interval_and_last_updated_advances(fragmentSpy, refresh):
    # Non-default intervals only (Config() default is 10), including both range limits.
    runtime = StubRuntime(healthySnapshot([], C), C, refresh=refresh)
    app = run(runtime)
    assert fragmentSpy == [{"run_every": refresh}]
    before = lastUpdated(app)
    assert before == f"Last updated: {local(C)} (data as of {local(C)})"
    runtime.clock.advance(refresh)
    app.run()
    after = lastUpdated(app)
    assert after == (
        f"Last updated: {local(C + timedelta(seconds=refresh))} (data as of {local(C)})"
    )
    assert fragmentSpy == [{"run_every": refresh}, {"run_every": refresh}]


def test_ac9_config_json_changes_after_startup_do_not_change_the_interval(tmp_path, fragmentSpy):
    # The real buildRuntime path: the file's non-default value reaches run_every, and a
    # later edit of config.json on disk does not (REQ AC-14: startup only).
    dataDir = tmp_path / "data"
    dataDir.mkdir()
    config = {"schemaVersion": 1, "dashboardRefreshIntervalSeconds": 15}
    (dataDir / "config.json").write_text(json.dumps(config), encoding="utf-8")
    runtime = launcher.buildRuntime(dataDir, FakeSessionProvider([]), FakeClock(C))
    assert runtime.config.dashboardRefreshIntervalSeconds == 15
    config["dashboardRefreshIntervalSeconds"] = 30
    (dataDir / "config.json").write_text(json.dumps(config), encoding="utf-8")
    app = run(runtime)
    assert fragmentSpy == [{"run_every": 15}]
    app.run()
    assert fragmentSpy == [{"run_every": 15}, {"run_every": 15}]


# --- AC-10: safe rendering and no login ---------------------------------------------------


EVIL_USER = "**bob**<script>x</script>"
EVIL_CLIENT = "[link](javascript:alert(1))\x07`tick`"


def test_ac10_user_derived_text_is_shown_literally_without_control_characters():
    evil = trackedActive(
        5,
        EVIL_USER,
        connectTime=C,
        startEventTimestamp=C,
        clientName=EVIL_CLIENT,
        clientAddress="<b>10.0.0.1</b>",
    )
    gone = trackedDisconnected(6, "_eve_ `x` <i>", disconnectedSince=C)
    evilNotice = notice("**reset** <script>alert(1)</script> [x](javascript:y) `z`")
    snapshot = healthySnapshot(
        [evil, gone],
        C,
        notices=[evilNotice],
        configWarnings=["<img src=x>: unknown setting ignored"],
    )
    app = run(StubRuntime(snapshot, C))
    [activeRow] = rows(app, 0)
    assert activeRow[:4] == [
        EVIL_USER,
        "RDP",
        "[link](javascript:alert(1))`tick`",
        "<b>10.0.0.1</b>",
    ]
    assert rows(app, 1) == [["_eve_ `x` <i>", local(C)]]
    # Markdown-rendering elements carry the escaped text, which displays literally.
    warnings = [w.value for w in app.warning]
    assert "\\*\\*reset\\*\\* \\<script\\>alert\\(1\\)\\<\\/script\\>" in warnings[1]
    assert shown(app.warning) == [
        "Configuration (config.json): <img src=x>: unknown setting ignored",
        "**reset** <script>alert(1)</script> [x](javascript:y) `z`",
    ]
    for value in warnings:
        assert re.search(r"(?<!\\)[*`<>\[\]]", value) is None


def test_bidi_and_zero_width_characters_never_reach_the_page():
    # Critic TASK-010 N1: an RDP client names itself; U+202E would show "ALICE-LAPTOP"
    # reversed as a different name, U+200B / U+2066-U+2069 / U+FEFF hide or reorder text.
    spoof = trackedActive(
        5,
        "⁦mallory⁩",
        connectTime=C,
        startEventTimestamp=C,
        clientName="ALICE-‮POTPAL",
        clientAddress="192.168.1.2​0﻿",
    )
    snapshot = healthySnapshot([spoof], C, notices=[notice("reset\u0085 (backup: x⁧y)")])
    app = run(StubRuntime(snapshot, C))
    assert rows(app)[0][:4] == ["mallory", "RDP", "ALICE-POTPAL", "192.168.1.20"]
    assert shown(app.warning) == ["reset (backup: xy)"]


def test_ac10_no_element_allows_html_and_there_is_no_login():
    first = notice(STATUS_NOTICE)
    app = run(StubRuntime(healthySnapshot([alice()], C, notices=[first]), C))
    elements = list(walk(app._tree))
    assert len(elements) > 10
    for element in elements:
        proto = getattr(element, "proto", None)
        assert not getattr(proto, "allow_html", False), element
        assert not getattr(proto, "unsafe_allow_html", False), element
    assert not app.text_input  # no user name / password prompt
    pageText = " ".join(str(getattr(e, "value", "")) for e in elements).lower()
    for word in ("login", "log in", "sign in", "password"):
        assert word not in pageText


def test_ac10_unsafe_allow_html_is_absent_from_the_source_tree():
    sources = list(SRC_DIR.rglob("*.py"))
    assert any(p.name == "dashboard.py" for p in sources)
    for path in sources:
        assert "unsafe_allow_html" not in path.read_text(encoding="utf-8"), path


# --- snapshot only, never blocks on a poll (REQ AC-21) -------------------------------------


def test_the_page_reads_only_config_clock_and_the_snapshot():
    # The stubs use __slots__: touching anything else (a store, the tracker, the source)
    # raises, which the page would report as a dashboard error.
    app = run(StubRuntime(healthySnapshot([alice()], C), C))
    assert not [e for e in app.error if "Dashboard error" in e.value]
    assert shown(app.error) == ["[🔴 In Use — alice (192.168.1.20)](#active-sessions)"]


def test_rendering_never_triggers_or_waits_for_a_poll(tmp_path):
    provider = FakeSessionProvider([osSession(2, "alice")])
    runtime = launcher.buildRuntime(
        tmp_path / "data", provider, FakeClock(C), enumerationTimeoutSeconds=10
    )
    runtime.pollOnce()
    assert provider.callCount == 1
    release = threading.Event()
    provider.blockNext(release)
    poller = threading.Thread(target=runtime.pollOnce, daemon=True)
    poller.start()
    try:
        deadline = time.monotonic() + 5
        while provider.callCount < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert provider.callCount == 2  # the poll is now stuck inside enumerate()
        started = time.monotonic()
        app = run(runtime)
        app.run()
        elapsed = time.monotonic() - started
        assert shown(app.error) == ["[🔴 In Use — alice (192.168.1.20)](#active-sessions)"]
        assert rows(app)[0][0] == "alice"
        assert provider.callCount == 2  # the page started no poll of its own
        assert elapsed < 3, elapsed
        assert poller.is_alive()
    finally:
        release.set()
        poller.join(timeout=10)
    assert not poller.is_alive()


def test_the_real_runtime_renders_its_published_snapshot(tmp_path):
    provider = FakeSessionProvider([osSession(2, "alice")])
    runtime = launcher.buildRuntime(tmp_path / "data", provider, FakeClock(C))
    app = run(runtime)
    assert any(i.startswith("Unknown") for i in shown(app.info))
    assert shown(app.warning) == [
        "Session information is currently unavailable",
        "Last known status: none",
    ]
    runtime.pollOnce()
    app.run()
    assert shown(app.error) == ["[🔴 In Use — alice (192.168.1.20)](#active-sessions)"]
    assert not app.warning
    assert rows(app)[0][:2] == ["alice", "RDP"]


# --- errors and traceIds -------------------------------------------------------------------


class ExplodingStore:
    def current(self):
        raise RuntimeError("boom")


def test_a_render_error_is_contained_and_logged_with_its_trace_id(capturedAppLog):
    runtime = StubRuntime(healthySnapshot([], C), C)
    app = run(runtime)
    assert not app.error
    runtime.stateStore.store = ExplodingStore()  # type: ignore[assignment]
    traceIds = []
    for _ in range(2):
        app.run()
        assert not app.exception
        # The static config-warning block and the status fragment fail separately.
        assert len(app.error) == 2
        for error in app.error:
            match = re.fullmatch(r"Dashboard error; see app\.log trace ([0-9a-f]{32})", error.value)
            assert match, error.value
            traceIds.append(match.group(1))
    assert len(set(traceIds)) == 4  # one traceId per request part, never reused
    lines = [line for line in capturedAppLog() if line["message"] == "dashboard render failed"]
    assert [(line["level"], line["traceId"]) for line in lines] == [
        ("ERROR", traceId) for traceId in traceIds
    ]
    assert "RuntimeError: boom" in lines[0]["detail"]


def test_a_fragment_error_leaves_the_rest_of_the_page_intact():
    class BrokenClock:
        def now(self):
            raise ValueError("clock broken")

    runtime = StubRuntime(healthySnapshot([], C, configWarnings=WARNINGS[:1]), C)
    runtime.clock = BrokenClock()  # type: ignore[assignment]
    app = run(runtime)
    assert shown(app.warning) == [f"Configuration (config.json): {WARNINGS[0]}"]
    [error] = app.error
    assert error.value.startswith("Dashboard error; see app.log trace ")
    assert not app.success and not app.dataframe


def test_expand_and_collapse_all_controls():
    app = run(StubRuntime(healthySnapshot([alice()], C), C))
    assert app.session_state.get("expander_active_open") is not True

    # Clicking Expand all opens all sections
    expandBtn = [b for b in app.button if b.key == "expandAllSectionsBtn"][0]
    expandBtn.click().run()
    assert app.session_state.get("expander_active_open") is True
    assert app.session_state.get("expander_queue_open") is True
    assert app.session_state.get("expander_notes_open") is True

    # Clicking Collapse all closes all sections
    collapseBtn = [b for b in app.button if b.key == "collapseAllSectionsBtn"][0]
    collapseBtn.click().run()
    assert app.session_state.get("expander_active_open") is False
    assert app.session_state.get("expander_queue_open") is False
    assert app.session_state.get("expander_notes_open") is False
