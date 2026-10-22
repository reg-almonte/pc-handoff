"""Hand-written validators for the persisted JSON documents (ADR-005, standard library only).

- ``decodeJson``: BOM-tolerant decode; anything unparseable raises ``UnparseableError``.
- ``validateStatusDocument``: full startup validation of ``status.json``.
- ``validateActivityDocument``: full startup validation of ``activity_log.json``, including
  the in-file pairing check.
- ``checkActivityOuter``: the shallow runtime check of ``activity_log.json`` (ADR-003).

The validators return a list of ``ValidationIssue``; an empty list means valid. Extra keys are
tolerated in ``status.json`` and in event objects (forward compatibility within
``schemaVersion: 1``). ``config.json`` is handled per field by ``config_store``.

The pairing check rejects an end whose ``timestamp`` precedes its start's. ``newEndEvent``
clamps every end to ``timestamp >= start.timestamp`` (ADR-007 R11), so the application's own
output always passes; equal timestamps are valid.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pc_handoff.clock import isAware

SCHEMA_VERSION = 1

PC_STATUS_VALUES = ("Available", "In Use", "Unknown")
LAST_KNOWN_STATUS_VALUES = ("Available", "In Use")  # never "Unknown" (decisions.log G1)
SESSION_STATE_VALUES = ("Active", "Disconnected")
CONNECTION_TYPE_VALUES = ("RDP", "Console")
EVENT_TYPE_VALUES = ("session_start", "session_end")
REASON_VALUES = ("observed", "detected_after_outage", "detected_at_startup")
END_CAUSE_VALUES = ("disconnect", "logoff", "reconnect_detected", "unknown")

SESSION_START = "session_start"
SESSION_END = "session_end"
ACTIVE = "Active"
DISCONNECTED = "Disconnected"

UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class UnparseableError(ValueError):
    """The bytes are not UTF-8 JSON (whole-file corrupt)."""


@dataclass(frozen=True)
class ValidationIssue:
    """One problem found in a document. ``path`` is JSONPath-like, e.g. ``$.sessions[0].state``."""

    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


# --- kind checkers -------------------------------------------------------------------------


def isStrictInt(value: Any) -> bool:
    """True for an ``int`` that is not a ``bool`` (ADR-005; floats such as 10.0 are rejected)."""
    return isinstance(value, int) and not isinstance(value, bool)


def isUuid(value: Any) -> bool:
    """True for a UUID string in canonical form: lowercase hex, 8-4-4-4-12, nothing else.

    The application writes ``str(uuid.uuid4())``, which is always in this form. Pairing
    compares ids as strings, so other spellings of the same UUID are rejected.
    """
    return isinstance(value, str) and UUID_PATTERN.fullmatch(value) is not None


def isIsoWithOffset(value: Any) -> bool:
    """True for an ISO 8601 date-time string with an explicit UTC offset (or ``Z``)."""
    return parseIsoOrNone(value) is not None


def parseIsoOrNone(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if isAware(dt) else None


def isNonNegativeInt(value: Any) -> str | None:
    return None if value >= 0 else "must be 0 or more"


def isNonEmpty(value: Any) -> str | None:
    return None if value else "must not be empty"


KIND_CHECKS: dict[str, tuple[Callable[[Any], bool], str]] = {
    "str": (lambda v: isinstance(v, str), "expected string"),
    "int": (isStrictInt, "expected integer"),
    "bool": (lambda v: isinstance(v, bool), "expected boolean"),
    "number": (lambda v: isStrictInt(v) or isinstance(v, float), "expected number"),
    "uuid": (isUuid, "expected a UUID (lowercase 8-4-4-4-12 hex)"),
    "isoDateTime": (isIsoWithOffset, "expected an ISO 8601 timestamp with UTC offset"),
    "list": (lambda v: isinstance(v, list), "expected array"),
    "dict": (lambda v: isinstance(v, dict), "expected object"),
}


@dataclass(frozen=True)
class FieldSpec:
    """One required key of an object: its kind, nullability, allowed values and extra check.

    ``check`` runs only on a non-null value of the right kind and returns an issue message or
    None. Every listed key must be present, even when it is nullable.
    """

    name: str
    kind: str
    nullable: bool = False
    enumValues: tuple[Any, ...] | None = None
    check: Callable[[Any], str | None] | None = None

    def __post_init__(self) -> None:
        if self.kind not in KIND_CHECKS:
            raise ValueError(f"unknown field kind: {self.kind!r}")


def validateObject(obj: Any, specs: tuple[FieldSpec, ...], path: str) -> list[ValidationIssue]:
    """Check presence, kind, nullability, enum and ``check`` of each field in ``specs``."""
    if not isinstance(obj, dict):
        return [ValidationIssue(path, "expected object")]
    issues: list[ValidationIssue] = []
    for spec in specs:
        fieldPath = f"{path}.{spec.name}"
        if spec.name not in obj:
            issues.append(ValidationIssue(fieldPath, "missing"))
            continue
        value = obj[spec.name]
        if value is None:
            if not spec.nullable:
                issues.append(ValidationIssue(fieldPath, "must not be null"))
            continue
        isKind, kindMessage = KIND_CHECKS[spec.kind]
        if not isKind(value):
            issues.append(ValidationIssue(fieldPath, kindMessage))
            continue
        if spec.enumValues is not None and value not in spec.enumValues:
            allowed = ", ".join(repr(v) for v in spec.enumValues)
            issues.append(ValidationIssue(fieldPath, f"{value!r} is not one of {allowed}"))
            continue
        if spec.check is not None:
            message = spec.check(value)
            if message is not None:
                issues.append(ValidationIssue(fieldPath, message))
    return issues


def decodeJson(raw: bytes) -> object:
    """Decode ``raw`` as UTF-8 (a leading BOM is tolerated) and parse it as JSON.

    Raises ``UnparseableError`` for undecodable bytes, invalid JSON, or nesting too deep to
    parse.
    """
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (ValueError, RecursionError) as exc:  # UnicodeDecodeError, JSONDecodeError
        raise UnparseableError(f"{type(exc).__name__}: {exc}") from exc


# --- shared field specs --------------------------------------------------------------------


def schemaVersionIssues(doc: dict[str, Any]) -> list[ValidationIssue]:
    if "schemaVersion" not in doc:
        return [ValidationIssue("$.schemaVersion", "missing")]
    version = doc["schemaVersion"]
    if not isStrictInt(version) or version != SCHEMA_VERSION:
        return [ValidationIssue("$.schemaVersion", f"must be the integer 1, got {version!r}")]
    return []


SESSION_ID = FieldSpec("sessionId", "int", check=isNonNegativeInt)
USER_NAME = FieldSpec("userName", "str", check=isNonEmpty)
DOMAIN = FieldSpec("domain", "str", nullable=True)
CLIENT_NAME = FieldSpec("clientName", "str", nullable=True)
CLIENT_ADDRESS = FieldSpec("clientAddress", "str", nullable=True)
LOGON_TIME = FieldSpec("logonTime", "isoDateTime", nullable=True)
CONNECTION_TYPE = FieldSpec("connectionType", "str", enumValues=CONNECTION_TYPE_VALUES)
CONNECT_TIME = FieldSpec("connectTime", "isoDateTime", nullable=True)
DURATION_APPROXIMATE = FieldSpec("durationApproximate", "bool")


# --- status.json ---------------------------------------------------------------------------

STATUS_SPECS = (
    FieldSpec("status", "str", enumValues=PC_STATUS_VALUES),
    FieldSpec("lastKnownStatus", "str", nullable=True, enumValues=LAST_KNOWN_STATUS_VALUES),
    FieldSpec("lastKnownStatusAt", "isoDateTime", nullable=True),
    FieldSpec("lastSuccessfulPollAt", "isoDateTime", nullable=True),
    FieldSpec("updatedAt", "isoDateTime"),
    FieldSpec("detectionHealthy", "bool"),
    FieldSpec("sessions", "list"),
)

STATUS_SESSION_SPECS = (
    SESSION_ID,
    USER_NAME,
    DOMAIN,
    FieldSpec("state", "str", enumValues=SESSION_STATE_VALUES),
    CONNECTION_TYPE,
    CLIENT_NAME,
    CLIENT_ADDRESS,
    LOGON_TIME,
    FieldSpec("firstDetectedAt", "isoDateTime", nullable=True),
    CONNECT_TIME,
    FieldSpec("usagePeriodId", "uuid", nullable=True),
    FieldSpec("usagePeriodStart", "isoDateTime", nullable=True),
    DURATION_APPROXIMATE,
    FieldSpec("disconnectedSince", "isoDateTime", nullable=True),
)

# State-conditional nulls (ADR-005): key -> must be null (True) / non-null (False).
STATE_RULES: dict[str, dict[str, bool]] = {
    ACTIVE: {"usagePeriodId": False, "usagePeriodStart": False, "disconnectedSince": True},
    DISCONNECTED: {"usagePeriodId": True, "usagePeriodStart": True},
}


def validateStatusDocument(doc: Any) -> list[ValidationIssue]:
    """Fully validate a parsed ``status.json`` (requirement section 5, ADR-005)."""
    if not isinstance(doc, dict):
        return [ValidationIssue("$", "expected object")]
    issues = schemaVersionIssues(doc) + validateObject(doc, STATUS_SPECS, "$")
    sessions = doc.get("sessions")
    if isinstance(sessions, list):
        for index, entry in enumerate(sessions):
            issues.extend(statusSessionIssues(entry, f"$.sessions[{index}]"))
    return issues


def statusSessionIssues(entry: Any, path: str) -> list[ValidationIssue]:
    issues = validateObject(entry, STATUS_SESSION_SPECS, path)
    if not isinstance(entry, dict):
        return issues
    state = entry.get("state")
    rules = STATE_RULES.get(state) if isinstance(state, str) else None
    if rules is None:  # missing or invalid state: already reported above
        return issues
    for key, mustBeNull in rules.items():
        if key not in entry:  # already reported as missing
            continue
        isNull = entry[key] is None
        if mustBeNull and not isNull:
            issues.append(ValidationIssue(f"{path}.{key}", f"must be null when {entry['state']}"))
        elif not mustBeNull and isNull:
            issues.append(ValidationIssue(f"{path}.{key}", f"required when {entry['state']}"))
    return issues


# --- activity_log.json ---------------------------------------------------------------------

EVENT_SPECS = (
    FieldSpec("eventId", "uuid"),
    FieldSpec("eventType", "str", enumValues=EVENT_TYPE_VALUES),
    FieldSpec("usagePeriodId", "uuid"),
    FieldSpec("timestamp", "isoDateTime"),
    SESSION_ID,
    USER_NAME,
    DOMAIN,
    LOGON_TIME,
    CLIENT_NAME,
    CLIENT_ADDRESS,
    CONNECTION_TYPE,
    CONNECT_TIME,
    FieldSpec("usagePeriodStart", "isoDateTime"),
    DURATION_APPROXIMATE,
    FieldSpec("reason", "str", enumValues=REASON_VALUES),
)

END_EVENT_SPECS = (
    FieldSpec("periodEnd", "isoDateTime", nullable=True),
    FieldSpec("durationSeconds", "int", nullable=True, check=isNonNegativeInt),
    FieldSpec("endCause", "str", enumValues=END_CAUSE_VALUES),
)


def checkActivityOuter(doc: Any) -> bool:
    """Shallow runtime check: an object with ``schemaVersion`` the integer 1 and an ``events``
    array. Events themselves are not inspected (requirement section 6, ADR-003)."""
    return (
        isinstance(doc, dict)
        and not schemaVersionIssues(doc)
        and isinstance(doc.get("events"), list)
    )


def validateActivityDocument(doc: Any) -> list[ValidationIssue]:
    """Fully validate a parsed ``activity_log.json`` (requirement section 5, ADR-005).

    Checks the outer structure, every event (``session_end`` also needs ``periodEnd``,
    ``durationSeconds`` and ``endCause``), then pairing across the valid events: at most one
    start and one end per ``usagePeriodId``, and no end earlier than its start. An end whose
    start is not in the file is valid; the start may be in a ``.corrupt-*`` backup.
    """
    if not isinstance(doc, dict):
        return [ValidationIssue("$", "expected object")]
    issues = schemaVersionIssues(doc)
    events = doc.get("events")
    if not isinstance(events, list):
        message = "missing" if "events" not in doc else "expected array"
        issues.append(ValidationIssue("$.events", message))
        return issues
    validEvents: list[tuple[int, dict[str, Any]]] = []
    for index, event in enumerate(events):
        eventIssues = eventIssuesFor(event, f"$.events[{index}]")
        issues.extend(eventIssues)
        if not eventIssues:
            validEvents.append((index, event))
    issues.extend(pairingIssues(validEvents))
    return issues


def eventIssuesFor(event: Any, path: str) -> list[ValidationIssue]:
    issues = validateObject(event, EVENT_SPECS, path)
    if isinstance(event, dict) and event.get("eventType") == SESSION_END:
        issues.extend(validateObject(event, END_EVENT_SPECS, path))
    return issues


def pairingIssues(events: list[tuple[int, dict[str, Any]]]) -> list[ValidationIssue]:
    """ADR-005 in-file pairing rule over events that passed their own checks."""
    issues: list[ValidationIssue] = []
    starts: dict[str, datetime] = {}
    ends: dict[str, tuple[int, datetime]] = {}
    for index, event in events:
        periodId = event["usagePeriodId"]
        timestamp = parseIsoOrNone(event["timestamp"])
        assert timestamp is not None  # guaranteed by EVENT_SPECS
        seen = starts if event["eventType"] == SESSION_START else ends
        if periodId in seen:
            issues.append(
                ValidationIssue(
                    f"$.events[{index}]",
                    f"second {event['eventType']} for this usagePeriodId",
                )
            )
            continue
        if seen is starts:
            starts[periodId] = timestamp
        else:
            ends[periodId] = (index, timestamp)
    for periodId, (index, endTimestamp) in ends.items():
        startTimestamp = starts.get(periodId)
        if startTimestamp is not None and endTimestamp < startTimestamp:
            issues.append(
                ValidationIssue(
                    f"$.events[{index}].timestamp",
                    "session_end precedes its session_start "
                    f"({endTimestamp.isoformat()} < {startTimestamp.isoformat()})",
                )
            )
    return issues
