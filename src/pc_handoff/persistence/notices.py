"""One-time startup notices shown on the dashboard (REQ AC-17, AC-18; TASK-008 collects them)."""

from dataclasses import dataclass


@dataclass(frozen=True)
class StartupNotice:
    """A data file was reset at startup. ``backupName`` is the ``.corrupt-*`` file name, or
    None when no backup could be made."""

    fileName: str
    backupName: str | None
    text: str
