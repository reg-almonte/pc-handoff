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
    RESERVATION_COLUMNS,
    DashboardViewModel,
    buildViewModel,
    configWarningLine,
    escapeMarkdown,
    sanitize,
    visibleNotices,
)

PAGE_TITLE = "PC Handoff"
NOT_STARTED_MESSAGE = "Start PC Handoff with: python -m pc_handoff"
WAITING_MESSAGE = "Waiting for the first status update."
DISMISSED_KEY = "dismissedNotices"
DISMISS_LABEL = "Dismiss"
RESERVATION_CONTROLS_KEY = "showReservationControls"
RESERVATION_CONTROLS_LABEL = "Manage reservations & status notes"
FEEDBACK_ERROR_KEY = "reservationFeedbackError"

logger = logging.getLogger(f"{LOGGER_NAME}.ui")


def dismissNotice(noticeId: str) -> None:
    """Button callback: hide one notice for this browser session only."""
    st.session_state[DISMISSED_KEY].add(noticeId)


def _runAction(action) -> None:
    """Run a reservation or status-note callback, recording any validation error for the UI."""
    try:
        action()
    except ValueError as exc:
        st.session_state[FEEDBACK_ERROR_KEY] = sanitize(str(exc))


def handleJoinQueue(manager) -> None:
    userName = st.session_state.get("joinUserName", "")
    duration = int(st.session_state.get("joinDurationMinutes", 60))
    notes = st.session_state.get("joinNotes", "") or None
    _runAction(lambda: manager.enqueueReservation(userName, duration, notes=notes))


def handleCancel(manager, reservationId: str) -> None:
    _runAction(lambda: manager.cancelReservation(reservationId))


def handleClaim(manager, reservationId: str) -> None:
    _runAction(lambda: manager.claimHandoff(reservationId))


def handleInitiateHandoff(manager) -> None:
    offerer = st.session_state.get("handoffOfferer", "").strip() or None
    _runAction(lambda: manager.initiateHandoff(offerer))


def handlePostStatusNote(manager) -> None:
    userName = st.session_state.get("noteUserName", "")
    message = st.session_state.get("noteMessage", "")
    ttl = int(st.session_state.get("noteTtlMinutes", 120))
    _runAction(lambda: manager.postStatusNote(userName, message, ttlMinutes=ttl))


def handleClearNote(manager, noteId: str) -> None:
    _runAction(lambda: manager.clearStatusNote(noteId))


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

    if model.handoffBanner is not None:
        st.markdown(escapeMarkdown(model.handoffBanner))
    if model.reservationRows:
        st.subheader(escapeMarkdown(model.reservationHeading), anchor=False)
        st.dataframe(table(model.reservationRows, RESERVATION_COLUMNS), hide_index=True)
    if model.statusNoteItems:
        st.subheader(escapeMarkdown(model.statusNotesHeading), anchor=False)
        for note in model.statusNoteItems:
            st.markdown(escapeMarkdown(note.line))

    st.caption(escapeMarkdown(model.lastUpdatedLine))
    st.caption(escapeMarkdown(model.footer))


def renderReservationControls(runtime, model: DashboardViewModel) -> None:
    manager = getattr(runtime, "reservationManager", None)
    if manager is None:
        return
    feedbackError = st.session_state.pop(FEEDBACK_ERROR_KEY, None)
    if feedbackError:
        st.warning(escapeMarkdown(feedbackError))

    for row in model.reservationRows:
        if row.canClaim:
            st.button(
                f"Claim Now ({row.user})",
                key=f"claim-{row.reservationId}",
                on_click=handleClaim,
                args=(manager, row.reservationId),
            )
        st.button(
            f"Cancel ({row.user})",
            key=f"cancel-{row.reservationId}",
            on_click=handleCancel,
            args=(manager, row.reservationId),
        )

    for note in model.statusNoteItems:
        st.button(
            f"Clear note ({note.user})",
            key=f"clear-note-{note.noteId}",
            on_click=handleClearNote,
            args=(manager, note.noteId),
        )

    if st.checkbox(RESERVATION_CONTROLS_LABEL, key=RESERVATION_CONTROLS_KEY):
        if not model.reservationRows:
            st.text(model.emptyQueueMessage or "")
        st.text_input("Your user name", key="joinUserName", max_chars=64)
        st.number_input(
            "Estimated duration (minutes)",
            min_value=5,
            max_value=480,
            value=60,
            step=15,
            key="joinDurationMinutes",
        )
        st.text_input("Notes (optional)", key="joinNotes", max_chars=280)
        st.button("Join Queue", key="joinQueueBtn", on_click=handleJoinQueue, args=(manager,))

        st.text_input(
            "Current user offering handoff (optional)", key="handoffOfferer", max_chars=64
        )
        st.button(
            "Ready to Handoff",
            key="initiateHandoffBtn",
            on_click=handleInitiateHandoff,
            args=(manager,),
        )

        st.text_input("Note author", key="noteUserName", max_chars=64)
        st.text_input("Status message", key="noteMessage", max_chars=280)
        st.number_input(
            "Note TTL (minutes)",
            min_value=5,
            max_value=1440,
            value=120,
            step=15,
            key="noteTtlMinutes",
        )
        st.button(
            "Post Status Note",
            key="postStatusNoteBtn",
            on_click=handlePostStatusNote,
            args=(manager,),
        )


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
    renderViewModel(model)
    renderReservationControls(runtime, model)


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
