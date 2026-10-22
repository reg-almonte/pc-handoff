"""Atomic file replacement, corrupt-file backup and leftover temp-file cleanup (ADR-003).

- ``atomicWriteBytes``: write a temp file in the same directory, flush and fsync it, then
  ``os.replace`` it over the target, retrying while Windows briefly holds the target.
- ``backupCorrupt``: rename a corrupt file to ``<name>.corrupt-<yyyyMMddTHHmmss>`` with its
  bytes unchanged (requirement section 5, backup before replace).
- ``cleanupTempFiles``: at startup, delete the temp files a crash may have left behind.
"""

import logging
import os
import time
import uuid
from pathlib import Path

from pc_handoff.app_logging import LOGGER_NAME
from pc_handoff.clock import Clock
from pc_handoff.persistence.paths import (
    ACTIVITY_LOG_FILE_NAME,
    CONFIG_FILE_NAME,
    STATUS_FILE_NAME,
)

REPLACE_ATTEMPTS = 6  # the first try plus up to 5 retries (ADR-003 step 4)
REPLACE_RETRY_SLEEP_SECONDS = 0.02
TEMP_MARKER = ".tmp-"
CORRUPT_MARKER = ".corrupt-"
BACKUP_TIME_FORMAT = "%Y%m%dT%H%M%S"
MANAGED_FILE_NAMES = (CONFIG_FILE_NAME, STATUS_FILE_NAME, ACTIVITY_LOG_FILE_NAME)

moduleLogger = logging.getLogger(f"{LOGGER_NAME}.persistence.atomic")


class PersistenceError(Exception):
    """A file could not be written; the target is unchanged."""


def tempPathFor(path: Path) -> Path:
    """``<name>.tmp-<pid>-<8 hex>`` next to ``path`` (ADR-003)."""
    return path.with_name(f"{path.name}{TEMP_MARKER}{os.getpid()}-{uuid.uuid4().hex[:8]}")


def atomicWriteBytes(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` atomically: a crash leaves the old or the new file.

    ``os.replace`` is retried on ``PermissionError`` (antivirus, indexer or an editor holding
    the file), sleeping 20 ms between attempts. On final failure the temp file is removed and
    ``PersistenceError`` is raised; the target keeps its previous content. Any other exception
    (e.g. ``KeyboardInterrupt``) also removes the temp file and propagates unchanged.
    """
    path = Path(path)
    tempPath = tempPathFor(path)
    try:
        with open(tempPath, "xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        replaceWithRetry(tempPath, path)
    except OSError as exc:
        removeTempFile(tempPath)
        raise PersistenceError(f"could not write {path.name}: {exc}") from exc
    except BaseException:
        removeTempFile(tempPath)
        raise


def replaceWithRetry(source: Path, target: Path) -> None:
    attempt = 1
    while True:
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt >= REPLACE_ATTEMPTS:
                raise
        attempt += 1
        time.sleep(REPLACE_RETRY_SLEEP_SECONDS)


def removeTempFile(tempPath: Path) -> None:
    try:
        tempPath.unlink(missing_ok=True)
    except OSError as exc:
        moduleLogger.warning("could not remove temp file %s: %s", tempPath.name, exc)


def readBytes(path: Path) -> bytes | None:
    """Return the file's bytes, or None when it does not exist. Other errors propagate."""
    try:
        return Path(path).read_bytes()
    except FileNotFoundError:
        return None


def backupCorrupt(path: Path, clock: Clock) -> Path:
    """Rename ``path`` to ``<name>.corrupt-<local yyyyMMddTHHmmss>`` and return the new path.

    If that name is taken (two backups in one second), ``-2``, ``-3`` ... is appended. The
    bytes are not changed. Raises ``OSError`` (e.g. ``FileNotFoundError``) if the rename fails.
    """
    path = Path(path)
    stamp = clock.now().strftime(BACKUP_TIME_FORMAT)
    base = f"{path.name}{CORRUPT_MARKER}{stamp}"
    backup = path.with_name(base)
    counter = 1
    while backup.exists():
        counter += 1
        backup = path.with_name(f"{base}-{counter}")
    os.replace(path, backup)
    moduleLogger.warning("%s backed up as %s", path.name, backup.name)
    return backup


def isManagedTempFile(path: Path) -> bool:
    return path.is_file() and any(
        path.name.startswith(f"{name}{TEMP_MARKER}") for name in MANAGED_FILE_NAMES
    )


def cleanupTempFiles(dataDir: Path, logger: logging.Logger | None = None) -> list[Path]:
    """Delete leftover ``config.json.tmp-*``, ``status.json.tmp-*`` and
    ``activity_log.json.tmp-*`` files in ``dataDir`` (not in subdirectories), and nothing else.

    Each deletion is logged at INFO; a file that cannot be deleted is logged at WARNING and
    skipped. Returns the deleted paths. A missing directory is not an error.
    """
    log = logger or moduleLogger
    directory = Path(dataDir)
    if not directory.is_dir():
        return []
    removed: list[Path] = []
    for path in sorted(directory.iterdir()):
        if not isManagedTempFile(path):
            continue
        try:
            path.unlink()
        except OSError as exc:
            log.warning("could not delete leftover temp file %s: %s", path.name, exc)
            continue
        log.info("deleted leftover temp file %s", path.name)
        removed.append(path)
    return removed
