"""Acceptance fixture layer for the REQ-001 end-to-end suite (TASK-011).

Every scenario runs the composed application exactly as the launcher assembles it
(``launcher.buildRuntime``: startup load and reconciliation, the real ``TimedSessionSource``,
tracker and stores) on a temp data directory with real file I/O, a ``FakeSessionProvider``
for the OS sessions and a ``FakeClock`` for detection times. The dashboard is the real page
(``ui/dashboard.py``) run headless through Streamlit's ``AppTest`` against that runtime.

- ``app`` fixture: an ``AcceptanceApp`` that is not started yet, so a test can seed files
  first. ``start()`` attaches the real ``app.log`` handler and builds the runtime; ``poll()``
  runs one ``pollOnce``; ``tick()`` advances the fake clock by the configured poll interval
  and polls (the poller's schedule, driven by hand); ``restart()`` drops the runtime, keeps
  every file and builds a new one; ``render()`` runs the page and returns a ``Page``.
- Teardown (TASK-011 AC-3): every acceptance data directory is checked with
  ``assertPairingAcross`` (the log plus every ``.corrupt-*`` backup) and
  ``assertAllFilesValid`` (each current data file passes its validator, and the activity log
  also passes ``activityDocumentProblems``, the TASK-004 checker that runs the real
  ``validateActivityDocument`` plus key-order and round-trip checks).

Fault note (Critic TASK-008 S5): ``pairingProblemsAcross`` would report TASK-006 accepted
limitation (11) (an exception after ``os.replace`` followed by external damage, leaving one
extra copy of events in a backup) as a second start/end. No acceptance scenario injects
that fault, so the strict check stays valid for every scenario here.
"""

import json
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

from pc_handoff import launcher
from pc_handoff.app_logging import LOGGER_NAME, AppLogFileHandler, configureAppLogging
from pc_handoff.clock import Clock, toIso
from pc_handoff.detection.provider import OsSession
from pc_handoff.domain.models import ActivityEvent
from pc_handoff.persistence.config_store import corruptionReason
from pc_handoff.persistence.schemas import validateActivityDocument, validateStatusDocument
from pc_handoff.runtime import registry
from pc_handoff.runtime.app_runtime import AppRuntime
from pc_handoff.ui.view_model import ACTIVE_COLUMNS, DISCONNECTED_COLUMNS
from tests.conftest import FAKE_CLOCK_START
from tests.fakes import FakeClock, FakeSessionProvider
from tests.fakes.activity_checks import activityDocumentProblems
from tests.fakes.pairing import assertPairingAcross

T0 = FAKE_CLOCK_START  # 2026-10-21T09:00:00+09:00
DASHBOARD_SCRIPT = str(launcher.DASHBOARD_SCRIPT)
APPTEST_TIMEOUT_SECONDS = 30
EXTERNAL_DIR = Path(__file__).resolve().parents[2]
MANUAL_REASON = "manual: see docs/manual-acceptance.md"


# --- small helpers --------------------------------------------------------------------------


def iso(dt: datetime | None) -> str | None:
    """The persisted spelling of a time: ISO 8601 with offset, whole seconds."""
    return None if dt is None else toIso(dt)


def local(dt: datetime) -> str:
    """The dashboard spelling of a time: host-local ``YYYY-MM-DD HH:MM:SS``."""
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def hms(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def readJson(path: Path) -> Any:
    return json.loads(path.read_bytes().decode("utf-8-sig"))


def unescape(text: str) -> str:
    """Undo ``escapeMarkdown``: what the browser shows for a markdown element."""
    return re.sub(r"\\(.)", r"\1", text)


# --- file validity (TASK-011 AC-3) ----------------------------------------------------------


def dataFileProblems(dataDir: Path) -> list[str]:
    """Every validity problem in the current data files of ``dataDir`` (missing files are
    not problems: each scenario asserts which files must exist)."""
    problems: list[str] = []
    config = dataDir / "config.json"
    if config.exists():
        try:
            reason = corruptionReason(readJson(config))
        except ValueError as exc:
            reason = f"cannot be parsed ({exc})"
        if reason is not None:
            problems.append(f"config.json: {reason}")
    status = dataDir / "status.json"
    if status.exists():
        problems += [f"status.json: {i}" for i in validateStatusDocument(readJson(status))]
    log = dataDir / "activity_log.json"
    if log.exists():
        doc = readJson(log)
        issues = validateActivityDocument(doc)
        problems += [f"activity_log.json: {i}" for i in issues]
        if not issues:
            events = [ActivityEvent.fromJson(e) for e in doc["events"]]
            problems += [f"activity_log.json: {p}" for p in activityDocumentProblems(events)]
    return problems


def assertAllFilesValid(dataDir: Path) -> None:
    problems = dataFileProblems(Path(dataDir))
    assert problems == [], problems


def assertDataDirSound(dataDir: Path) -> None:
    """The TASK-011 AC-3 teardown check: pairing across backups plus file validity."""
    if Path(dataDir).exists():
        assertPairingAcross(Path(dataDir))
        assertAllFilesValid(Path(dataDir))


# --- the rendered page ----------------------------------------------------------------------


@dataclass
class Page:
    """The text a browser shows for one dashboard run (markdown escapes undone)."""

    badge: str
    badgeKind: str
    warnings: list[str]
    subheaders: list[str]
    texts: list[str]
    captions: list[str]
    activeRows: list[list[str]]
    disconnectedRows: list[list[str]]
    buttons: list[str] = field(default_factory=list)

    @property
    def lastUpdated(self) -> str:
        [line] = [c for c in self.captions if c.startswith("Last updated: ")]
        return line

    def activeRow(self, user: str) -> list[str]:
        [row] = [r for r in self.activeRows if r[0] == user]
        return row


def pageFrom(at: AppTest) -> Page:
    assert not at.exception, [e.value for e in at.exception]
    badgeButtons = [
        ("error", b.label)
        for b in at.button
        if b.key == "statusInUseBadgeBtn" or "In Use" in b.label
    ]
    rawBadges = [
        (kind, unescape(element.value))
        for kind, elements in (("success", at.success), ("error", at.error), ("info", at.info))
        for element in elements
    ] + badgeButtons
    badges = [
        (kind, re.sub(r"^[🔴🟢]\s*", "", re.sub(r"\[(.*?)\]\(.*?\)", r"\1", text)))
        for kind, text in rawBadges
        if not text.startswith(("Dashboard error", "Start PC Handoff"))
    ]
    assert len(badges) == 1, (badges, rawBadges)
    (badgeKind, badge), activeRows, disconnectedRows = badges[0], [], []
    for frame in at.dataframe:
        columns = tuple(frame.value.columns)
        rows = frame.value.astype(str).values.tolist()
        if columns == ACTIVE_COLUMNS:
            activeRows = rows
        else:
            assert columns == DISCONNECTED_COLUMNS, columns
            disconnectedRows = rows
    return Page(
        badge=badge,
        badgeKind=badgeKind,
        warnings=[unescape(w.value) for w in at.warning],
        subheaders=[unescape(s.value) for s in at.subheader],
        texts=[t.value for t in at.text],
        captions=[unescape(c.value) for c in at.caption],
        activeRows=activeRows,
        disconnectedRows=disconnectedRows,
        buttons=[b.label for b in at.button],
    )


# --- the composed application ---------------------------------------------------------------


class AcceptanceApp:
    """The application as the launcher composes it, driven by hand (see module docstring)."""

    def __init__(
        self,
        dataDir: Path,
        clock: Clock | None = None,
        provider: FakeSessionProvider | None = None,
    ) -> None:
        self.dataDir = Path(dataDir)
        self.clock = clock if clock is not None else FakeClock(T0)
        self.provider = provider if provider is not None else FakeSessionProvider()
        self.enumerationTimeoutSeconds: float | None = None
        self.runtime: AppRuntime | None = None
        self.appTest: AppTest | None = None

    # lifecycle ------------------------------------------------------------------------------

    def start(self) -> "AcceptanceApp":
        """Logging, then the startup load, as the launcher does after taking the lock."""
        configureAppLogging(self.dataDir)
        self.runtime = launcher.buildRuntime(
            self.dataDir,
            self.provider,
            self.clock,
            enumerationTimeoutSeconds=self.enumerationTimeoutSeconds,
        )
        return self

    def restart(self) -> "AcceptanceApp":
        """Stop (the runtime is dropped, every file is kept) and start again."""
        self.stop()
        return self.start()

    def stop(self) -> None:
        registry.clearRuntime()
        self.runtime = None
        self.appTest = None

    # driving --------------------------------------------------------------------------------

    @property
    def interval(self) -> int:
        assert self.runtime is not None, "start() first"
        return self.runtime.config.sessionPollIntervalSeconds

    def setSessions(self, *sessions: OsSession) -> None:
        self.provider.setSessions(list(sessions))

    def poll(self, traceId: str | None = None) -> str:
        assert self.runtime is not None, "start() first"
        return self.runtime.pollOnce(traceId)

    def tick(self, traceId: str | None = None) -> str:
        """The next scheduled poll: advance one poll interval, then poll."""
        self.clock.advance(self.interval)
        return self.poll(traceId)

    def now(self) -> datetime:
        return self.clock.now()

    # files ----------------------------------------------------------------------------------

    def path(self, name: str) -> Path:
        return self.dataDir / name

    def statusJson(self) -> dict[str, Any]:
        return readJson(self.path("status.json"))

    def activityJson(self) -> dict[str, Any]:
        return readJson(self.path("activity_log.json"))

    def events(self) -> list[dict[str, Any]]:
        return self.activityJson()["events"]

    def starts(self) -> list[dict[str, Any]]:
        return [e for e in self.events() if e["eventType"] == "session_start"]

    def ends(self) -> list[dict[str, Any]]:
        return [e for e in self.events() if e["eventType"] == "session_end"]

    def backups(self, name: str) -> list[Path]:
        return sorted(self.dataDir.glob(f"{name}.corrupt-*"))

    def appLog(self) -> list[dict[str, Any]]:
        path = self.dataDir / "logs" / "app.log"
        for handler in logging.getLogger(LOGGER_NAME).handlers:
            handler.flush()
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    # the dashboard --------------------------------------------------------------------------

    def render(self) -> Page:
        """A new browser session opening the dashboard of the current runtime."""
        assert self.runtime is not None, "start() first"
        registry.clearRuntime()
        registry.registerRuntime(self.runtime)
        self.appTest = AppTest.from_file(
            DASHBOARD_SCRIPT, default_timeout=APPTEST_TIMEOUT_SECONDS
        ).run()
        return pageFrom(self.appTest)

    def rerender(self) -> Page:
        """The same browser session refreshing (a fragment run or a rerun)."""
        assert self.appTest is not None, "render() first"
        self.appTest.run()
        return pageFrom(self.appTest)


def closeAppLogHandlers() -> None:
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        if isinstance(handler, AppLogFileHandler):
            logger.removeHandler(handler)
            handler.close()


# --- run-time outcome guard (Critic TASK-011 N1) ---------------------------------------------

AC_TEST_NAME = re.compile(r"test_ac(\d{2})_[a-z0-9_]+(\[.*\])?")
MANUAL_ONLY_AC = 13


class AcOutcomeGuard:
    """Judges the reports of the ``test_acNN`` tests that actually ran.

    A ``test_acNN`` other than ``test_ac13`` that is skipped (in any phase, by any means: a
    mark, a fixture, a hook, ``pytest.skip`` under any alias, ``pytest.skip.Exception``,
    ``unittest.SkipTest``, ``@unittest.skip``) or that carries an xfail outcome is a problem;
    so is a ``test_ac13`` that runs, or is skipped for another reason than the manual one.

    Subset runs (``-k``, ``-m``, ``--lf``, a single file): only reports that reached the
    guard are judged. Deselected or uncollected tests produce no report, so a subset run is
    never failed for what it did not run; the meta-test (``test_ac_traceability.py``) is what
    proves that all 27 exist and are collected.
    """

    def __init__(self) -> None:
        self.problems: list[str] = []

    def record(self, report: pytest.TestReport) -> None:
        match = AC_TEST_NAME.fullmatch(report.nodeid.split("::")[-1])
        if match is None:
            return
        manual = int(match.group(1)) == MANUAL_ONLY_AC
        where = f"{report.nodeid} ({report.when})"
        if hasattr(report, "wasxfail"):
            self.problems.append(f"{where}: xfail outcome ({report.outcome}) on an AC test")
        elif report.skipped and not manual:
            self.problems.append(f"{where}: skipped: {skipReason(report)}")
        elif report.skipped and MANUAL_REASON not in skipReason(report):
            self.problems.append(f"{where}: skipped, but not for '{MANUAL_REASON}'")
        elif manual and report.when == "call":
            self.problems.append(f"{where}: ran; it must be skipped as a manual check")


def skipReason(report: pytest.TestReport) -> str:
    longrepr = report.longrepr
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        return str(longrepr[2]).removeprefix("Skipped: ")
    return str(longrepr)


AC_OUTCOMES = AcOutcomeGuard()


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Called for the items under ``tests/acceptance`` (this conftest's directory)."""
    AC_OUTCOMES.record(report)


@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if AC_OUTCOMES.problems and session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter: Any) -> None:
    if AC_OUTCOMES.problems:
        terminalreporter.section("acceptance outcome guard (Critic TASK-011 N1)", red=True)
        for problem in AC_OUTCOMES.problems:
            terminalreporter.write_line(f"VACUOUS AC TEST: {problem}", red=True)
        terminalreporter.write_line(
            "The session fails: a test_acNN other than test_ac13 must run, not skip or xfail."
        )


@pytest.fixture
def checkedDataDirs() -> Iterator[list[Path]]:
    """Data directories to check at teardown (TASK-011 AC-3), for tests that run the app
    without the ``app`` fixture (launcher threads and subprocesses)."""
    dirs: list[Path] = []
    registry.clearRuntime()
    try:
        yield dirs
    finally:
        registry.clearRuntime()
        closeAppLogHandlers()
    for directory in dirs:
        assertDataDirSound(directory)


@pytest.fixture
def app(tmp_path: Path, checkedDataDirs: list[Path]) -> AcceptanceApp:
    """An unstarted ``AcceptanceApp`` on ``<tmp>/data`` with a ``FakeClock`` at T0."""
    dataDir = tmp_path / "data"
    checkedDataDirs.append(dataDir)
    return AcceptanceApp(dataDir)
