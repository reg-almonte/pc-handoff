"""Checks that a list of emitted events forms a valid ``activity_log.json`` document.

``activityDocumentProblems`` is a test-side stand-in for the TASK-005
``validateActivityDocument`` (which does not exist yet when TASK-004 is implemented). It
checks the requirement section 5 event shape and the ADR-005 in-file pairing rule. Once
``pc_handoff.persistence.schemas.validateActivityDocument`` exists, it is run as well, so the
tracker tests automatically start exercising the real validator.
"""

import importlib
import uuid
from typing import Any

from pc_handoff.clock import parseIso
from pc_handoff.domain.models import ActivityEvent, EventType

COMMON_KEYS = (
    "eventId",
    "eventType",
    "usagePeriodId",
    "timestamp",
    "sessionId",
    "userName",
    "domain",
    "logonTime",
    "clientName",
    "clientAddress",
    "connectionType",
    "connectTime",
    "usagePeriodStart",
    "durationApproximate",
    "reason",
)
END_KEYS = ("periodEnd", "durationSeconds", "endCause")
SCHEMAS_MODULE = "pc_handoff.persistence.schemas"


def isUuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def eventProblems(index: int, doc: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    isEnd = doc.get("eventType") == EventType.SESSION_END.value
    expected = COMMON_KEYS + (END_KEYS if isEnd else ())
    if tuple(doc) != expected:
        problems.append(f"event {index}: keys {tuple(doc)}")
    for key in ("eventId", "usagePeriodId"):
        if not isUuid(doc.get(key)):
            problems.append(f"event {index}: {key} is not a UUID")
    if not isinstance(doc.get("userName"), str) or not doc["userName"]:
        problems.append(f"event {index}: empty userName")
    if isEnd:
        duration = doc.get("durationSeconds")
        if duration is not None and (type(duration) is not int or duration < 0):
            problems.append(f"event {index}: durationSeconds {duration!r}")
        if doc.get("endCause") is None:
            problems.append(f"event {index}: endCause is null")
        if doc.get("periodEnd") is not None and parseIso(doc["periodEnd"]) < parseIso(
            doc["usagePeriodStart"]
        ):
            problems.append(f"event {index}: periodEnd before usagePeriodStart")
    if ActivityEvent.fromJson(doc).toJson() != doc:
        problems.append(f"event {index}: does not round-trip")
    return problems


def pairingProblems(events: list[dict[str, Any]]) -> list[str]:
    problems: list[str] = []
    starts: dict[str, str] = {}
    ends: set[str] = set()
    for index, doc in enumerate(events):
        periodId = doc["usagePeriodId"]
        if doc["eventType"] == EventType.SESSION_START.value:
            if periodId in starts:
                problems.append(f"event {index}: second session_start for {periodId}")
            starts[periodId] = doc["timestamp"]
        else:
            if periodId in ends:
                problems.append(f"event {index}: second session_end for {periodId}")
            ends.add(periodId)
            if periodId in starts and parseIso(doc["timestamp"]) < parseIso(starts[periodId]):
                problems.append(f"event {index}: session_end precedes its session_start")
    return problems


def activityDocumentProblems(events: list[ActivityEvent]) -> list[str]:
    """Return every problem found in ``{"schemaVersion": 1, "events": [...]}``."""
    document = {"schemaVersion": 1, "events": [e.toJson() for e in events]}
    problems: list[str] = []
    for index, doc in enumerate(document["events"]):
        problems.extend(eventProblems(index, doc))
    problems.extend(pairingProblems(document["events"]))
    problems.extend(realValidatorProblems(document))
    return problems


def realValidatorProblems(document: dict[str, Any]) -> list[str]:
    """Run TASK-005's ``validateActivityDocument`` when it exists.

    Only the absence of the schemas module itself skips it. Any other import failure (a
    broken ``schemas.py`` or one of its own imports) propagates, so it can never silently
    disable the real validator (Critic TASK-004 S1).
    """
    try:
        schemas = importlib.import_module(SCHEMAS_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name != SCHEMAS_MODULE:
            raise
        return []
    return [str(issue) for issue in schemas.validateActivityDocument(document)]
