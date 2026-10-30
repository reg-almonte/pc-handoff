"""Offline-first host PC notification subsystem (REQ-003, SD002 section 4)."""

from pc_handoff.notifications.notifier import (
    CompositeNotifier,
    HostNotification,
    Notifier,
    NullNotifier,
    formatEventNotification,
)
from pc_handoff.notifications.teams_notifier import TeamsNotifier
from pc_handoff.notifications.windows_notifier import (
    WindowsNotifier,
    buildMsgCommand,
    buildToastCommand,
    createNotifier,
)

__all__ = [
    "CompositeNotifier",
    "HostNotification",
    "Notifier",
    "NullNotifier",
    "TeamsNotifier",
    "WindowsNotifier",
    "buildMsgCommand",
    "buildToastCommand",
    "createNotifier",
    "formatEventNotification",
]
