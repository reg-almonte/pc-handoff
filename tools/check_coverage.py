"""Separate line and branch coverage gates (ADR-006, Critic MINOR-5).

coverage.py's ``fail_under`` checks only the combined figure, so this script reads the
``coverage.json`` written by pytest-cov and requires line coverage AND branch coverage to
each be strictly greater than 85.0%.

Usage (from ``external/``)::

    pytest && python tools/check_coverage.py coverage.json

Exit codes: 0 when both gates pass, 1 otherwise (including a missing or unreadable file,
missing totals, zero statements or zero branches).
"""

import json
import sys
from collections.abc import Sequence
from pathlib import Path

THRESHOLD_PERCENT = 85.0
DEFAULT_REPORT = "coverage.json"


class CoverageReportError(Exception):
    """The coverage report is missing, unreadable or incomplete."""


def loadTotals(reportPath: Path) -> dict:
    """Return the ``totals`` object of a coverage.py JSON report."""
    try:
        text = reportPath.read_text(encoding="utf-8")
    except OSError as error:
        raise CoverageReportError(f"cannot read {reportPath}: {error}") from error
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise CoverageReportError(f"{reportPath} is not valid JSON: {error}") from error
    totals = document.get("totals") if isinstance(document, dict) else None
    if not isinstance(totals, dict):
        raise CoverageReportError(f"{reportPath} has no 'totals' object")
    return totals


def percent(totals: dict, coveredKey: str, totalKey: str) -> float:
    """Return ``100 * totals[coveredKey] / totals[totalKey]``; raise if the total is zero."""
    covered = totals.get(coveredKey)
    total = totals.get(totalKey)
    for key, value in ((coveredKey, covered), (totalKey, total)):
        if not isinstance(value, int) or isinstance(value, bool):
            raise CoverageReportError(f"totals.{key} is missing or not an integer")
    if total == 0:
        raise CoverageReportError(f"totals.{totalKey} is 0; nothing was measured")
    return covered * 100 / total


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    reportPath = Path(args[0] if args else DEFAULT_REPORT)
    try:
        totals = loadTotals(reportPath)
        linePercent = percent(totals, "covered_lines", "num_statements")
        branchPercent = percent(totals, "covered_branches", "num_branches")
    except CoverageReportError as error:
        print(f"Coverage gate FAILED: {error}")
        return 1

    print(f"Line coverage: {linePercent:.2f}% | Branch coverage: {branchPercent:.2f}%")
    failures = [
        name
        for name, value in (("line", linePercent), ("branch", branchPercent))
        if not value > THRESHOLD_PERCENT
    ]
    if failures:
        print(
            f"Coverage gate FAILED: {' and '.join(failures)} coverage must be "
            f"> {THRESHOLD_PERCENT:.1f}%"
        )
        return 1
    print(f"Coverage gate passed: line and branch coverage are both > {THRESHOLD_PERCENT:.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
