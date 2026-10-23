"""The PC Handoff status dashboard (TASK-010, ADR-008). Streamlit runs this file as a script.

The page only reads the runtime the launcher registered (ADR-002): its startup ``config``,
its ``clock`` and ``stateStore.current()``, a plain reference read of an immutable snapshot.
It never calls a store, the tracker or the session source, so rendering cannot trigger or
wait on a poll (REQ AC-21).

- Config warnings are static for the process lifetime and sit outside the fragment.
- The status area is an ``st.fragment`` re-run every ``dashboardRefreshIntervalSeconds``
  from the startup config (REQ AC-14). Each run gets a fresh traceId (requirement
  section 8); a rendering error is logged with it and shown as a short error, never a crash.
- Notices can be dismissed per browser session (``st.session_state["dismissedNotices"]``).
- Safe rendering: user- and OS-derived text is sanitized by the view model and shown only in
  dataframe cells, ``st.text`` or, in markdown-rendering elements, after ``escapeMarkdown``.
  No element allows HTML. There is no login (REQ AC-13).
"""

import logging

import pandas as pd
import streamlit as st

from pc_handoff.app_logging import LOGGER_NAME, newTraceId, traceScope
from pc_handoff.runtime.registry import getRuntime
from pc_handoff.ui.view_model import (
    ACTIVE_COLUMNS,
    BADGE_ERROR,
    BADGE_SUCCESS,
    DISCONNECTED_COLUMNS,
    DashboardViewModel,
    buildViewModel,
    configWarningLine,
    escapeMarkdown,
    visibleNotices,
)

PAGE_TITLE = "PC Handoff"
NOT_STARTED_MESSAGE = "Start PC Handoff with: python -m pc_handoff"
WAITING_MESSAGE = "Waiting for the first status update."
DISMISSED_KEY = "dismissedNotices"
DISMISS_LABEL = "Dismiss"

logger = logging.getLogger(f"{LOGGER_NAME}.ui")


def dismissNotice(noticeId: str) -> None:
    """Button callback: hide one notice for this browser session only."""
    st.session_state[DISMISSED_KEY].add(noticeId)


def table(rows, columns) -> pd.DataFrame:
    """Plain text cells: the grid never interprets markdown or HTML."""
    return pd.DataFrame([row.cells() for row in rows], columns=list(columns), dtype="string")


def renderBadge(model: DashboardViewModel) -> None:
    text = escapeMarkdown(model.badgeText)
    if model.badgeKind == BADGE_SUCCESS:
        st.success(text)
    elif model.badgeKind == BADGE_ERROR:
        st.error(text)
    else:
        st.info(text)


def renderViewModel(model: DashboardViewModel) -> None:
    renderBadge(model)
    if model.unavailableBanner is not None:
        st.warning(escapeMarkdown(model.unavailableBanner))
        st.warning(escapeMarkdown(model.lastKnownLine or ""))
    for item in visibleNotices(model.noticeItems, st.session_state[DISMISSED_KEY]):
        st.warning(escapeMarkdown(item.text))
        st.button(DISMISS_LABEL, key=item.noticeId, on_click=dismissNotice, args=(item.noticeId,))

    st.subheader(escapeMarkdown(model.activeHeading), anchor=False)
    if model.activeRows:
        st.dataframe(table(model.activeRows, ACTIVE_COLUMNS), hide_index=True)
    else:
        st.text(model.emptyActiveMessage)
    if model.disconnectedRows:
        st.subheader(escapeMarkdown(model.disconnectedHeading), anchor=False)
        st.dataframe(table(model.disconnectedRows, DISCONNECTED_COLUMNS), hide_index=True)

    st.caption(escapeMarkdown(model.lastUpdatedLine))
    st.caption(escapeMarkdown(model.footer))


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
    renderViewModel(buildViewModel(snapshot, runtime.clock.now()))


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
