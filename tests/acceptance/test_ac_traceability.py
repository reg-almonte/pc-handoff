"""Traceability meta-test: every REQ-001 AC has a task AC and an end-to-end test (TASK-011).

- **What pytest really collects** (Critic TASK-011 S1). A child ``pytest --collect-only``
  run over ``tests/acceptance`` reports every collected item and its effective markers:
  - each REQ AC 1 to 27 has exactly one collected ``test_acNN_<slug>`` function, in the
    module the TASK-011 spec assigns;
  - no ``test_acNN`` carries ``skip``, ``skipif`` or ``xfail``, at function, class or module
    level, except ``test_ac13``, which is skipped with the reason "manual: see
    docs/manual-acceptance.md".
- **What collection cannot see** (AST): a ``pytest.skip``, ``pytest.xfail`` or
  ``pytest.importorskip`` call inside a ``test_acNN`` body, and a ``test_acNN`` defined
  anywhere but at module top level, for example nested in a helper, where it never runs.
- **The task specs.** Every task spec's AC headings are parsed for their ``[REQ AC-n ...]``
  tags:
  - each of the 27 REQ ACs is covered by at least one AC of TASK-001 to TASK-010;
  - TASK-011's own tags cover all 27 (AC-1 to AC-27 except AC-13, plus AC-13 in its
    manual-checklist AC).

The specs live in the factory repository, outside ``external/``, which is its own
repository. The spec checks are skipped only when there is no factory repository at all
(``../.sdlc`` absent: ``external/`` checked out on its own). When ``../.sdlc`` exists, a
missing or renamed REQ-001 spec directory fails (Critic TASK-011 S2).
"""

import ast
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.acceptance.conftest import EXTERNAL_DIR, MANUAL_REASON

ACCEPTANCE_DIR = Path(__file__).resolve().parent
FACTORY_DIR = EXTERNAL_DIR.parent / ".sdlc"
SPEC_DIR = FACTORY_DIR / "specs" / "REQ-001-pc-status-monitoring"
REQ_ACS = set(range(1, 28))
MANUAL_ONLY = {13}
TEST_NAME = re.compile(r"test_ac(\d{2})_[a-z0-9_]+")
SKIPPING_MARKS = ("skip", "skipif", "xfail")
SKIPPING_CALLS = ("skip", "xfail", "importorskip")

# The TASK-011 spec's module table (AC-13 is manual and sits with AC-21's manual p95 part).
EXPECTED_MODULE = {
    **dict.fromkeys((1, 2, 3, 4, 5, 6, 7, 8, 22, 23, 24, 25, 26), "test_detection_and_status"),
    **dict.fromkeys((9, 10), "test_fallbacks"),
    **dict.fromkeys((11, 12), "test_outage"),
    **dict.fromkeys((14, 15), "test_config"),
    **dict.fromkeys((16, 17, 18, 19, 20, 27), "test_files_and_restart"),
    **dict.fromkeys((13, 21), "test_concurrency_and_timing"),
}

# Run in a child process: pytest's own collection, printing one JSON line per item.
COLLECT_SCRIPT = """
import json, sys
import pytest

class Dump:
    def pytest_collection_modifyitems(self, items):
        for item in items:
            marks = [
                [m.name, str(m.kwargs.get("reason", ""))]
                for m in item.iter_markers()
                if m.name in ("skip", "skipif", "xfail")
            ]
            row = {"nodeid": item.nodeid, "name": getattr(item, "originalname", item.name),
                   "marks": marks}
            print("ITEM " + json.dumps(row), flush=True)

args = ["--collect-only", "-q", "--no-cov", "-p", "no:cacheprovider", sys.argv[1]]
sys.exit(pytest.main(args, plugins=[Dump()]))
"""


def collectedItems(directory: Path = ACCEPTANCE_DIR) -> list[dict[str, Any]]:
    """Every item pytest collects in ``directory``, with its skip/skipif/xfail markers."""
    result = subprocess.run(
        [sys.executable, "-c", COLLECT_SCRIPT, str(directory)],
        cwd=EXTERNAL_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    return [json.loads(line[5:]) for line in result.stdout.splitlines() if line.startswith("ITEM ")]


@pytest.fixture(scope="module")
def acTests() -> dict[int, dict[str, Any]]:
    """``{AC number: {"module", "name", "marks"}}``, one entry per collected function
    (parametrized cases folded); several functions for one AC fail here."""
    functions: dict[tuple[str, str], list[list[str]]] = {}
    for item in collectedItems():
        name = item["name"]
        if not name.startswith("test_ac") or TEST_NAME.fullmatch(name) is None:
            continue
        module = Path(item["nodeid"].split("::")[0]).stem
        functions.setdefault((module, name), [])
        for mark in item["marks"]:
            if mark not in functions[(module, name)]:
                functions[(module, name)].append(mark)
    found: dict[int, list[dict[str, Any]]] = {}
    for (module, name), marks in functions.items():
        number = int(TEST_NAME.fullmatch(name).group(1))
        found.setdefault(number, []).append({"module": module, "name": name, "marks": marks})
    for number, tests in found.items():
        assert len(tests) == 1, (number, tests)
    return {number: tests[0] for number, tests in found.items()}


def acFunctionNodes(path: Path) -> tuple[list[ast.FunctionDef], list[str]]:
    """The top-level ``test_ac*`` functions of ``path``, and every ``test_ac*`` def that is
    not top level (nested or in a class)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    topLevel = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name.startswith("test_ac")
    ]
    everywhere = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name.startswith("test_ac")
    ]
    nested = [name for name in everywhere if name not in {n.name for n in topLevel}]
    return topLevel, nested


def skippingCalls(function: ast.FunctionDef) -> list[str]:
    """``pytest.skip`` / ``pytest.xfail`` / ``pytest.importorskip`` calls in a body."""
    return [
        node.func.attr
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in SKIPPING_CALLS
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "pytest"
    ]


def acceptanceSourceProblems(directory: Path = ACCEPTANCE_DIR) -> list[str]:
    problems: list[str] = []
    for path in sorted(directory.glob("test_*.py")):
        topLevel, nested = acFunctionNodes(path)
        problems += [f"{path.name}: {name} is not a top-level function" for name in nested]
        for function in topLevel:
            if TEST_NAME.fullmatch(function.name) is None:
                problems.append(f"{path.name}: {function.name} does not follow test_acNN_<slug>")
            calls = skippingCalls(function)
            if calls:
                problems.append(f"{path.name}: {function.name} calls pytest.{calls}")
    return problems


def test_every_req_ac_has_exactly_one_collected_end_to_end_test_in_its_module(acTests):
    assert set(acTests) == REQ_ACS, sorted(REQ_ACS ^ set(acTests))
    for number, test in sorted(acTests.items()):
        assert test["module"] == EXPECTED_MODULE[number], (number, test)


def test_only_ac13_is_skipped_and_it_points_to_the_manual_checklist(acTests):
    for number, test in sorted(acTests.items()):
        expected = [["skip", MANUAL_REASON]] if number in MANUAL_ONLY else []
        assert test["marks"] == expected, (number, test)
    checklist = (EXTERNAL_DIR / "docs" / "manual-acceptance.md").read_text(encoding="utf-8")
    assert "AC-13" in checklist and "AC-21" in checklist


def test_no_ac_test_skips_itself_at_run_time_or_hides_from_collection():
    assert acceptanceSourceProblems() == []


# --- spec traceability ----------------------------------------------------------------------

AC_HEADING = re.compile(r"^### AC-(\d+):(.*)$", re.MULTILINE)
TAG = re.compile(r"\[([^\[\]]*)\]\s*$")
AC_RANGE = re.compile(r"AC-(\d+) to AC-(\d+)")
AC_REF = re.compile(r"AC-(\d+)")


def reqAcsInTag(tag: str) -> set[int]:
    """REQ AC numbers in one heading tag, e.g. ``REQ AC-3, AC-19, Persistence constraint``,
    ``REQ AC-14; Critic MINOR-8`` or ``REQ AC-1 to AC-27 except AC-13``."""
    numbers: set[int] = set()
    for clause in tag.split(";"):
        clause = clause.strip()
        if not clause.startswith("REQ"):
            continue
        included, _, excluded = clause.partition(" except ")
        for low, high in AC_RANGE.findall(included):
            numbers |= set(range(int(low), int(high) + 1))
        numbers |= {int(n) for n in AC_REF.findall(AC_RANGE.sub("", included))}
        numbers -= {int(n) for n in AC_REF.findall(excluded)}
    return numbers


def taskCoverage() -> dict[str, dict[int, set[int]]]:
    """``{task id: {task AC number: REQ AC numbers}}`` from every task spec's AC headings."""
    coverage: dict[str, dict[int, set[int]]] = {}
    for path in sorted((SPEC_DIR / "tasks").glob("TASK-*.md")):
        taskId = path.name[:8]
        headings = AC_HEADING.findall(path.read_text(encoding="utf-8-sig"))
        assert headings, f"{path.name} has no AC headings"
        coverage[taskId] = {}
        for number, title in headings:
            tag = TAG.search(title.strip())
            coverage[taskId][int(number)] = reqAcsInTag(tag.group(1)) if tag else set()
    return coverage


requiresSpecs = pytest.mark.skipif(
    not FACTORY_DIR.is_dir(),
    reason="no factory repository (../.sdlc absent: external/ checked out on its own)",
)


@requiresSpecs
def test_the_req001_spec_directory_exists_in_the_factory_repository():
    # Inside the factory repository a moved or renamed spec directory must fail, never skip
    # the traceability checks (Critic TASK-011 S2).
    assert (SPEC_DIR / "requirement.md").is_file(), SPEC_DIR
    assert sorted(p.name[:8] for p in (SPEC_DIR / "tasks").glob("TASK-*.md")), SPEC_DIR


def test_the_tag_parser_reads_every_tag_form():
    assert reqAcsInTag("REQ AC-11, Logging constraint") == {11}
    assert reqAcsInTag("REQ AC-3, AC-19, Persistence constraint") == {3, 19}
    assert reqAcsInTag("REQ AC-14; Critic MINOR-8") == {14}
    assert reqAcsInTag("REQ section 1, section 4, AC-2; Critic iteration-2 BLOCKING-1") == {2}
    assert reqAcsInTag("REQ AC-1 to AC-27 except AC-13") == REQ_ACS - {13}
    assert reqAcsInTag("Critic BLOCKING-1, ADR-007 R11") == set()
    assert reqAcsInTag("Testing constraint; Critic MINOR-5") == set()


@requiresSpecs
def test_the_requirement_has_27_acs():
    text = (SPEC_DIR / "requirement.md").read_text(encoding="utf-8-sig")
    assert {int(n) for n, _ in AC_HEADING.findall(text)} == REQ_ACS
    assert "acceptance_criteria_count: 27" in text


@requiresSpecs
def test_every_req_ac_is_covered_by_a_task_ac_of_tasks_001_to_010():
    coverage = taskCoverage()
    assert sorted(coverage) == [f"TASK-{n:03d}" for n in range(1, 12)]
    covered: dict[int, list[str]] = {}
    for taskId, acs in coverage.items():
        if taskId == "TASK-011":
            continue
        for taskAc, reqAcs in acs.items():
            for reqAc in reqAcs:
                covered.setdefault(reqAc, []).append(f"{taskId} AC-{taskAc}")
    assert set(covered) == REQ_ACS, sorted(REQ_ACS - set(covered))


@requiresSpecs
def test_task_011_traces_all_27_req_acs_with_ac13_manual():
    acs = taskCoverage()["TASK-011"]
    assert acs[1] == REQ_ACS - MANUAL_ONLY  # the end-to-end suite
    assert acs[5] >= MANUAL_ONLY  # the manual checklist
    assert set().union(*acs.values()) == REQ_ACS
