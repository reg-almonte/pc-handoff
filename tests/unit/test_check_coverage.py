"""Tests for tools/check_coverage.py (TASK-001 AC-5)."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "tools" / "check_coverage.py"


def loadScriptModule() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_coverage", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checkCoverage = loadScriptModule()


def writeReport(
    path: Path, coveredLines: int, statements: int, coveredBranches: int, branches: int
) -> Path:
    path.write_text(
        json.dumps(
            {
                "meta": {"version": "7.6.0", "branch_coverage": True},
                "files": {},
                "totals": {
                    "covered_lines": coveredLines,
                    "num_statements": statements,
                    "covered_branches": coveredBranches,
                    "num_branches": branches,
                    "percent_covered": 0.0,
                },
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    ("lineCovered", "branchCovered", "expectedExit", "expectedLine", "expectedBranch"),
    [
        (90, 80, 1, "90.00%", "80.00%"),
        (80, 90, 1, "80.00%", "90.00%"),
        (86, 86, 0, "86.00%", "86.00%"),
        (85, 90, 1, "85.00%", "90.00%"),
    ],
)
def test_ac05_line_and_branch_gated_separately(
    tmp_path: Path,
    lineCovered: int,
    branchCovered: int,
    expectedExit: int,
    expectedLine: str,
    expectedBranch: str,
) -> None:
    report = writeReport(tmp_path / "coverage.json", lineCovered, 100, branchCovered, 100)
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), str(report)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == expectedExit, result.stdout + result.stderr
    assert f"Line coverage: {expectedLine}" in result.stdout
    assert f"Branch coverage: {expectedBranch}" in result.stdout


def test_passes_just_above_threshold(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    report = writeReport(tmp_path / "c.json", 8501, 10000, 8501, 10000)
    assert checkCoverage.main([str(report)]) == 0
    assert "Coverage gate passed" in capsys.readouterr().out


def test_reports_both_failures(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    report = writeReport(tmp_path / "c.json", 50, 100, 50, 100)
    assert checkCoverage.main([str(report)]) == 1
    assert "line and branch coverage must be > 85.0%" in capsys.readouterr().out


def test_missing_file_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert checkCoverage.main([str(tmp_path / "absent.json")]) == 1
    assert "cannot read" in capsys.readouterr().out


def test_default_path_is_coverage_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    writeReport(tmp_path / "coverage.json", 90, 100, 90, 100)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["check_coverage.py"])
    assert checkCoverage.main() == 0
    assert "Line coverage: 90.00%" in capsys.readouterr().out


def test_zero_statements_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    report = writeReport(tmp_path / "c.json", 0, 0, 0, 0)
    assert checkCoverage.main([str(report)]) == 1
    assert "num_statements is 0" in capsys.readouterr().out


def test_zero_branches_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    report = writeReport(tmp_path / "c.json", 90, 100, 0, 0)
    assert checkCoverage.main([str(report)]) == 1
    assert "num_branches is 0" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("{not json", "not valid JSON"),
        ("[]", "no 'totals' object"),
        ('{"totals": 5}', "no 'totals' object"),
        ('{"totals": {"num_statements": 10}}', "totals.covered_lines is missing"),
        (
            '{"totals": {"covered_lines": true, "num_statements": 10}}',
            "totals.covered_lines is missing or not an integer",
        ),
        (
            '{"totals": {"covered_lines": 9, "num_statements": "10"}}',
            "totals.num_statements is missing or not an integer",
        ),
    ],
)
def test_malformed_reports_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: str, message: str
) -> None:
    report = tmp_path / "c.json"
    report.write_text(content, encoding="utf-8")
    assert checkCoverage.main([str(report)]) == 1
    assert message in capsys.readouterr().out
