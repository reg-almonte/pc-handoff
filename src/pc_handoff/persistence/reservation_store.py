"""``reservations.json``: the atomic reservation queue and status note store (DEC-010, SD001).

- **Startup** (``loadAtStartup``): the file is fully validated before conversion. A corrupt
  file (unparseable, invalid, or ``schemaVersion`` not 1) is backed up as
  ``reservations.json.corrupt-<ts>`` with its bytes unchanged and reported with a
  ``StartupNotice``. A missing file starts with an empty ``QueueState`` and no notice.
- **Runtime** (``write``): the document is validated before writing and written atomically
  from the in-memory ``QueueState``.
"""

import logging
from dataclasses import dataclass, replace

from pc_handoff.clock import Clock
from pc_handoff.domain.models import SCHEMA_VERSION
from pc_handoff.domain.reservations import QueueState, emptyQueueState
from pc_handoff.persistence.atomic import (
    PersistenceError,
    atomicWriteBytes,
    backupCorrupt,
    readBytes,
)
from pc_handoff.persistence.encoding import encodeJson
from pc_handoff.persistence.notices import StartupNotice
from pc_handoff.persistence.paths import RESERVATIONS_FILE_NAME, DataPaths
from pc_handoff.persistence.reporting import describeIssues
from pc_handoff.persistence.schemas import (
    UnparseableError,
    decodeJson,
    validateReservationsDocument,
)


@dataclass(frozen=True)
class ReservationLoadResult:
    """``usable`` is True only for a valid file; ``state`` is always a valid ``QueueState``."""

    state: QueueState
    usable: bool
    notice: StartupNotice | None


class ReservationStore:
    """Loads ``reservations.json`` at startup and writes it atomically on state changes."""

    def __init__(self, paths: DataPaths, clock: Clock, logger: logging.Logger) -> None:
        self.paths = paths
        self.clock = clock
        self.logger = logger

    def loadAtStartup(self) -> ReservationLoadResult:
        """Read and fully validate ``reservations.json``. Never raises for file problems."""
        now = self.clock.now()
        try:
            path = self.paths.reservations
            raw = readBytes(path)
        except (OSError, ValueError) as exc:
            self.logger.error(
                "%s could not be read (%s); it is not used", RESERVATIONS_FILE_NAME, exc
            )
            return ReservationLoadResult(emptyQueueState(now), False, None)
        if raw is None:
            return ReservationLoadResult(emptyQueueState(now), False, None)

        try:
            doc = decodeJson(raw)
            issues = validateReservationsDocument(doc)
            reason = describeIssues(issues) if issues else None
        except UnparseableError as exc:
            reason = f"cannot be parsed ({exc})"
        if reason is None:
            return ReservationLoadResult(QueueState.fromJson(doc), True, None)
        return self.backUp(reason)

    def backUp(self, reason: str) -> ReservationLoadResult:
        now = self.clock.now()
        try:
            backup = backupCorrupt(self.paths.reservations, self.clock)
        except OSError as exc:
            self.logger.error(
                "%s is corrupt (%s) and could not be backed up (%s); it is left unchanged",
                RESERVATIONS_FILE_NAME,
                reason,
                exc,
            )
            text = f"{RESERVATIONS_FILE_NAME} is corrupt and could not be backed up"
            return ReservationLoadResult(
                emptyQueueState(now),
                False,
                StartupNotice(RESERVATIONS_FILE_NAME, None, text),
            )
        self.logger.warning(
            "%s was corrupt (%s) and has been reset (backup: %s)",
            RESERVATIONS_FILE_NAME,
            reason,
            backup.name,
        )
        text = f"Reservation data was reset (backup: {backup.name})"
        return ReservationLoadResult(
            emptyQueueState(now),
            False,
            StartupNotice(RESERVATIONS_FILE_NAME, backup.name, text),
        )

    def write(self, state: QueueState) -> None:
        """Write ``reservations.json`` atomically from ``state`` (indent 2, UTF-8, no BOM)."""
        try:
            normalized = replace(
                state,
                schemaVersion=SCHEMA_VERSION,
                updatedAt=self.clock.now(),
            )
            document = normalized.toJson()
        except (TypeError, ValueError, AttributeError) as exc:
            raise PersistenceError(
                f"{RESERVATIONS_FILE_NAME} not written: document could not be built ({exc})"
            ) from exc
        issues = validateReservationsDocument(document)
        if issues:
            raise PersistenceError(
                f"{RESERVATIONS_FILE_NAME} not written: document failed validation "
                f"({describeIssues(issues)})"
            )
        data = encodeJson(document, indent=2) + b"\n"
        atomicWriteBytes(self.paths.reservations, data)
