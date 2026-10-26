"""Dedicated self-service management view with semantic tabs (REQ-003, TASK-021)."""

import streamlit as st

from pc_handoff.ui.navigation import (
    NAV_TO_STATUS_KEY,
    NAV_TO_STATUS_LABEL,
    NavigationController,
)
from pc_handoff.ui.view_model import (
    NOTE_CATEGORY_LABELS,
    RESERVATION_COLUMNS,
    DashboardViewModel,
    escapeMarkdown,
    generateRdpFileContent,
    resolveRdpHost,
    sanitize,
)
from pc_handoff.ui.views.status_view import FEEDBACK_ERROR_KEY, table

TAB_JOIN_QUEUE = "➕ Join Queue"
TAB_READY_TO_HANDOFF = "🤝 Ready to Handoff"
TAB_POST_STATUS_NOTE = "📝 Post Status Note"


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


def handleUpdateReservation(manager, reservationId: str) -> None:
    duration = int(st.session_state.get(f"edit-duration-{reservationId}", 60))
    notes = st.session_state.get(f"edit-notes-{reservationId}", "") or None
    _runAction(
        lambda: manager.updateReservation(
            reservationId,
            requestedDurationMinutes=duration,
            notes=notes,
        )
    )


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
    category = st.session_state.get("noteCategory", "general")
    ttl = int(st.session_state.get("noteTtlMinutes", 120))
    _runAction(
        lambda: manager.postStatusNote(
            userName,
            message,
            ttlMinutes=ttl,
            category=category,
        )
    )


def handleClearNote(manager, noteId: str) -> None:
    _runAction(lambda: manager.clearStatusNote(noteId))


def renderManageView(runtime, model: DashboardViewModel) -> None:
    """Render the dedicated Manage Queue & Notes view with three semantic tabs."""
    manager = getattr(runtime, "reservationManager", None)
    st.button(
        NAV_TO_STATUS_LABEL,
        key=NAV_TO_STATUS_KEY,
        on_click=lambda: NavigationController.goToStatus(st.session_state),
    )
    if manager is None:
        return

    feedbackError = st.session_state.pop(FEEDBACK_ERROR_KEY, None)
    if feedbackError:
        st.warning(escapeMarkdown(feedbackError))

    tabJoin, tabHandoff, tabNotes = st.tabs(
        [TAB_JOIN_QUEUE, TAB_READY_TO_HANDOFF, TAB_POST_STATUS_NOTE]
    )

    with tabJoin:
        st.info(escapeMarkdown(model.waitEstimateLine))
        if model.reservationRows:
            st.subheader(escapeMarkdown(model.reservationHeading), anchor=False)
            st.dataframe(table(model.reservationRows, RESERVATION_COLUMNS), hide_index=True)
        else:
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

        for row in model.reservationRows:
            if row.canEdit:
                st.number_input(
                    f"Update duration for {row.user} (minutes)",
                    min_value=5,
                    max_value=480,
                    value=row.durationMinutes,
                    step=15,
                    key=f"edit-duration-{row.reservationId}",
                )
                st.text_input(
                    f"Update notes for {row.user}",
                    value=row.rawNotes,
                    max_chars=280,
                    key=f"edit-notes-{row.reservationId}",
                )
                st.button(
                    f"Save Changes ({row.user})",
                    key=f"update-{row.reservationId}",
                    on_click=handleUpdateReservation,
                    args=(manager, row.reservationId),
                )
            st.button(
                f"Cancel ({row.user})",
                key=f"cancel-{row.reservationId}",
                on_click=handleCancel,
                args=(manager, row.reservationId),
            )

    with tabHandoff:
        if model.handoffBanner is not None:
            st.markdown(escapeMarkdown(model.handoffBanner))
        st.text_input(
            "Current user offering handoff (optional)", key="handoffOfferer", max_chars=64
        )
        st.button(
            "Ready to Handoff",
            key="initiateHandoffBtn",
            on_click=handleInitiateHandoff,
            args=(manager,),
        )
        for row in model.reservationRows:
            if row.canClaim:
                st.button(
                    f"Claim Now ({row.user})",
                    key=f"claim-{row.reservationId}",
                    on_click=handleClaim,
                    args=(manager, row.reservationId),
                )
        st.download_button(
            label="Download .rdp Connection File",
            data=generateRdpFileContent(host=resolveRdpHost()),
            file_name="pc-handoff.rdp",
            mime="application/x-rdp",
            key="downloadRdpBtn",
        )

    with tabNotes:
        st.text_input("Note author", key="noteUserName", max_chars=64)
        st.text_input("Status message", key="noteMessage", max_chars=280)
        st.selectbox(
            "Category",
            options=list(NOTE_CATEGORY_LABELS.keys()),
            format_func=lambda c: NOTE_CATEGORY_LABELS.get(c, c),
            key="noteCategory",
        )
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
        if model.statusNoteItems:
            st.subheader(escapeMarkdown(model.statusNotesHeading), anchor=False)
            for note in model.statusNoteItems:
                st.markdown(escapeMarkdown(note.line))
                st.button(
                    f"Clear note ({note.user})",
                    key=f"clear-note-{note.noteId}",
                    on_click=handleClearNote,
                    args=(manager, note.noteId),
                )
