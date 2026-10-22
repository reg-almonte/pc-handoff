"""Tests for the status.json validator and the shared kind checkers (TASK-005 AC-7, ADR-005).

Golden documents are built through the TASK-003 models (``StatusSession.fromTracked``,
``StatusDocument.toJson``), so every shape the application writes is proven to validate
(round trip, ADR-005 drift mitigation).
"""

import copy
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from pc_handoff.detection.provider import ConnectionType
from pc_handoff.domain.models import PcStatus, StatusDocument, StatusSession
from pc_handoff.persistence.schemas import (
    FieldSpec,
    UnparseableError,
    ValidationIssue,
    decodeJson,
    isIsoWithOffset,
    isStrictInt,
    isUuid,
    validateObject,
    validateStatusDocument,
)
from tests.fakes.builders import trackedActive, trackedDisconnected

JST = timezone(timedelta(hours=9))
T0 = datetime(2026, 10, 21, 9, 0, 0, tzinfo=JST)
POLL_AT = T0 + timedelta(minutes=10)


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def activeSession(**overrides: Any) -> StatusSession:
    overrides.setdefault("connectTime", at(0))
    overrides.setdefault("startEventTimestamp", at(1))
    return StatusSession.fromTracked(trackedActive(2, "alice", **overrides))


def disconnectedSession(**overrides: Any) -> StatusSession:
    return StatusSession.fromTracked(
        trackedDisconnected(3, "bob", disconnectedSince=at(5), **overrides)
    )


def statusDocument(
    sessions: tuple[StatusSession, ...] | None = None,
    status: PcStatus = PcStatus.IN_USE,
    lastKnownStatus: PcStatus | None = PcStatus.IN_USE,
    lastKnownStatusAt: datetime | None = POLL_AT,
    lastSuccessfulPollAt: datetime | None = POLL_AT,
    detectionHealthy: bool = True,
) -> dict[str, Any]:
    if sessions is None:
        sessions = (activeSession(), disconnectedSession())
    return StatusDocument(
        status=status,
        lastKnownStatus=lastKnownStatus,
        lastKnownStatusAt=lastKnownStatusAt,
        lastSuccessfulPollAt=lastSuccessfulPollAt,
        updatedAt=at(10),
        detectionHealthy=detectionHealthy,
        sessions=sessions,
    ).toJson()


def paths(issues: list[ValidationIssue]) -> list[str]:
    return [issue.path for issue in issues]


# --- kind checkers (decisions.log G5) ------------------------------------------------------


class TestKindCheckers:
    @pytest.mark.parametrize("value", [0, 1, -3, 2**40])
    def test_strict_int_accepts_ints(self, value: int) -> None:
        assert isStrictInt(value)

    @pytest.mark.parametrize("value", [True, False, 1.0, 10.0, "1", None, [1]])
    def test_strict_int_rejects_bool_float_and_others(self, value: Any) -> None:
        """G5: a bool is NOT an int; a float with an integral value is not one either."""
        assert not isStrictInt(value)

    def test_uuid_accepts_canonical_lowercase(self) -> None:
        assert isUuid("0b6f7c1e-3d2a-4f5b-9c8d-7e6f5a4b3c2d")

    @pytest.mark.parametrize(
        "value",
        [
            "0B6F7C1E-3D2A-4F5B-9C8D-7E6F5A4B3C2D",  # not canonical (upper case)
            "0b6f7c1e3d2a4f5b9c8d7e6f5a4b3c2d",  # no hyphens
            "{0b6f7c1e-3d2a-4f5b-9c8d-7e6f5a4b3c2d}",
            "urn:uuid:0b6f7c1e-3d2a-4f5b-9c8d-7e6f5a4b3c2d",
            "0b6f7c1e-3d2a-4f5b-9c8d-7e6f5a4b3c2d\n",
            "0b6f7c1e-3d2a-4f5b-9c8d-7e6f5a4b3c2",
            "not-a-uuid",
            "",
            None,
            12345,
            True,
        ],
    )
    def test_uuid_rejects_non_canonical_and_non_strings(self, value: Any) -> None:
        assert not isUuid(value)

    @pytest.mark.parametrize(
        "value",
        ["2026-10-21T09:15:02+09:00", "2026-10-21T00:15:02Z", "2026-10-21T09:15:02.5+09:00"],
    )
    def test_iso_with_offset_accepts(self, value: str) -> None:
        assert isIsoWithOffset(value)

    @pytest.mark.parametrize(
        "value",
        ["2026-10-21T09:15:02", "2026-10-21", "yesterday", "", None, 1729500000, True],
    )
    def test_iso_with_offset_rejects_naive_malformed_and_non_strings(self, value: Any) -> None:
        assert not isIsoWithOffset(value)


class TestDecodeJson:
    def test_plain_utf8(self) -> None:
        assert decodeJson(b'{"a": 1}') == {"a": 1}

    def test_bom_is_tolerated(self) -> None:
        assert decodeJson(b'\xef\xbb\xbf{"a": "\xc3\xa9"}') == {"a": "é"}

    @pytest.mark.parametrize(
        "raw",
        [b"{not json", b"", b"\xff\xfe\x00{", b'{"a": "\xc3"}', b"[" * 100_000],
        ids=["syntax", "empty", "utf16-bytes", "bad-utf8", "too-deep"],
    )
    def test_unparseable_raises(self, raw: bytes) -> None:
        with pytest.raises(UnparseableError):
            decodeJson(raw)


class TestValidateObject:
    def test_reports_missing_type_null_enum_and_check(self) -> None:
        specs = (
            FieldSpec("a", "int"),
            FieldSpec("b", "str", nullable=True),
            FieldSpec("c", "str", enumValues=("x", "y")),
            FieldSpec("d", "int", check=lambda v: None if v > 0 else "must be positive"),
            FieldSpec("e", "bool"),
        )
        issues = validateObject({"a": True, "b": None, "c": "z", "d": 0}, specs, "$")
        assert [str(i) for i in issues] == [
            "$.a: expected integer",
            "$.c: 'z' is not one of 'x', 'y'",
            "$.d: must be positive",
            "$.e: missing",
        ]

    def test_null_where_not_nullable(self) -> None:
        issues = validateObject({"a": None}, (FieldSpec("a", "str"),), "$")
        assert [str(i) for i in issues] == ["$.a: must not be null"]

    def test_non_object(self) -> None:
        issues = validateObject([1], (FieldSpec("a", "str"),), "$.x")
        assert [str(i) for i in issues] == ["$.x: expected object"]

    @pytest.mark.parametrize(
        ("kind", "good", "bad"),
        [
            ("number", 1.5, "1.5"),
            ("number", 2, True),
            ("list", [], {}),
            ("dict", {}, []),
            ("bool", False, 0),
        ],
    )
    def test_other_kinds(self, kind: str, good: Any, bad: Any) -> None:
        specs = (FieldSpec("v", kind),)
        assert validateObject({"v": good}, specs, "$") == []
        assert paths(validateObject({"v": bad}, specs, "$")) == ["$.v"]

    def test_unknown_kind_is_a_programming_error(self) -> None:
        with pytest.raises(ValueError, match="unknown field kind"):
            FieldSpec("v", "float")


# --- status.json golden documents (AC-7, round trip) --------------------------------------


class TestStatusGolden:
    def test_golden_document_is_valid(self) -> None:
        assert validateStatusDocument(statusDocument()) == []

    def test_null_client_fields_are_valid(self) -> None:
        """AC-7 / REQ AC-9 / AC-25: a session with null clientName and clientAddress passes."""
        doc = statusDocument(sessions=(activeSession(clientName=None, clientAddress=None),))
        assert doc["sessions"][0]["clientName"] is None
        assert doc["sessions"][0]["clientAddress"] is None
        assert validateStatusDocument(doc) == []

    def test_null_domain_logon_time_and_first_detected_at_are_valid(self) -> None:
        """decisions.log TASK-003 S9: firstDetectedAt, domain and logonTime may be null."""
        session = activeSession(domain=None, logonTime=None, startEventTimestamp=None)
        doc = statusDocument(sessions=(session, disconnectedSession(domain=None, logonTime=None)))
        entry = doc["sessions"][0]
        assert (entry["domain"], entry["logonTime"], entry["firstDetectedAt"]) == (None,) * 3
        assert validateStatusDocument(doc) == []

    def test_approximate_period_with_null_connect_time_is_valid(self) -> None:
        """REQ AC-10: connectTime null, usagePeriodStart = T1, durationApproximate true."""
        tracked = trackedActive(2, "alice", connectTime=None, startEventTimestamp=at(1))
        doc = statusDocument(sessions=(StatusSession.fromTracked(tracked),))
        assert doc["sessions"][0]["connectTime"] is None
        assert doc["sessions"][0]["durationApproximate"] is True
        assert validateStatusDocument(doc) == []

    def test_console_session_is_valid(self) -> None:
        session = activeSession(
            connectionType=ConnectionType.CONSOLE, clientName=None, clientAddress=None
        )
        assert validateStatusDocument(statusDocument(sessions=(session,))) == []

    def test_fresh_install_unknown_document_is_valid(self) -> None:
        """REQ AC-27: Unknown, unhealthy, every last-known value null, no sessions."""
        doc = statusDocument(
            sessions=(),
            status=PcStatus.UNKNOWN,
            lastKnownStatus=None,
            lastKnownStatusAt=None,
            lastSuccessfulPollAt=None,
            detectionHealthy=False,
        )
        assert validateStatusDocument(doc) == []

    @pytest.mark.parametrize("lastKnown", [None, PcStatus.AVAILABLE, PcStatus.IN_USE])
    def test_unknown_with_each_legal_last_known_status_is_valid(
        self, lastKnown: PcStatus | None
    ) -> None:
        """G1: lastKnownStatus is null, Available or In Use."""
        doc = statusDocument(
            status=PcStatus.UNKNOWN, lastKnownStatus=lastKnown, detectionHealthy=False
        )
        assert validateStatusDocument(doc) == []

    def test_available_empty_document_is_valid(self) -> None:
        doc = statusDocument(sessions=(), status=PcStatus.AVAILABLE)
        assert validateStatusDocument(doc) == []

    def test_unknown_extra_keys_are_tolerated(self) -> None:
        """ADR-005: extra keys in status.json are tolerated within schemaVersion 1."""
        doc = statusDocument()
        doc["future"] = {"x": 1}
        doc["sessions"][0]["futureField"] = True
        assert validateStatusDocument(doc) == []


# --- status.json mutations (AC-7) ----------------------------------------------------------


def mutated(mutate) -> dict[str, Any]:
    doc = copy.deepcopy(statusDocument())
    mutate(doc)
    return doc


def setTop(key: str, value: Any):
    def mutate(doc: dict[str, Any]) -> None:
        doc[key] = value

    return mutate


def setSession(index: int, key: str, value: Any):
    def mutate(doc: dict[str, Any]) -> None:
        doc["sessions"][index][key] = value

    return mutate


def delSession(index: int, key: str):
    def mutate(doc: dict[str, Any]) -> None:
        del doc["sessions"][index][key]

    return mutate


class TestStatusMutations:
    @pytest.mark.parametrize(
        ("mutate", "path"),
        [
            # the six AC-7 mutations
            (setTop("status", "Maybe"), "$.status"),
            (setTop("schemaVersion", 2), "$.schemaVersion"),
            (setSession(0, "usagePeriodId", None), "$.sessions[0].usagePeriodId"),
            (
                setSession(1, "usagePeriodStart", "2026-10-21T09:00:00+09:00"),
                "$.sessions[1].usagePeriodStart",
            ),
            (setTop("updatedAt", "2026-10-21T09:10:00"), "$.updatedAt"),
            (setSession(0, "usagePeriodId", "not-a-uuid"), "$.sessions[0].usagePeriodId"),
            # further rules
            (setTop("schemaVersion", True), "$.schemaVersion"),
            (setTop("schemaVersion", "1"), "$.schemaVersion"),
            (setTop("schemaVersion", 1.0), "$.schemaVersion"),
            (setTop("lastKnownStatus", "Unknown"), "$.lastKnownStatus"),
            (setTop("lastKnownStatus", "Maybe"), "$.lastKnownStatus"),
            (setTop("lastKnownStatusAt", "soon"), "$.lastKnownStatusAt"),
            (setTop("lastSuccessfulPollAt", "2026-10-21"), "$.lastSuccessfulPollAt"),
            (setTop("updatedAt", None), "$.updatedAt"),
            (setTop("detectionHealthy", 1), "$.detectionHealthy"),
            (setTop("sessions", {}), "$.sessions"),
            (setTop("sessions", [1]), "$.sessions[0]"),
            (setSession(0, "sessionId", True), "$.sessions[0].sessionId"),
            (setSession(0, "sessionId", "2"), "$.sessions[0].sessionId"),
            (setSession(0, "sessionId", -1), "$.sessions[0].sessionId"),
            (setSession(0, "userName", ""), "$.sessions[0].userName"),
            (setSession(0, "userName", None), "$.sessions[0].userName"),
            (setSession(0, "domain", 7), "$.sessions[0].domain"),
            (setSession(0, "state", "Connected"), "$.sessions[0].state"),
            (setSession(0, "state", ["Active"]), "$.sessions[0].state"),
            (delSession(0, "state"), "$.sessions[0].state"),
            (setSession(0, "connectionType", "rdp"), "$.sessions[0].connectionType"),
            (setSession(0, "clientName", 5), "$.sessions[0].clientName"),
            (setSession(0, "clientAddress", ["x"]), "$.sessions[0].clientAddress"),
            (setSession(0, "logonTime", "2026-10-21T08:00:00"), "$.sessions[0].logonTime"),
            (setSession(0, "firstDetectedAt", 0), "$.sessions[0].firstDetectedAt"),
            (setSession(0, "connectTime", "x"), "$.sessions[0].connectTime"),
            (setSession(0, "usagePeriodStart", None), "$.sessions[0].usagePeriodStart"),
            (setSession(0, "durationApproximate", None), "$.sessions[0].durationApproximate"),
            (setSession(0, "durationApproximate", "false"), "$.sessions[0].durationApproximate"),
            (
                setSession(0, "disconnectedSince", "2026-10-21T09:05:00+09:00"),
                "$.sessions[0].disconnectedSince",
            ),
            (
                setSession(1, "usagePeriodId", "0b6f7c1e-3d2a-4f5b-9c8d-7e6f5a4b3c2d"),
                "$.sessions[1].usagePeriodId",
            ),
            (setSession(1, "disconnectedSince", "later"), "$.sessions[1].disconnectedSince"),
            (delSession(0, "clientName"), "$.sessions[0].clientName"),
            (delSession(1, "firstDetectedAt"), "$.sessions[1].firstDetectedAt"),
        ],
    )
    def test_each_mutation_names_the_offending_path(self, mutate, path: str) -> None:
        issues = validateStatusDocument(mutated(mutate))
        assert path in paths(issues), issues

    @pytest.mark.parametrize("key", list(statusDocument()))
    def test_each_missing_top_level_key_is_reported(self, key: str) -> None:
        doc = statusDocument()
        del doc[key]
        assert f"$.{key}" in paths(validateStatusDocument(doc))

    @pytest.mark.parametrize("doc", [[], "status", None, 1])
    def test_non_object_root(self, doc: Any) -> None:
        assert paths(validateStatusDocument(doc)) == ["$"]

    def test_conditional_rules_skip_a_session_with_an_invalid_state(self) -> None:
        """An unknown state yields the enum issue only, not a cascade of conditional ones."""
        doc = mutated(setSession(0, "state", "Maybe"))
        assert paths(validateStatusDocument(doc)) == ["$.sessions[0].state"]

    def test_missing_conditional_key_is_reported_once(self) -> None:
        doc = mutated(delSession(0, "usagePeriodId"))
        issues = validateStatusDocument(doc)
        assert [str(i) for i in issues] == ["$.sessions[0].usagePeriodId: missing"]

    def test_issue_string_form(self) -> None:
        issue = ValidationIssue("$.status", "bad")
        assert str(issue) == "$.status: bad"
