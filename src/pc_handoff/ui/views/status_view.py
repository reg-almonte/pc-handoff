"""Main passive PC Status monitoring view (REQ-003, TASK-021)."""

import pandas as pd
import streamlit as st

from pc_handoff.ui.navigation import (
    LEGACY_CONTROLS_KEY,
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
RESERVATION_CONTROLS_LABEL = "Manage reservations & status notes"
FEEDBACK_ERROR_KEY = "reservationFeedbackError"


def dismissNotice(noticeId: str) -> None:
    """Button callback: hide one notice for this browser session only."""
    st.session_state[DISMISSED_KEY].add(noticeId)


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


def renderStatusView(runtime, model: DashboardViewModel) -> None:
    """Render the passive PC Status view and header navigation to ManageView."""
    manager = getattr(runtime, "reservationManager", None)
    if manager is not None:
        _, navCol = st.columns([3, 1])
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
        st.checkbox(RESERVATION_CONTROLS_LABEL, key=LEGACY_CONTROLS_KEY)
