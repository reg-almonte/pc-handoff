"""The operator documentation covers what TASK-011 AC-4 and AC-5 require, and matches the code.

These checks are mechanical, not a review. They catch a missing section, and drift between
the docs and the code: exit codes, config ranges and defaults, schema field names, and poll
outcomes.
"""

import re
from pathlib import Path

import pytest

from pc_handoff import launcher
from pc_handoff.persistence import instance_lock
from pc_handoff.persistence.config_store import SETTINGS
from pc_handoff.persistence.schemas import (
    END_EVENT_SPECS,
    EVENT_SPECS,
    STATUS_SESSION_SPECS,
    STATUS_SPECS,
)
from pc_handoff.runtime.app_runtime import POLL_ENUMERATION_FAILED, POLL_ERROR, POLL_OK
from tests.acceptance.conftest import EXTERNAL_DIR

DOCS = EXTERNAL_DIR / "docs"


def read(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


def flat(text: str) -> str:
    """``text`` with every run of whitespace as one space (phrases may wrap)."""
    return " ".join(text.split())


def section(text: str, heading: str) -> str:
    """The body of the ``## <heading>`` section."""
    match = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    assert match, heading
    return match.group(1)


@pytest.fixture(scope="module")
def operations() -> str:
    return read("operations.md")


def test_operations_has_the_run_command_and_data_directory_options(operations):
    assert "python -m pc_handoff --data-dir" in operations
    assert launcher.DATA_DIR_ENV in operations and "streamlit run" in operations


def test_operations_exit_code_table_matches_the_launcher(operations):
    codes = {
        launcher.EXIT_OK: "Stopped normally",
        launcher.EXIT_FAILED: "Startup error or dashboard server error",
        instance_lock.EXIT_ALREADY_RUNNING: "Another PC Handoff instance",
        instance_lock.EXIT_LOCK_UNAVAILABLE: "lock file or the data directory is unavailable",
        launcher.EXIT_PYTHON_TOO_OLD: "older than 3.11",
        launcher.EXIT_PORT_IN_USE: "dashboard port is already in use",
    }
    assert sorted(codes) == [0, 1, 3, 4, 5, 6]
    rows = dict(re.findall(r"^\| (\d) \| (.*)$", operations, re.M))
    assert sorted(int(code) for code in rows) == sorted(codes)
    for code, words in codes.items():
        assert words in rows[str(code)], (code, rows[str(code)])


def test_exit_6_says_config_json_is_read_first_and_the_other_files_are_untouched(operations):
    # Critic TASK-011 BLOCKING-2: the launcher loads config.json (creating or resetting it)
    # before the port check, then stops before status.json and activity_log.json
    # (launcher.runWithLock; ADR-002 amendment). Row 6, section 5 and L18 must all say so.
    [row6] = re.findall(r"^\| 6 \| (.*)$", operations, re.M)
    for phrase in (
        "stopped after reading `config.json`",
        "created with the defaults if it was missing",
        "backed up and reset if it was corrupt",
        "before `status.json` and `activity_log.json` were read, backed up or created",
        "`PC Handoff starting`",
        "`dashboard port in use`",
        # Critic TASK-011 N3: the list is not exhaustive; config.json's own lines appear too.
        "among others",
        "`config.json was missing and has been created with defaults`",
        "`config.json backed up as config.json.corrupt-...`",
    ):
        assert phrase in row6, phrase
    assert "before any data file" not in flat(operations)
    assert "Only `app.log`" not in flat(operations)
    section5 = flat(section(operations, "5. Dashboard, port and network"))
    assert "A corrupt `config.json` is reset to the defaults before the port is known" in section5
    l18 = flat(section(operations, "11. Known limitations").split("**L18.")[1].split("**L19.")[0])
    assert "`config.json` is reset to the defaults before the port is known" in l18


def test_operations_config_table_matches_the_config_store(operations):
    for name, minimum, maximum, default in SETTINGS:
        row = f"| `{name}` | integer | {minimum}-{maximum} | {default} | Restart required |"
        assert row in operations, row


def test_operations_covers_network_files_lock_logs_and_hooks(operations):
    for phrase in (
        "0.0.0.0",
        "logger.hideWelcomeMessage",
        "cross-origin WebSocket",
        "8501",
        "dual-stack",
        "offset 1",
        "Add-MpPreference -ExclusionPath",
        "pendingEvents",
        "durationMs",
        "eventsInBatch",
        "PC_HANDOFF_ALLOW_TEST_HOOKS",
        "PC_HANDOFF_TEST_SESSIONS_FILE",
        ".corrupt-<yyyyMMddTHHmmss>[-n]",
        "client.toolbarMode",
    ):
        assert phrase in flat(operations), phrase
    for outcome in (POLL_OK, POLL_ENUMERATION_FAILED, POLL_ERROR):
        assert f"`{outcome}`" in operations, outcome


def test_every_known_limitation_cites_its_decision(operations):
    limitations = section(operations, "11. Known limitations")
    entries = re.split(r"^\*\*L\d+\. ", limitations, flags=re.M)[1:]
    assert len(entries) >= 15
    for entry in entries:
        assert re.search(r"ADR-00\d|decisions\.log|Critic|requirement section|\(A7\)", entry), (
            entry[:80]
        )
    for topic in (
        "TASK-006 accepted limitation 11",
        "TASK-008 S4",
        "TASK-009 S6",
        "config.json together with a taken port",
        "Forward step",
        "I1",
        "I2",
        "I3",
        "I4",
        "ADR-007 R3",
        "ADR-007 R10",
        "TASK-010 N1",
        "N2b",
        "TASK-007 S6",
    ):
        assert topic in flat(limitations), topic


def test_data_files_document_every_field_layout_backups_and_value_semantics():
    text = read("data-files.md")
    for spec in STATUS_SPECS + STATUS_SESSION_SPECS + EVENT_SPECS + END_EVENT_SPECS:
        assert f"`{spec.name}`" in text, spec.name
    for name, *_ in SETTINGS:
        assert f"`{name}`" in text, name
    for phrase in (
        "Canonical layout (ADR-003)",
        "refers to event values, not bytes",
        "<name>.corrupt-<yyyyMMddTHHmmss>",
        "Pairing rule",
        "Not checked by design",
        "fromisoformat",
    ):
        assert phrase in flat(text), phrase


def test_the_manual_checklist_covers_its_items_with_a_results_table():
    text = read("manual-acceptance.md")
    for ref in ("AC-13", "AC-14", "AC-21", "A3", "A4", "A9", "A10", "A11", "A12", "R1", "N2b"):
        assert ref in text, ref
    results = section(text, "Results")
    assert "| Check | Validates | Date | Tester | Result (PASS / FAIL) |" in results
    checks = re.findall(r"^## (M\d+)\. ", text, re.M)
    assert len(checks) >= 12
    for check in checks:
        assert re.search(rf"^\| {check}(\.\d)? ", results, re.M), check


def test_the_readme_links_the_three_documents():
    readme = (EXTERNAL_DIR / "README.md").read_text(encoding="utf-8")
    for name in ("operations.md", "data-files.md", "manual-acceptance.md"):
        assert f"(docs/{name})" in readme
        assert Path(DOCS / name).is_file()
