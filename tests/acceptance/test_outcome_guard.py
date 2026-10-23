"""The run-time outcome guard for the ``test_acNN`` tests (Critic TASK-011 N1).

Collection and the AST cannot see every way a test can skip itself (a fixture or hook that
skips, an aliased ``pytest.skip``, ``raise pytest.skip.Exception``, ``unittest.SkipTest``,
``@unittest.skip``). The guard watches the reports of the tests that actually ran and fails
the session when an acceptance test other than ``test_ac13`` was skipped or xfailed.
"""

import pytest

from tests.acceptance.conftest import MANUAL_REASON, AcOutcomeGuard

MODULE = "tests/acceptance/test_fallbacks.py"


def report(name: str, outcome: str, when: str = "call", reason: str = "", xfail: bool = False):
    longrepr = (MODULE, 1, f"Skipped: {reason}") if outcome == "skipped" else None
    rep = pytest.TestReport(
        nodeid=f"{MODULE}::{name}",
        location=(MODULE, 1, name),
        keywords={},
        outcome=outcome,
        longrepr=longrepr,
        when=when,
    )
    if xfail:
        rep.wasxfail = reason
    return rep


def guardAfter(*reports) -> AcOutcomeGuard:
    guard = AcOutcomeGuard()
    for rep in reports:
        guard.record(rep)
    return guard


AC10 = "test_ac10_missing_connect_time_gives_an_approximate_duration"
AC13 = "test_ac13_dashboard_is_reachable_from_another_lan_machine"


def test_passing_acceptance_tests_and_the_manual_ac13_skip_are_fine():
    guard = guardAfter(
        report(AC10, "passed", "setup"),
        report(AC10, "passed", "call"),
        report(AC10, "passed", "teardown"),
        report(f"{AC10.replace('ac10', 'ac05')}[os-time]", "passed"),
        report(AC13, "skipped", "setup", MANUAL_REASON),
        report(AC13, "passed", "teardown"),
    )
    assert guard.problems == []


@pytest.mark.parametrize("when", ["setup", "call"])
def test_a_skipped_acceptance_test_is_a_problem_whatever_skipped_it(when):
    # setup: an autouse fixture or a runtest_setup hook; call: pytest.skip under any alias,
    # raise pytest.skip.Exception, unittest.SkipTest or @unittest.skip.
    guard = guardAfter(report(AC10, "skipped", when, "hidden"))
    [problem] = guard.problems
    assert AC10 in problem and "skipped" in problem and "hidden" in problem


@pytest.mark.parametrize("outcome", ["skipped", "passed"], ids=["xfailed", "xpassed"])
def test_an_xfail_on_an_acceptance_test_is_a_problem(outcome):
    guard = guardAfter(report(AC10, outcome, "call", "expected", xfail=True))
    [problem] = guard.problems
    assert AC10 in problem and "xfail" in problem


def test_the_manual_ac13_must_be_skipped_for_its_reason_and_never_run():
    assert guardAfter(report(AC13, "skipped", "setup", "something else")).problems
    assert guardAfter(report(AC13, "passed", "setup"), report(AC13, "passed", "call")).problems


def test_other_tests_and_non_ac_names_are_ignored():
    guard = guardAfter(
        report("test_only_ac13_is_skipped_and_it_points_to_the_manual_checklist", "skipped"),
        report("test_lock_second_instance_exits_3", "skipped"),
        report("test_ac1_three_digit_form_is_not_an_ac_test", "skipped"),
    )
    assert guard.problems == []


def test_only_the_tests_that_ran_are_judged():
    # A -k or --lf run reports only the selected tests; deselected ones never reach the
    # guard, so a subset run is judged on what it ran and is not failed for the rest.
    assert guardAfter().problems == []
    assert guardAfter(report(AC10, "passed")).problems == []
