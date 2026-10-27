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

PAGE_TITLE = "PC Handoff"
DISMISSED_KEY = "dismissedNotices"
DISMISS_LABEL = "Dismiss"
DISMISSED_TOP_NOTE_KEY = "dismissedTopNoteId"
CLOSE_TOP_NOTE_KEY = "closeTopStatusNoteBtn"
CLOSE_TOP_NOTE_LABEL = "✕"
STATUS_IN_USE_BTN_KEY = "statusInUseBadgeBtn"
STATUS_QUEUE_BTN_KEY = "statusQueueBadgeBtn"
STATUS_NOTES_ANCHOR = "status-notes"
ACTIVE_SESSIONS_ANCHOR = "active-sessions"
RESERVATION_QUEUE_ANCHOR = "reservation-queue"

EXPAND_ALL_KEY = "expandAllSectionsBtn"
COLLAPSE_ALL_KEY = "collapseAllSectionsBtn"
SESSION_ACTIVE_OPEN_KEY = "expander_active_open"
SESSION_QUEUE_OPEN_KEY = "expander_queue_open"
SESSION_NOTES_OPEN_KEY = "expander_notes_open"
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


def _onInUseBadgeClick() -> None:
    st.session_state[SESSION_ACTIVE_OPEN_KEY] = True


def _onQueueBadgeClick() -> None:
    st.session_state[SESSION_QUEUE_OPEN_KEY] = True


def renderBadge(model: DashboardViewModel) -> None:
    if model.badgeKind == BADGE_ERROR:
        st.button(
            model.badgeText,
            key=STATUS_IN_USE_BTN_KEY,
            on_click=_onInUseBadgeClick,
            use_container_width=True,
            help="Click to expand Active sessions",
        )
    elif model.badgeKind == BADGE_SUCCESS:
        st.success(model.badgeText)
    else:
        st.info(model.badgeText)


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


def renderSessionsCard(model: DashboardViewModel, *, expanded: bool = False) -> None:
    """Render the collapsible Active and Disconnected sessions section."""
    label = f"🖥️ {escapeMarkdown(model.activeHeading)} ({len(model.activeRows)})"
    with st.expander(label, expanded=expanded):
        st.subheader(escapeMarkdown(model.activeHeading), anchor=ACTIVE_SESSIONS_ANCHOR)
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


def renderReservationCard(
    model: DashboardViewModel, *, manager=None, expanded: bool = False
) -> None:
    """Render the collapsible Reservation Queue section with inline Claim Now button."""
    label = f"📋 {escapeMarkdown(model.reservationHeading)} ({len(model.reservationRows)})"
    with st.expander(label, expanded=expanded):
        headerCol, actionCol = st.columns([4, 1], vertical_alignment="center")
        if model.reservationRows:
            with headerCol:
                st.subheader(
                    escapeMarkdown(model.reservationHeading), anchor=RESERVATION_QUEUE_ANCHOR
                )
        if manager is not None:
            with actionCol:
                for row in model.reservationRows:
                    if row.canClaim:
                        st.button(
                            f"Claim Now ({row.user})",
                            key=f"claim-{row.reservationId}",
                            on_click=_handleQuickClaim,
                            args=(manager, row.reservationId),
                            use_container_width=True,
                        )
        if model.handoffBanner is not None:
            st.markdown(escapeMarkdown(model.handoffBanner))
        if model.reservationRows:
            st.dataframe(
                table(model.reservationRows, RESERVATION_COLUMNS),
                hide_index=True,
                use_container_width=True,
            )


def renderViewModel(model: DashboardViewModel, *, manager=None) -> None:
    expandParam = st.query_params.get("expand")
    if expandParam == "active":
        st.session_state[SESSION_ACTIVE_OPEN_KEY] = True
    elif expandParam == "queue":
        st.session_state[SESSION_QUEUE_OPEN_KEY] = True
    elif expandParam == "notes":
        st.session_state[SESSION_NOTES_OPEN_KEY] = True

    with st.container(border=True):
        statusCol, queueCol, _spacer, waitCol = st.columns(
            [3.2, 3.0, 0.4, 3.4], vertical_alignment="center"
        )
        with statusCol:
            renderBadge(model)
        with queueCol:
            if model.nextInQueueUser:
                st.button(
                    f"⏳ {model.nextInQueueText}",
                    key=STATUS_QUEUE_BTN_KEY,
                    on_click=_onQueueBadgeClick,
                    use_container_width=True,
                    help="Click to expand Reservation queue",
                )
            else:
                with st.container(border=True):
                    st.markdown(f"⏳ **{escapeMarkdown(model.nextInQueueText)}**")
        with waitCol, st.container(border=True):
            st.markdown(f"🕒 **{escapeMarkdown(model.compactWaitText)}**")
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

    ctrlCol1, ctrlCol2, _ctrlSpacer = st.columns([1, 1, 8])
    with ctrlCol1:
        if st.button("Expand all", key=EXPAND_ALL_KEY):
            st.session_state[SESSION_ACTIVE_OPEN_KEY] = True
            st.session_state[SESSION_QUEUE_OPEN_KEY] = True
            st.session_state[SESSION_NOTES_OPEN_KEY] = True
    with ctrlCol2:
        if st.button("Collapse all", key=COLLAPSE_ALL_KEY):
            st.session_state[SESSION_ACTIVE_OPEN_KEY] = False
            st.session_state[SESSION_QUEUE_OPEN_KEY] = False
            st.session_state[SESSION_NOTES_OPEN_KEY] = False

    activeExpanded = st.session_state.get(SESSION_ACTIVE_OPEN_KEY, False)
    queueExpanded = st.session_state.get(SESSION_QUEUE_OPEN_KEY, False)
    notesExpanded = st.session_state.get(SESSION_NOTES_OPEN_KEY, False)

    renderSessionsCard(model, expanded=activeExpanded)

    if model.handoffBanner is not None or model.reservationRows:
        renderReservationCard(model, manager=manager, expanded=queueExpanded)

    if model.statusNoteItems:
        notesLabel = f"📌 {escapeMarkdown(model.statusNotesHeading)} ({len(model.statusNoteItems)})"
        with st.expander(notesLabel, expanded=notesExpanded):
            st.subheader(escapeMarkdown(model.statusNotesHeading), anchor=STATUS_NOTES_ANCHOR)
            for note in model.statusNoteItems:
                st.markdown(escapeMarkdown(note.line))

    st.divider()
    footerLeft, _footerSpacer, footerRight = st.columns([3, 2, 2], vertical_alignment="center")
    with footerLeft:
        st.caption(escapeMarkdown(model.lastUpdatedLine))
    with footerRight:
        st.caption(escapeMarkdown(model.footer))


def renderStatusView(runtime, model: DashboardViewModel) -> None:
    """Render the passive PC Status view and header navigation to ManageView."""
    manager = getattr(runtime, "reservationManager", None)
    titleCol, navCol = st.columns([4, 1], vertical_alignment="center")
    with titleCol:
        st.title(PAGE_TITLE, anchor=False)
        if manager is not None:
            st.caption(escapeMarkdown(MANAGE_INSTRUCTION_TEXT))
    if manager is not None:
        with navCol:
            st.button(
                NAV_TO_MANAGE_LABEL,
                key=NAV_TO_MANAGE_KEY,
                on_click=lambda: NavigationController.goToManage(st.session_state),
                use_container_width=True,
            )

    renderViewModel(model, manager=manager)
