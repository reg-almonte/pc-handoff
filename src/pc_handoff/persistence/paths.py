"""Locations of the application's files inside the data directory.

Every file the application touches is resolved through ``DataPaths``, and
``resolveInside`` refuses any name that would leave the data directory (requirement Security
constraint: file access is limited to the configured data directory).
"""

from pathlib import Path, PurePosixPath, PureWindowsPath

CONFIG_FILE_NAME = "config.json"
STATUS_FILE_NAME = "status.json"
ACTIVITY_LOG_FILE_NAME = "activity_log.json"
RESERVATIONS_FILE_NAME = "reservations.json"
KNOWN_USERS_FILE_NAME = "known_users.json"
LOCK_FILE_NAME = "pc-handoff.lock"
LOGS_DIR_NAME = "logs"
APP_LOG_FILE_NAME = "app.log"


class DataPaths:
    """Paths of the data files under one data directory (resolved once, at construction)."""

    def __init__(self, dataDir: str | Path) -> None:
        self.dataDir = Path(dataDir).resolve()

    def resolveInside(self, name: str) -> Path:
        """Return ``dataDir / name`` resolved, or raise ``ValueError`` if it is not strictly
        inside the data directory.

        ``name`` must be relative: absolute paths, drive letters and roots are refused on every
        OS, as are ``..`` escapes and the data directory itself.
        """
        if PurePosixPath(name).anchor or PureWindowsPath(name).anchor:
            raise ValueError(f"path must be relative to the data directory: {name!r}")
        resolved = (self.dataDir / name).resolve()
        if resolved == self.dataDir or not resolved.is_relative_to(self.dataDir):
            raise ValueError(f"path escapes the data directory: {name!r}")
        return resolved

    @property
    def config(self) -> Path:
        return self.resolveInside(CONFIG_FILE_NAME)

    @property
    def status(self) -> Path:
        return self.resolveInside(STATUS_FILE_NAME)

    @property
    def activityLog(self) -> Path:
        return self.resolveInside(ACTIVITY_LOG_FILE_NAME)

    @property
    def reservations(self) -> Path:
        return self.resolveInside(RESERVATIONS_FILE_NAME)

    @property
    def knownUsers(self) -> Path:
        return self.resolveInside(KNOWN_USERS_FILE_NAME)

    @property
    def lock(self) -> Path:
        return self.resolveInside(LOCK_FILE_NAME)

    @property
    def logsDir(self) -> Path:
        return self.resolveInside(LOGS_DIR_NAME)

    @property
    def appLog(self) -> Path:
        return self.resolveInside(f"{LOGS_DIR_NAME}/{APP_LOG_FILE_NAME}")
