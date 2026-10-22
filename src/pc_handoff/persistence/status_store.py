"""``status.json``: the snapshot store (requirement sections 5 and 6, ADR-005, TASK-006).

- **Startup** (``loadAtStartup``): the file is fully validated before it is converted
  (decisions.log G3/G5). A corrupt file (unparseable, invalid, or ``schemaVersion`` not 1) is
  backed up as ``status.json.corrupt-<ts>`` with its bytes unchanged and reported with a
  notice (REQ AC-17). A missing file is simply not usable. Nothing is written at startup:
  the first poll writes the file.
- **Runtime** (``write``): the document is built fresh from the in-memory ``TrackerState``
  and written atomically on every poll. The current file is never read, checked or backed
  up (requirement section 6).
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from pc_handoff.clock import Clock
from pc_handoff.domain.models import StatusDocument, StatusSession
from pc_handoff.domain.tracker import TrackerState
from pc_handoff.persistence.atomic import (
    PersistenceError,
    atomicWriteBytes,
    backupCorrupt,
    readBytes,
)
from pc_handoff.persistence.encoding import encodeJson
from pc_handoff.persistence.notices import StartupNotice
from pc_handoff.persistence.paths import STATUS_FILE_NAME, DataPaths
from pc_handoff.persistence.reporting import describeIssues
from pc_handoff.persistence.schemas import (
    UnparseableError,
    decodeJson,
    validateStatusDocument,
)


@dataclass(frozen=True)
class StatusLoadResult:
    """``usable`` is True only for a valid file; then ``document`` is its content."""

    document: StatusDocument | None
    usable: bool
    notice: StartupNotice | None


def buildStatusDocument(state: TrackerState, updatedAt: datetime) -> StatusDocument:
    """The ``status.json`` document for ``state``, built from scratch.

    Nothing is copied from a previously loaded document, so ``schemaVersion`` is always the
    current one (decisions.log G3). Raises ``ValueError`` for an Active session with no open
    period (``StatusSession.fromTracked``).
    """
    return StatusDocument(
        status=state.status,
        lastKnownStatus=state.lastKnownStatus,
        lastKnownStatusAt=state.lastKnownStatusAt,
        lastSuccessfulPollAt=state.lastSuccessfulPollAt,
        updatedAt=updatedAt,
        detectionHealthy=state.detectionHealthy,
        sessions=tuple(StatusSession.fromTracked(s) for s in state.sessions.values()),
    )


class StatusStore:
    """Loads ``status.json`` once at startup and writes it from memory after every poll.

    Not thread-safe: a single caller thread is assumed (the poller thread, ADR-002, after the
    launcher's startup load) and there is no internal locking. The dashboard must never call
    it; it reads the in-memory snapshot instead.
    """

    def __init__(self, paths: DataPaths, clock: Clock, logger: logging.Logger) -> None:
        self.paths = paths
        self.clock = clock
        self.logger = logger

    def loadAtStartup(self) -> StatusLoadResult:
        """Read and fully validate ``status.json``. Never raises for file problems."""
        try:
            path = self.paths.status
            raw = readBytes(path)
        except (OSError, ValueError) as exc:
            self.logger.error("%s could not be read (%s); it is not used", STATUS_FILE_NAME, exc)
            return StatusLoadResult(None, False, None)
        if raw is None:
            return StatusLoadResult(None, False, None)

        try:
            doc = decodeJson(raw)
            issues = validateStatusDocument(doc)
            reason = describeIssues(issues) if issues else None
        except UnparseableError as exc:
            reason = f"cannot be parsed ({exc})"
        if reason is None:
            # Validated first (G3/G5): fromJson does not check types itself.
            return StatusLoadResult(StatusDocument.fromJson(doc), True, None)
        return self.backUp(reason)

    def backUp(self, reason: str) -> StatusLoadResult:
        try:
            backup = backupCorrupt(self.paths.status, self.clock)
        except OSError as exc:
            self.logger.error(
                "%s is corrupt (%s) and could not be backed up (%s); it is left unchanged",
                STATUS_FILE_NAME,
                reason,
                exc,
            )
            text = f"{STATUS_FILE_NAME} is corrupt and could not be backed up"
            return StatusLoadResult(None, False, StartupNotice(STATUS_FILE_NAME, None, text))
        self.logger.warning(
            "%s was corrupt (%s) and has been reset (backup: %s)",
            STATUS_FILE_NAME,
            reason,
            backup.name,
        )
        text = f"Status data was reset (backup: {backup.name})"
        return StatusLoadResult(None, False, StartupNotice(STATUS_FILE_NAME, backup.name, text))

    def write(self, state: TrackerState) -> None:
        """Write ``status.json`` atomically from ``state`` (indent 2, UTF-8, no BOM).

        The current file is never read. The document is validated before writing, so the
        store never writes a file its own startup check would reject. Every way the document
        can fail to be produced (it cannot be built from ``state``, fails validation, or
        cannot be serialized) raises ``PersistenceError`` with the file left unchanged, as for
        a failed write. A lone surrogate in a string is written as an ASCII escape (S2).
        """
        try:
            document = buildStatusDocument(state, self.clock.now()).toJson()
        except (TypeError, ValueError) as exc:
            raise PersistenceError(
                f"{STATUS_FILE_NAME} not written: document could not be built ({exc})"
            ) from exc
        issues = validateStatusDocument(document)
        if issues:
            raise PersistenceError(
                f"{STATUS_FILE_NAME} not written: document failed validation "
                f"({describeIssues(issues)})"
            )
        try:
            data = encodeJson(document, indent=2) + b"\n"
        except (TypeError, ValueError) as exc:
            raise PersistenceError(
                f"{STATUS_FILE_NAME} not written: document could not be serialized ({exc})"
            ) from exc
        atomicWriteBytes(self.paths.status, data)
