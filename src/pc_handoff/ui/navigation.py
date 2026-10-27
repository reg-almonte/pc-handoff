"""Navigation controller for switching between the Status and Manage views (REQ-003, TASK-021)."""

from collections.abc import MutableMapping
from typing import Any

VIEW_STATE_KEY = "current_view"
VIEW_STATUS = "status"
VIEW_MANAGE = "manage"

NAV_TO_MANAGE_KEY = "nav_to_manage"
NAV_TO_MANAGE_LABEL = "⚙️ Manage Queue & Notes"
NAV_TO_STATUS_KEY = "nav_to_status"
NAV_TO_STATUS_LABEL = "← Back to PC Status"


class NavigationController:
    """Pure session-state router between ``status`` and ``manage`` views."""

    @staticmethod
    def currentView(sessionState: MutableMapping[str, Any]) -> str:
        """Return ``'manage'`` or ``'status'`` for the active browser session."""
        if sessionState.get(VIEW_STATE_KEY) == VIEW_MANAGE:
            return VIEW_MANAGE
        return VIEW_STATUS

    @staticmethod
    def goToManage(sessionState: MutableMapping[str, Any]) -> None:
        """Switch the browser session to the dedicated Manage view."""
        sessionState[VIEW_STATE_KEY] = VIEW_MANAGE

    @staticmethod
    def goToStatus(sessionState: MutableMapping[str, Any]) -> None:
        """Switch the browser session back to the main PC Status view."""
        sessionState[VIEW_STATE_KEY] = VIEW_STATUS
