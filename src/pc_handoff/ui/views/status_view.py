"""Main passive PC Status monitoring view (REQ-003, TASK-021, TASK-023)."""

import pandas as pd
import streamlit as st

from pc_handoff.ui.navigation import (
    NAV_TO_MANAGE_KEY,
    NAV_TO_MANAGE_LABEL,
    NavigationController,
)
from pc_handoff.ui.view_model import (
    ACTIVE_COLUMNS,
    BADGE_ERROR,
    BADGE_SUCCESS,
    DISCONNECTED_COLUMNS,
    RESERVATION_COLUMNS,
    DashboardViewModel,
    escapeMarkdown,
    sanitize,
    visibleNotices,
)

DISMISSED_KEY = "dismissedNotices"
DISMISS_LABEL = "Dismiss"
DISMISSED_TOP_NOTE_KEY = "dismissedTopNoteId"
CLOSE_TOP_NOTE_KEY = "closeTopStatusNoteBtn"
CLOSE_TOP_NOTE_LABEL = "✕"
STATUS_NOTES_ANCHOR = "status-notes"
MANAGE_INSTRUCTION_TEXT = (
    "To join the queue, offer a handoff, or post a status note, "
    "use the ⚙️ Manage Queue & Notes button at the upper right."
)
FEEDBACK_ERROR_KEY = "reservationFeedbackError"


def dismissNotice(noticeId: str) -> None:
    """Button callback: hide one notice for this browser session only."""
    st.session_state[DISMISSED_KEY].add(noticeId)


def dismissTopStatusNote(noteId: str) -> None:
    """Button callback: temporarily close the top status-note banner until page refresh."""
    st.session_state[DISMISSED_TOP_NOTE_KEY] = noteId


def _handleQuickClaim(manager, reservationId: str) -> None:
    try:
        manager.claimHandoff(reservationId)
    except ValueError as exc:
        st.session_state[FEEDBACK_ERROR_KEY] = sanitize(str(exc))


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


def renderTopStatusNoteBanner(model: DashboardViewModel) -> None:
    """Render the top active Status Note as a highlighted banner with jump link and close button."""
    if not model.statusNoteItems:
        return
    topNote = model.statusNoteItems[0]
    if st.session_state.get(DISMISSED_TOP_NOTE_KEY) == topNote.noteId:
        return
    bannerCol, closeCol = st.columns([11, 1], vertical_alignment="center")
    with bannerCol:
        bannerText = (
            f"📌 **Status Note [{escapeMarkdown(topNote.categoryLabel)}]** — "
            f"**{escapeMarkdown(topNote.user)}**: {escapeMarkdown(topNote.message)} "
            f"([Jump to Status notes ↓](#{STATUS_NOTES_ANCHOR}))"
        )
        st.info(bannerText)
    with closeCol:
        st.button(
            CLOSE_TOP_NOTE_LABEL,
            key=CLOSE_TOP_NOTE_KEY,
            on_click=dismissTopStatusNote,
            args=(topNote.noteId,),
        )


def renderViewModel(model: DashboardViewModel) -> None:
    with st.container(border=True):
        renderBadge(model)
        renderTopStatusNoteBanner(model)
        if model.unavailableBanner is not None:
            st.warning(escapeMarkdown(model.unavailableBanner))
            st.warning(escapeMarkdown(model.lastKnownLine or ""))
        for item in visibleNotices(model.noticeItems, st.session_state[DISMISSED_KEY]):
            st.warning(escapeMarkdown(item.text))
            st.button(
                DISMISS_LABEL,
                key=item.noticeId,
                on_click=dismissNotice,
                args=(item.noticeId,),
            )

    with st.container(border=True):
        st.subheader(escapeMarkdown(model.activeHeading), anchor=False)
        if model.activeRows:
            st.dataframe(
                table(model.activeRows, ACTIVE_COLUMNS),
                hide_index=True,
                use_container_width=True,
            )
        else:
            st.text(model.emptyActiveMessage)
        if model.disconnectedRows:
            st.divider()
            st.subheader(escapeMarkdown(model.disconnectedHeading), anchor=False)
            st.dataframe(
                table(model.disconnectedRows, DISCONNECTED_COLUMNS),
                hide_index=True,
                use_container_width=True,
            )

    if model.handoffBanner is not None or model.reservationRows:
        with st.container(border=True):
            if model.handoffBanner is not None:
                st.markdown(escapeMarkdown(model.handoffBanner))
            if model.reservationRows:
                st.subheader(escapeMarkdown(model.reservationHeading), anchor=False)
                st.dataframe(
                    table(model.reservationRows, RESERVATION_COLUMNS),
                    hide_index=True,
                    use_container_width=True,
                )

    if model.statusNoteItems:
        with st.container(border=True):
            st.subheader(escapeMarkdown(model.statusNotesHeading), anchor=STATUS_NOTES_ANCHOR)
            for note in model.statusNoteItems:
                st.markdown(escapeMarkdown(note.line))

    st.divider()
    st.caption(escapeMarkdown(model.lastUpdatedLine))
    st.caption(escapeMarkdown(model.footer))


def renderStatusView(runtime, model: DashboardViewModel) -> None:
    """Render the passive PC Status view and header navigation to ManageView."""
    manager = getattr(runtime, "reservationManager", None)
    if manager is not None:
        _, navCol = st.columns([5, 1], vertical_alignment="center")
        with navCol:
            st.button(
                NAV_TO_MANAGE_LABEL,
                key=NAV_TO_MANAGE_KEY,
                on_click=lambda: NavigationController.goToManage(st.session_state),
                use_container_width=True,
            )

    renderViewModel(model)

    if manager is not None:
        for row in model.reservationRows:
            if row.canClaim:
                st.button(
                    f"Claim Now ({row.user})",
                    key=f"claim-{row.reservationId}",
                    on_click=_handleQuickClaim,
                    args=(manager, row.reservationId),
                )
        st.caption(escapeMarkdown(MANAGE_INSTRUCTION_TEXT))
