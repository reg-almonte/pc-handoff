"""The PC Handoff status dashboard (TASK-010, TASK-021, ADR-008, DEC-011).

Streamlit runs this file as a script. The page reads the runtime registered by the launcher
and routes between the passive Status View and the dedicated Manage View using
``NavigationController`` (REQ-003).
"""

import logging

import streamlit as st

from pc_handoff.app_logging import LOGGER_NAME, newTraceId, traceScope
from pc_handoff.runtime.registry import getRuntime
from pc_handoff.ui.navigation import (
    LEGACY_CONTROLS_KEY,
    VIEW_MANAGE,
    NavigationController,
)
from pc_handoff.ui.view_model import (
    buildViewModel,
    configWarningLine,
    escapeMarkdown,
)
from pc_handoff.ui.views.manage_view import renderManageView
from pc_handoff.ui.views.status_view import (
    DISMISS_LABEL,
    DISMISSED_KEY,
    FEEDBACK_ERROR_KEY,
    RESERVATION_CONTROLS_LABEL,
    dismissNotice,
    renderBadge,
    renderStatusView,
    renderViewModel,
    table,
)

PAGE_TITLE = "PC Handoff"
NOT_STARTED_MESSAGE = "Start PC Handoff with: python -m pc_handoff"
WAITING_MESSAGE = "Waiting for the first status update."
RESERVATION_CONTROLS_KEY = LEGACY_CONTROLS_KEY

__all__ = [
    "DISMISSED_KEY",
    "DISMISS_LABEL",
    "FEEDBACK_ERROR_KEY",
    "NOT_STARTED_MESSAGE",
    "PAGE_TITLE",
    "RESERVATION_CONTROLS_KEY",
    "RESERVATION_CONTROLS_LABEL",
    "WAITING_MESSAGE",
    "dismissNotice",
    "renderBadge",
    "renderViewModel",
    "table",
]

logger = logging.getLogger(f"{LOGGER_NAME}.ui")


def guarded(render, runtime) -> None:
    """Run one part of a dashboard request under a fresh traceId (requirement section 8).

    An exception is logged at ERROR with that traceId and shown as a short error, so a
    rendering fault never crashes the page or the server (ADR-008).
    """
    traceId = newTraceId()
    with traceScope(traceId):
        try:
            render(runtime)
        except Exception:
            logger.error("dashboard render failed", exc_info=True)
            st.error(f"Dashboard error; see app.log trace {traceId}")


def renderConfigWarnings(runtime) -> None:
    """Config warnings are fixed for the process lifetime (REQ AC-15), so they stay static."""
    snapshot = runtime.stateStore.current()
    for warning in () if snapshot is None else snapshot.configWarnings:
        st.warning(escapeMarkdown(configWarningLine(warning)))


def renderStatus(runtime) -> None:
    snapshot = runtime.stateStore.current()
    if snapshot is None:
        st.info(WAITING_MESSAGE)
        return
    model = buildViewModel(snapshot, runtime.clock.now())
    if NavigationController.currentView(st.session_state) == VIEW_MANAGE:
        renderManageView(runtime, model)
    else:
        renderStatusView(runtime, model)


def statusArea(runtime) -> None:
    """The auto-refreshing part of the page; every run is its own request."""
    guarded(renderStatus, runtime)


def main() -> None:
    st.set_page_config(page_title=PAGE_TITLE, layout="wide")
    st.title(PAGE_TITLE, anchor=False)
    runtime = getRuntime()
    if runtime is None:
        st.error(NOT_STARTED_MESSAGE)
        st.stop()
    st.session_state.setdefault(DISMISSED_KEY, set())
    guarded(renderConfigWarnings, runtime)
    interval = runtime.config.dashboardRefreshIntervalSeconds  # startup config (REQ AC-14)
    st.fragment(run_every=interval)(statusArea)(runtime)


main()
