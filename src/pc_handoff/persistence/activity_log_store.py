"""``activity_log.json``: the audit log store (ADR-003, ADR-005, TASK-006).

**Canonical layout.** The store always writes one layout, so that appending is a byte
operation::

    {"schemaVersion": 1, <extra top-level keys>, "events": [
    {...event 1...},
    {...event 2...}
    ]}

``schemaVersion`` comes first and ``events`` last; any other top-level key sits between them
in its original order. One event per line (``json.dumps`` default separators, UTF-8, no
BOM), joined by ``",\\n"``; the file ends with ``SUFFIX`` (``EMPTY_SUFFIX`` when there are no
events). Existing events are re-serialized from their parsed dicts, never through
``ActivityEvent.toJson``, so unknown keys and key order are kept.

**Startup** (``loadAtStartup``): full validation (ADR-005) before any conversion; a corrupt
file is backed up and replaced by a fresh empty one; a valid non-canonical file is rewritten
canonically without a backup (event values unchanged, bytes may change).

**Runtime** (``appendBatch``), steps 0-4 of ADR-003:

0. The batch joins ``pendingEvents`` first; the working list is the whole queue.
1. Outer check: fast path when the file bytes equal ``lastWrittenBytes`` (no parse); slow
   path otherwise (parse, ``checkActivityOuter``, rebuild the index). A damaged file is
   backed up and a fresh document started; a missing file is recreated.
2. Pairing guard (``screenEvents``) over the working list.
3. New bytes and cache values are computed before any I/O.
4. Atomic replace; then the cache is assigned (``pairIndex``, ``fileEventKeys``,
   ``eventCount``, ``lastWrittenBytes`` last) and only then is the queue cleared.

**Pairing guard rules.** An event is skipped as already persisted when the file holds an
event with the same ``(eventId, eventType, usagePeriodId)`` (the idempotent retry). Otherwise
it is refused, logged at ERROR and dropped when it fails the ADR-005 event schema, or would be
a second start or end for its ``usagePeriodId``, or is a start whose period already has an
end (decisions.log F1), or is an end whose timestamp precedes its start's. Duplicate eventIds
in other periods are tolerated, as by the validator (F2). An end whose start is not in the
file is allowed: the start may be in a ``.corrupt-*`` backup (REQ AC-18).
"""

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from pc_handoff.clock import Clock
from pc_handoff.domain.models import ActivityEvent
from pc_handoff.persistence.atomic import (
    PersistenceError,
    atomicWriteBytes,
    backupCorrupt,
    readBytes,
)
from pc_handoff.persistence.encoding import encodeJson
from pc_handoff.persistence.notices import StartupNotice
from pc_handoff.persistence.paths import ACTIVITY_LOG_FILE_NAME, DataPaths
from pc_handoff.persistence.reporting import describeIssues
from pc_handoff.persistence.schemas import (
    EVENT_TYPE_VALUES,
    SESSION_START,
    UnparseableError,
    checkActivityOuter,
    decodeJson,
    eventIssuesFor,
    parseIsoOrNone,
    validateActivityDocument,
)

SUFFIX = b"\n]}\n"
EMPTY_SUFFIX = b"]}\n"
EVENT_SEPARATOR = b",\n"
DOCUMENT_HEAD = b'{"schemaVersion": 1, '
EVENTS_KEY = b'"events": ['
OUTER_KEYS = ("schemaVersion", "events")

# (eventId, eventType, usagePeriodId): what makes an incoming event "already persisted".
EventKey = tuple[str, str, str]


# --- canonical layout ---------------------------------------------------------------------


def serializeCanonical(extraKeys: dict[str, Any], eventDicts: list[Any]) -> bytes:
    """The canonical bytes of a document with these extra top-level keys and events."""
    extras = b"".join(
        json.dumps(key).encode("ascii") + b": " + encodeJson(value) + b", "
        for key, value in extraKeys.items()
    )
    head = DOCUMENT_HEAD + extras + EVENTS_KEY
    if not eventDicts:
        return head + EMPTY_SUFFIX
    return head + b"\n" + EVENT_SEPARATOR.join(encodeJson(e) for e in eventDicts) + SUFFIX


def extrasOf(parsed: dict[str, Any]) -> dict[str, Any]:
    """The top-level keys other than ``schemaVersion`` and ``events``, in file order."""
    return {key: value for key, value in parsed.items() if key not in OUTER_KEYS}


def isCanonical(raw: bytes, parsed: dict[str, Any]) -> bool:
    """True when ``raw`` is exactly the canonical serialization of its own parsed content."""
    return raw == serializeCanonical(extrasOf(parsed), parsed["events"])


# --- pairing index and guard --------------------------------------------------------------


@dataclass(frozen=True)
class PairEntry:
    """What the file holds for one ``usagePeriodId``. ``startTimestamp`` is the first start's
    timestamp (None when there is no start, or its timestamp cannot be parsed)."""

    startTimestamp: datetime | None
    hasStart: bool
    hasEnd: bool


NO_ENTRY = PairEntry(None, False, False)


def buildIndex(eventDicts: Iterable[Any]) -> tuple[dict[str, PairEntry], set[EventKey]]:
    """The pairing index and event keys of the file's events.

    Every object with a string ``usagePeriodId`` and a known ``eventType`` counts, valid or
    not, so on any file the guard is at least as strict as ``validateActivityDocument``
    (which pairs only individually valid events; on a valid file the two coincide).
    """
    index: dict[str, PairEntry] = {}
    keys: set[EventKey] = set()
    for event in eventDicts:
        if not isinstance(event, dict):
            continue
        eventId = event.get("eventId")
        eventType = event.get("eventType")
        periodId = event.get("usagePeriodId")
        if not isinstance(periodId, str) or not isinstance(eventType, str):
            continue
        if eventType not in EVENT_TYPE_VALUES:
            continue
        if isinstance(eventId, str):
            keys.add((eventId, eventType, periodId))
        entry = index.get(periodId, NO_ENTRY)
        if eventType == SESSION_START:
            if not entry.hasStart:  # the validator also keeps the first start
                index[periodId] = replace(
                    entry, hasStart=True, startTimestamp=parseIsoOrNone(event.get("timestamp"))
                )
        else:
            index[periodId] = replace(entry, hasEnd=True)
    return index, keys


@dataclass
class Screened:
    """The guard's outcome: accepted events in order, their dicts, and the index changes."""

    accepted: list[ActivityEvent] = field(default_factory=list)
    dicts: list[dict[str, Any]] = field(default_factory=list)
    updates: dict[str, PairEntry] = field(default_factory=dict)
    keys: set[EventKey] = field(default_factory=set)


def pairingRefusal(
    doc: dict[str, Any], entry: PairEntry, timestamp: datetime | None
) -> tuple[str | None, PairEntry]:
    """Why ``doc`` breaks the pairing rule against ``entry`` (None if it does not), and the
    entry after accepting it."""
    if doc["eventType"] == SESSION_START:
        if entry.hasStart:
            return "second session_start for this usagePeriodId", entry
        if entry.hasEnd:
            return "session_start after its session_end (F1)", entry
        return None, replace(entry, hasStart=True, startTimestamp=timestamp)
    if entry.hasEnd:
        return "second session_end for this usagePeriodId", entry
    start = entry.startTimestamp
    if start is not None and timestamp is not None and timestamp < start:
        return (
            f"session_end precedes its session_start "
            f"({timestamp.isoformat()} < {start.isoformat()})",
            entry,
        )
    return None, replace(entry, hasEnd=True)


def screenEvents(
    events: list[ActivityEvent],
    pairIndex: dict[str, PairEntry],
    fileKeys: set[EventKey],
    logger: logging.Logger,
) -> Screened:
    """Apply the pairing guard to ``events`` in order, against the file's index and keys and
    the events accepted before them. Neither ``pairIndex`` nor ``fileKeys`` is modified."""
    result = Screened()

    def refuse(event: Any, reason: str) -> None:
        logger.error(
            "activity event refused (%s): eventId %s, eventType %s, usagePeriodId %s",
            reason,
            getattr(event, "eventId", None),
            getattr(event, "eventType", None),
            getattr(event, "usagePeriodId", None),
        )

    for event in events:
        try:
            doc = event.toJson()
        except (ValueError, TypeError, AttributeError) as exc:
            refuse(event, f"cannot be serialized: {exc}")
            continue
        issues = eventIssuesFor(doc, "$")
        if issues:
            refuse(event, f"invalid: {describeIssues(issues)}")
            continue
        periodId = doc["usagePeriodId"]
        key: EventKey = (doc["eventId"], doc["eventType"], periodId)
        if key in fileKeys:
            logger.info(
                "activity event already persisted; skipped: eventId %s, eventType %s, "
                "usagePeriodId %s",
                *key,
            )
            continue
        entry = result.updates.get(periodId) or pairIndex.get(periodId, NO_ENTRY)
        reason, updated = pairingRefusal(doc, entry, parseIsoOrNone(doc["timestamp"]))
        if reason is not None:
            refuse(event, reason)
            continue
        result.updates[periodId] = updated
        result.keys.add(key)
        result.accepted.append(event)
        result.dicts.append(doc)
    return result


# --- the store ----------------------------------------------------------------------------


@dataclass(frozen=True)
class ActivityLoadResult:
    """``usable`` is True only for a valid file; then ``events`` is its content."""

    events: list[ActivityEvent] | None
    usable: bool
    notice: StartupNotice | None


@dataclass(frozen=True)
class AppendResult:
    """``written`` is True when the call ended with nothing pending (every accepted event is
    in the file, or there was nothing to write). ``notice`` is a runtime notice for the
    dashboard: the file was damaged and backed up, or was missing and has been recreated."""

    written: bool
    notice: StartupNotice | None = None


@dataclass
class LogBase:
    """The document an append builds on, as established by step 1."""

    fast: bool
    pairIndex: dict[str, PairEntry]
    fileEventKeys: set[EventKey]
    eventCount: int
    extras: dict[str, Any] = field(default_factory=dict)
    eventDicts: list[Any] = field(default_factory=list)
    notice: StartupNotice | None = None
    # True when the file must be backed up first and that failed: nothing may be written.
    blocked: bool = False


class ActivityLogStore:
    """Validated load at startup; cached atomic append with a pairing guard at runtime.

    Not thread-safe: a single caller thread is assumed (the poller thread, ADR-002, after the
    launcher's startup load; ``enqueuePending`` is called from that thread's exception
    handler). The queue and the cache are not synchronized and there is no internal locking.

    **Backup failures (Critic S3, S4).** A log that must be backed up (damaged at runtime, or
    rejected by full validation at startup) but cannot be is never written to. Every append
    retries the backup and keeps the events queued. One episode lasts from the first failed
    backup until the backup succeeds, the file passes the outer check again (only for a
    runtime episode) or the file is gone. Rate limit: the first failure of an episode is
    logged at ERROR and returns a notice (at startup the startup notice is that one), then
    every ``backupFailureLogEvery``-th failure after it (attempts 1, 1+N, 1+2N, ...) is
    logged at ERROR with the attempt count and the others at DEBUG. The end of an episode is
    logged at INFO with the number of failed attempts.
    """

    # 60 polls at the default 5 s interval: about one ERROR line every 5 minutes.
    backupFailureLogEvery = 60

    def __init__(self, paths: DataPaths, clock: Clock, logger: logging.Logger) -> None:
        self.paths = paths
        self.clock = clock
        self.logger = logger
        self.pendingEvents: list[ActivityEvent] = []
        self.pairIndex: dict[str, PairEntry] = {}
        self.fileEventKeys: set[EventKey] = set()
        self.eventCount = 0
        self.lastWrittenBytes: bytes | None = None
        # True while the file is known to be absent because this store could not create it
        # (startup) or moved it to a backup (runtime): recreating it then is not news.
        self.missingExpected = False
        # Failed backups in the current backup-failure episode (0: no episode), S3.
        self.backupFailures = 0
        # Why startup rejected the file when it could not be backed up; while set, the file
        # is never adopted and every append retries the backup (S4).
        self.rejectedAtStartup: str | None = None

    # --- startup ----------------------------------------------------------------------------

    def loadAtStartup(self) -> ActivityLoadResult:
        """Load and fully validate ``activity_log.json``. Never raises for file problems."""
        try:
            path = self.paths.activityLog
            raw = readBytes(path)
        except (OSError, ValueError) as exc:
            self.logger.error(
                "%s could not be read (%s); it is not used", ACTIVITY_LOG_FILE_NAME, exc
            )
            return ActivityLoadResult(None, False, None)
        if raw is None:
            # Nothing to back up. A missing log is not a usable reconciliation source.
            self.createFresh(path)
            return ActivityLoadResult(None, False, None)

        events: list[ActivityEvent] = []
        try:
            doc = decodeJson(raw)
            issues = validateActivityDocument(doc)
            reason = describeIssues(issues) if issues else None
        except UnparseableError as exc:
            reason = f"cannot be parsed ({exc})"
        if reason is None:
            assert isinstance(doc, dict)  # validated
            try:
                # Validated first (G3/G5): fromJson does not check types itself.
                events = [ActivityEvent.fromJson(e) for e in doc["events"]]
            except ValueError as exc:
                reason = f"events cannot be converted ({exc})"
        if reason is not None:
            return self.resetCorrupt(path, reason)

        eventDicts = doc["events"]
        pairIndex, keys = buildIndex(eventDicts)
        canonical = serializeCanonical(extrasOf(doc), eventDicts)
        if raw != canonical:
            try:
                atomicWriteBytes(path, canonical)
            except PersistenceError as exc:
                self.logger.warning(
                    "%s could not be normalized to canonical layout (%s); "
                    "the first append rewrites it",
                    ACTIVITY_LOG_FILE_NAME,
                    exc,
                )
                return ActivityLoadResult(events, True, None)
            self.logger.info("%s normalized to canonical layout", ACTIVITY_LOG_FILE_NAME)
        self.commitCache(pairIndex, keys, len(eventDicts), canonical)
        return ActivityLoadResult(events, True, None)

    def resetCorrupt(self, path: Path, reason: str) -> ActivityLoadResult:
        """Back up a corrupt log, then start a fresh empty one (backup before replace)."""
        try:
            backup = backupCorrupt(path, self.clock)
        except OSError as exc:
            self.logger.error(
                "%s is corrupt (%s) and could not be backed up (%s, attempt 1); it is left "
                "unchanged and every append retries the backup",
                ACTIVITY_LOG_FILE_NAME,
                reason,
                exc,
            )
            # S4: never adopt this file at runtime; this failure opens the S3 episode.
            self.rejectedAtStartup = f"failed full validation at startup: {reason}"
            self.backupFailures = 1
            text = f"{ACTIVITY_LOG_FILE_NAME} is corrupt and could not be backed up"
            return ActivityLoadResult(
                None, False, StartupNotice(ACTIVITY_LOG_FILE_NAME, None, text)
            )
        if self.createFresh(path):
            outcome = "has been reset"
            text = (
                f"{ACTIVITY_LOG_FILE_NAME} was corrupt and has been reset (backup: {backup.name})"
            )
        else:
            outcome = "has been moved aside"
            text = (
                f"{ACTIVITY_LOG_FILE_NAME} was corrupt and has been moved to {backup.name}; "
                "a new file will be created at the next write"
            )
        self.logger.warning(
            "%s was corrupt (%s) and %s (backup: %s)",
            ACTIVITY_LOG_FILE_NAME,
            reason,
            outcome,
            backup.name,
        )
        return ActivityLoadResult(
            None, False, StartupNotice(ACTIVITY_LOG_FILE_NAME, backup.name, text)
        )

    def createFresh(self, path: Path) -> bool:
        """Write the empty canonical log; on failure log an ERROR and return False."""
        data = serializeCanonical({}, [])
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomicWriteBytes(path, data)
        except (OSError, PersistenceError) as exc:
            self.logger.error("%s could not be created: %s", ACTIVITY_LOG_FILE_NAME, exc)
            self.missingExpected = True
            return False
        self.commitCache({}, set(), 0, data)
        return True

    # --- runtime ----------------------------------------------------------------------------

    def enqueuePending(self, events: Iterable[ActivityEvent]) -> None:
        """Queue events without writing; the next ``appendBatch`` writes them first.

        Only for a batch that never reached ``appendBatch`` (TASK-009, Critic MAJOR-3).
        """
        self.pendingEvents.extend(events)

    def pendingCount(self) -> int:
        return len(self.pendingEvents)

    def appendBatch(self, events: Iterable[ActivityEvent]) -> AppendResult:
        """Append one poll's events (after any still pending) in one atomic write.

        On ``PersistenceError`` (or an unreadable file) the accepted events stay queued, in
        order, and ``written`` is False. Any other exception propagates with the queue intact;
        the next call then takes the slow path and skips what was already written.
        """
        self.pendingEvents.extend(events)  # step 0: handed over at entry
        working = list(self.pendingEvents)
        try:
            path = self.paths.activityLog
            raw = readBytes(path)
        except (OSError, ValueError) as exc:
            self.logger.error(
                "%s could not be read (%s); %d event(s) stay pending",
                ACTIVITY_LOG_FILE_NAME,
                exc,
                len(working),
            )
            return AppendResult(False, None)

        base = self.currentBase(path, raw)  # step 1
        if base.blocked:
            return AppendResult(False, base.notice)

        screened = screenEvents(working, base.pairIndex, base.fileEventKeys, self.logger)
        self.pendingEvents = list(screened.accepted)  # skipped and refused events leave

        # Step 3: the new bytes and cache values, before any I/O.
        newBytes = self.buildBytes(raw, base, screened.dicts)
        newIndex = {**base.pairIndex, **screened.updates} if screened.updates else base.pairIndex
        newKeys = base.fileEventKeys | screened.keys if screened.keys else base.fileEventKeys
        newCount = base.eventCount + len(screened.dicts)

        if newBytes != raw:  # step 4
            try:
                if raw is None:
                    path.parent.mkdir(parents=True, exist_ok=True)
                atomicWriteBytes(path, newBytes)
            except (OSError, PersistenceError) as exc:
                self.logger.error(
                    "%s could not be written (%s); %d event(s) stay pending",
                    ACTIVITY_LOG_FILE_NAME,
                    exc,
                    len(self.pendingEvents),
                )
                return AppendResult(False, base.notice)
        self.commitCache(newIndex, newKeys, newCount, newBytes)
        self.pendingEvents = []
        return AppendResult(True, base.notice)

    def currentBase(self, path: Path, raw: bytes | None) -> LogBase:
        """Step 1: the runtime outer check.

        The result is ``blocked`` when the file must be backed up first and that failed.
        """
        if raw is None:
            self.endBackupEpisode("no longer needs a backup (the file is gone)")
            self.rejectedAtStartup = None
            if self.missingExpected:
                self.logger.info("%s does not exist; it is created now", ACTIVITY_LOG_FILE_NAME)
                return LogBase(False, {}, set(), 0)
            self.logger.warning(
                "%s was missing while running; a new file is started", ACTIVITY_LOG_FILE_NAME
            )
            text = f"{ACTIVITY_LOG_FILE_NAME} was missing and has been recreated"
            notice = StartupNotice(ACTIVITY_LOG_FILE_NAME, None, text)
            return LogBase(False, {}, set(), 0, notice=notice)

        if self.rejectedAtStartup is not None:
            # S4: known to be invalid; never adopted, whatever its outer structure.
            return self.backUpDamaged(path, self.rejectedAtStartup)

        if raw == self.lastWrittenBytes:
            suffix = SUFFIX if self.eventCount > 0 else EMPTY_SUFFIX
            if raw.endswith(suffix):
                self.endBackupEpisode("no longer needs a backup (it passes the outer check again)")
                return LogBase(True, self.pairIndex, self.fileEventKeys, self.eventCount)
            self.logger.error(
                "%s cache does not end with the canonical suffix; using the slow path",
                ACTIVITY_LOG_FILE_NAME,
            )

        try:
            doc = decodeJson(raw)
            reason = None if checkActivityOuter(doc) else "outer structure check failed"
        except UnparseableError as exc:
            reason = f"cannot be parsed ({exc})"
        if reason is not None:
            return self.backUpDamaged(path, reason)
        self.endBackupEpisode("no longer needs a backup (it passes the outer check again)")
        assert isinstance(doc, dict)  # checkActivityOuter passed
        eventDicts = doc["events"]
        pairIndex, keys = buildIndex(eventDicts)
        return LogBase(False, pairIndex, keys, len(eventDicts), extrasOf(doc), eventDicts)

    def backUpDamaged(self, path: Path, reason: str) -> LogBase:
        """Back up a log that must not be appended to; a fresh document follows on success."""
        try:
            backup = backupCorrupt(path, self.clock)
        except OSError as exc:
            return self.backupFailed(reason, exc)
        self.endBackupEpisode(f"was backed up as {backup.name}")
        self.rejectedAtStartup = None
        self.missingExpected = True
        self.logger.warning(
            "%s was damaged while running (%s) and has been reset (backup: %s)",
            ACTIVITY_LOG_FILE_NAME,
            reason,
            backup.name,
        )
        text = (
            f"{ACTIVITY_LOG_FILE_NAME} was damaged while running and has been reset "
            f"(backup: {backup.name})"
        )
        notice = StartupNotice(ACTIVITY_LOG_FILE_NAME, backup.name, text)
        return LogBase(False, {}, set(), 0, notice=notice)

    def backupFailed(self, reason: str, exc: OSError) -> LogBase:
        """S3: count the failure; one notice per episode and rate-limited ERROR lines."""
        self.backupFailures += 1
        attempt = self.backupFailures
        level = logging.ERROR if (attempt - 1) % self.backupFailureLogEvery == 0 else logging.DEBUG
        self.logger.log(
            level,
            "%s is damaged (%s) and could not be backed up (%s, attempt %d); "
            "it is left unchanged and %d event(s) stay pending",
            ACTIVITY_LOG_FILE_NAME,
            reason,
            exc,
            attempt,
            len(self.pendingEvents),
        )
        notice = None
        if attempt == 1:
            text = (
                f"{ACTIVITY_LOG_FILE_NAME} is damaged and could not be backed up; "
                "new events are kept in memory until it can be"
            )
            notice = StartupNotice(ACTIVITY_LOG_FILE_NAME, None, text)
        return LogBase(False, {}, set(), 0, notice=notice, blocked=True)

    def endBackupEpisode(self, outcome: str) -> None:
        """Close a backup-failure episode, if one is open, with one INFO line."""
        if self.backupFailures == 0:
            return
        failures = self.backupFailures
        self.backupFailures = 0
        self.logger.info(
            "%s %s after %d failed backup attempt(s)", ACTIVITY_LOG_FILE_NAME, outcome, failures
        )

    def buildBytes(self, raw: bytes | None, base: LogBase, newDicts: list[dict[str, Any]]) -> bytes:
        """Step 3: suffix slicing on the fast path, a canonical re-serialization otherwise."""
        if not base.fast:
            return serializeCanonical(base.extras, base.eventDicts + newDicts)
        assert raw is not None  # the fast path needs the file's bytes
        if not newDicts:
            return raw
        joined = EVENT_SEPARATOR.join(encodeJson(d) for d in newDicts)
        if base.eventCount > 0:
            return raw[: -len(SUFFIX)] + EVENT_SEPARATOR + joined + SUFFIX
        return raw[: -len(EMPTY_SUFFIX)] + b"\n" + joined + SUFFIX

    def commitCache(
        self,
        pairIndex: dict[str, PairEntry],
        fileEventKeys: set[EventKey],
        eventCount: int,
        lastWrittenBytes: bytes,
    ) -> None:
        """Assign precomputed cache values after a successful write; no I/O.

        ``lastWrittenBytes`` goes last: it is what enables the fast path, so an interruption
        between these assignments leaves the fast path disabled rather than using a stale
        index (Critic iteration-3 N2).
        """
        self.missingExpected = False
        self.pairIndex = pairIndex
        self.fileEventKeys = fileEventKeys
        self.eventCount = eventCount
        self.lastWrittenBytes = lastWrittenBytes
