"""Tests for the activity_log.json validators (TASK-005 AC-8, ADR-005, decisions.log G2/G5).

Golden documents come from the TASK-003 factories (``newStartEvent`` / ``newEndEvent``),
including ends clamped by ADR-007 R11, so the application's own output is proven to pass the
startup check (Critic arch BLOCKING-1, ADR-005 round trip).
"""

import copy
import logging
import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from pc_handoff.domain.models import (
    ActivityEvent,
    EndCause,
    EventReason,
    newEndEvent,
    newStartEvent,
)
from pc_handoff.persistence.schemas import (
    ValidationIssue,
    checkActivityOuter,
    validateActivityDocument,
)
from tests.fakes import activity_checks
from tests.fakes.builders import trackedActive

JST = timezone(timedelta(hours=9))
T0 = datetime(2026, 10, 21, 9, 0, 0, tzinfo=JST)
START_AT = T0 + timedelta(minutes=1)
END_NOW = T0 + timedelta(minutes=30)
PERIOD_END = T0 + timedelta(minutes=29)
LOGGER = logging.getLogger("pc_handoff.tests.schemas")


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def periodPair(
    startAt: datetime | None = START_AT,
    endNow: datetime = END_NOW,
    periodEnd: datetime | None = PERIOD_END,
    **sessionOverrides: Any,
) -> tuple[ActivityEvent, ActivityEvent]:
    """A start and its end, both built through the TASK-003 factories."""
    sessionOverrides.setdefault("connectTime", at(0))
    session = trackedActive(2, "alice", startEventTimestamp=startAt, **sessionOverrides)
    period = session.openPeriod
    assert period is not None
    start = newStartEvent(session, period, EventReason.OBSERVED, startAt)
    end = newEndEvent(
        session,
        period,
        periodEnd,
        EndCause.DISCONNECT,
        EventReason.OBSERVED,
        period.durationApproximate,
        endNow,
        LOGGER,
    )
    return start, end


def document(*events: ActivityEvent, **extra: Any) -> dict[str, Any]:
    return {"schemaVersion": 1, **extra, "events": [e.toJson() for e in events]}


def golden() -> dict[str, Any]:
    start, end = periodPair()
    open2, _ = periodPair(startAt=at(40), endNow=at(50), connectTime=None, clientName=None)
    return document(start, end, open2)


def paths(issues: list[ValidationIssue]) -> list[str]:
    return [issue.path for issue in issues]


# --- golden documents and the round trip ---------------------------------------------------


class TestActivityGolden:
    def test_golden_document_is_valid(self) -> None:
        assert validateActivityDocument(golden()) == []

    def test_empty_log_is_valid(self) -> None:
        """REQ AC-16 / AC-27: ``{"schemaVersion": 1, "events": []}``."""
        assert validateActivityDocument({"schemaVersion": 1, "events": []}) == []

    def test_end_whose_start_is_absent_is_valid(self) -> None:
        """AC-8 / REQ AC-18: the start may live in a .corrupt-* backup."""
        _, end = periodPair()
        assert validateActivityDocument(document(end)) == []

    def test_end_without_start_and_null_period_end_is_valid(self) -> None:
        """Source-2 period closed with nothing known: periodEnd and durationSeconds null."""
        _, end = periodPair(startAt=None, periodEnd=None)
        doc = document(end)
        assert doc["events"][0]["periodEnd"] is None
        assert doc["events"][0]["durationSeconds"] is None
        assert validateActivityDocument(doc) == []

    def test_null_identity_and_client_fields_are_valid(self) -> None:
        """S9 / REQ AC-9: domain, logonTime, clientName, clientAddress, connectTime may be null."""
        start, end = periodPair(
            domain=None, logonTime=None, clientName=None, clientAddress=None, connectTime=None
        )
        doc = document(start, end)
        for key in ("domain", "logonTime", "clientName", "clientAddress", "connectTime"):
            assert doc["events"][1][key] is None
        assert validateActivityDocument(doc) == []

    @pytest.mark.parametrize("cause", list(EndCause))
    @pytest.mark.parametrize("reason", list(EventReason))
    def test_every_end_cause_and_reason_is_valid(
        self, cause: EndCause, reason: EventReason
    ) -> None:
        session = trackedActive(4, "carol", connectTime=at(0), startEventTimestamp=at(1))
        period = session.openPeriod
        assert period is not None
        end = newEndEvent(session, period, at(5), cause, reason, True, at(6), LOGGER)
        assert validateActivityDocument(document(end)) == []

    def test_extra_keys_are_tolerated(self) -> None:
        """ADR-005: extra top-level keys and extra event keys are tolerated."""
        doc = golden()
        doc["note"] = "kept"
        doc["events"][0]["future"] = 1
        assert validateActivityDocument(doc) == []

    def test_utc_and_local_offsets_compare_as_instants(self) -> None:
        """An end written with a different offset is ordered by instant, not by string."""
        start, end = periodPair()
        doc = document(start, end)
        # Same instant as the start, written in UTC: equal instants are allowed.
        doc["events"][1]["timestamp"] = at(1).astimezone(UTC).isoformat()
        assert validateActivityDocument(doc) == []


class TestR11ClampsRoundTrip:
    """decisions.log G2 / ADR-007 R11: the startup check accepts exactly what the clamp makes."""

    def test_backward_clock_step_clamps_timestamp_to_start_and_passes(self) -> None:
        start, end = periodPair(startAt=at(10), endNow=at(4), periodEnd=at(4))
        assert end.timestamp == start.timestamp  # clamped
        assert validateActivityDocument(document(start, end)) == []

    def test_period_end_clamped_to_usage_period_start_gives_zero_duration(self) -> None:
        start, end = periodPair(startAt=at(1), endNow=at(2), periodEnd=at(0) - timedelta(hours=1))
        assert end.periodEnd == at(0)
        assert end.durationSeconds == 0
        assert validateActivityDocument(document(start, end)) == []

    def test_both_clamps_at_once(self) -> None:
        start, end = periodPair(startAt=at(10), endNow=at(-60), periodEnd=at(-60))
        assert (end.timestamp, end.periodEnd, end.durationSeconds) == (at(10), at(0), 0)
        assert validateActivityDocument(document(start, end)) == []

    def test_one_second_before_the_start_is_rejected(self) -> None:
        """The check fires exactly one step past what the clamp can produce."""
        start, end = periodPair(startAt=at(10), endNow=at(4), periodEnd=at(4))
        doc = document(start, end)
        doc["events"][1]["timestamp"] = (at(10) - timedelta(seconds=1)).isoformat()
        assert paths(validateActivityDocument(doc)) == ["$.events[1].timestamp"]


# --- mutations (AC-8, G2, G5) --------------------------------------------------------------


def mutated(mutate) -> dict[str, Any]:
    doc = copy.deepcopy(golden())
    mutate(doc)
    return doc


def setEvent(index: int, key: str, value: Any):
    def mutate(doc: dict[str, Any]) -> None:
        doc["events"][index][key] = value

    return mutate


def delEvent(index: int, key: str):
    def mutate(doc: dict[str, Any]) -> None:
        del doc["events"][index][key]

    return mutate


def duplicateEvent(index: int, newEventId: bool = True):
    def mutate(doc: dict[str, Any]) -> None:
        copied = copy.deepcopy(doc["events"][index])
        if newEventId:
            copied["eventId"] = str(uuid.uuid4())
        doc["events"].append(copied)

    return mutate


def endBeforeStart(doc: dict[str, Any]) -> None:
    doc["events"][1]["timestamp"] = (at(1) - timedelta(seconds=1)).isoformat()


class TestActivityMutations:
    # AC-8: the three invalid mutations.
    def test_missing_end_cause_is_invalid(self) -> None:
        issues = validateActivityDocument(mutated(delEvent(1, "endCause")))
        assert paths(issues) == ["$.events[1].endCause"]

    def test_unknown_reason_is_invalid(self) -> None:
        issues = validateActivityDocument(mutated(setEvent(0, "reason", "guessed")))
        assert paths(issues) == ["$.events[0].reason"]

    def test_two_starts_for_one_period_are_invalid(self) -> None:
        issues = validateActivityDocument(mutated(duplicateEvent(0)))
        assert paths(issues) == ["$.events[3]"]
        assert "second session_start" in issues[0].message

    def test_end_preceding_its_start_is_invalid(self) -> None:
        issues = validateActivityDocument(mutated(endBeforeStart))
        assert paths(issues) == ["$.events[1].timestamp"]
        assert "precedes" in issues[0].message

    def test_end_written_before_its_start_in_the_file_is_still_ordered_by_time(self) -> None:
        """The ordering check compares timestamps, whatever the position in the file."""
        doc = mutated(endBeforeStart)
        doc["events"][0], doc["events"][1] = doc["events"][1], doc["events"][0]
        assert paths(validateActivityDocument(doc)) == ["$.events[0].timestamp"]

    def test_two_ends_for_one_period_are_invalid(self) -> None:
        issues = validateActivityDocument(mutated(duplicateEvent(1)))
        assert paths(issues) == ["$.events[3]"]
        assert "second session_end" in issues[0].message

    def test_byte_identical_duplicate_event_is_a_second_start(self) -> None:
        """ADR-005 fixes the cross-event rules; a repeated eventId is caught as a second start."""
        issues = validateActivityDocument(mutated(duplicateEvent(2, newEventId=False)))
        assert [i.message for i in issues] == ["second session_start for this usagePeriodId"]

    @pytest.mark.parametrize(
        ("mutate", "path"),
        [
            # G2: session_end requires endCause; durationSeconds is an int >= 0 or null.
            (setEvent(1, "endCause", None), "$.events[1].endCause"),
            (setEvent(1, "endCause", "crash"), "$.events[1].endCause"),
            (setEvent(1, "durationSeconds", -1), "$.events[1].durationSeconds"),
            (setEvent(1, "durationSeconds", True), "$.events[1].durationSeconds"),
            (setEvent(1, "durationSeconds", 1740.0), "$.events[1].durationSeconds"),
            (setEvent(1, "durationSeconds", "1740"), "$.events[1].durationSeconds"),
            (delEvent(1, "durationSeconds"), "$.events[1].durationSeconds"),
            (delEvent(1, "periodEnd"), "$.events[1].periodEnd"),
            (setEvent(1, "periodEnd", "2026-10-21T09:29:00"), "$.events[1].periodEnd"),
            # G5: UUIDs and strict ints
            (setEvent(0, "eventId", "1234"), "$.events[0].eventId"),
            (setEvent(0, "eventId", None), "$.events[0].eventId"),
            (setEvent(0, "usagePeriodId", "ABC"), "$.events[0].usagePeriodId"),
            (setEvent(0, "sessionId", True), "$.events[0].sessionId"),
            (setEvent(0, "sessionId", 2.0), "$.events[0].sessionId"),
            (setEvent(0, "sessionId", -2), "$.events[0].sessionId"),
            # field types, enums and nullability
            (setEvent(0, "eventType", "session_pause"), "$.events[0].eventType"),
            (setEvent(0, "timestamp", "2026-10-21T09:01:00"), "$.events[0].timestamp"),
            (setEvent(0, "timestamp", None), "$.events[0].timestamp"),
            (setEvent(0, "userName", ""), "$.events[0].userName"),
            (setEvent(0, "domain", 1), "$.events[0].domain"),
            (setEvent(0, "logonTime", "dawn"), "$.events[0].logonTime"),
            (setEvent(0, "clientName", False), "$.events[0].clientName"),
            (setEvent(0, "clientAddress", 192), "$.events[0].clientAddress"),
            (setEvent(0, "connectionType", "VPN"), "$.events[0].connectionType"),
            (setEvent(0, "connectTime", "x"), "$.events[0].connectTime"),
            (setEvent(0, "usagePeriodStart", None), "$.events[0].usagePeriodStart"),
            (setEvent(0, "durationApproximate", 0), "$.events[0].durationApproximate"),
            (delEvent(0, "clientName"), "$.events[0].clientName"),
        ],
    )
    def test_each_field_rule(self, mutate, path: str) -> None:
        assert path in paths(validateActivityDocument(mutated(mutate)))

    def test_non_object_event(self) -> None:
        doc = golden()
        doc["events"].append("session_start")
        assert paths(validateActivityDocument(doc)) == ["$.events[3]"]

    def test_invalid_events_are_left_out_of_the_pairing_check(self) -> None:
        """An event with a broken usagePeriodId or timestamp reports its own issue only."""
        doc = mutated(duplicateEvent(0))
        doc["events"][3]["timestamp"] = "garbage"
        assert paths(validateActivityDocument(doc)) == ["$.events[3].timestamp"]

    @pytest.mark.parametrize(
        ("doc", "path"),
        [
            ([], "$"),
            (None, "$"),
            ({"events": []}, "$.schemaVersion"),
            ({"schemaVersion": 2, "events": []}, "$.schemaVersion"),
            ({"schemaVersion": True, "events": []}, "$.schemaVersion"),
            ({"schemaVersion": "1", "events": []}, "$.schemaVersion"),
            ({"schemaVersion": 1}, "$.events"),
            ({"schemaVersion": 1, "events": {}}, "$.events"),
        ],
    )
    def test_outer_structure_rules(self, doc: Any, path: str) -> None:
        assert path in paths(validateActivityDocument(doc))


# --- runtime outer check (AC-8) ------------------------------------------------------------


class TestCheckActivityOuter:
    def test_true_for_golden_and_empty(self) -> None:
        assert checkActivityOuter(golden())
        assert checkActivityOuter({"schemaVersion": 1, "events": []})

    def test_shallow_true_with_an_invalid_event_inside(self) -> None:
        doc = mutated(setEvent(0, "reason", "guessed"))
        doc["events"].append(42)
        assert validateActivityDocument(doc) != []
        assert checkActivityOuter(doc)

    @pytest.mark.parametrize(
        "doc",
        [
            [],
            {"schemaVersion": 1},
            {"schemaVersion": "1", "events": []},
            {"schemaVersion": True, "events": []},
            {"schemaVersion": 2, "events": []},
            {"schemaVersion": 1, "events": {}},
            None,
            "x",
        ],
    )
    def test_false_for_bad_outer_shapes(self, doc: Any) -> None:
        assert not checkActivityOuter(doc)


# --- the TASK-004 stand-in now runs the real validator (decisions.log, Critic TASK-004 S1) ---


class TestStandInUsesRealValidator:
    def test_real_validator_is_called_without_monkeypatching(self) -> None:
        doc = mutated(endBeforeStart)
        problems = activity_checks.realValidatorProblems(doc)
        assert problems == [str(i) for i in validateActivityDocument(doc)]
        assert problems and "precedes" in problems[0]

    def test_stand_in_and_real_validator_agree_on_factory_output(self) -> None:
        start, end = periodPair(startAt=at(10), endNow=at(4), periodEnd=at(-5))
        assert activity_checks.activityDocumentProblems([start, end]) == []
