"""Log-friendly summaries of validation results, shared by the stores (TASK-006)."""

from collections.abc import Sequence

from pc_handoff.persistence.schemas import ValidationIssue

MAX_LOGGED_ISSUES = 10


def describeIssues(issues: Sequence[ValidationIssue]) -> str:
    """The first ``MAX_LOGGED_ISSUES`` issues joined by ``"; "``, then how many were left out."""
    shown = "; ".join(str(issue) for issue in issues[:MAX_LOGGED_ISSUES])
    hidden = len(issues) - MAX_LOGGED_ISSUES
    return shown if hidden <= 0 else f"{shown}; and {hidden} more"
