"""Pure dashboard view model (ADR-008): a ``DashboardSnapshot`` plus ``now`` in, display text out.

Everything the page shows is computed here as plain strings, so all text-level behaviour is
unit-testable without Streamlit:

- the badge (``Available`` / ``In Use`` / ``Unknown``) and its kind;
- the Unknown banner, "Last known status: ..." and the frozen "Last known sessions" view
  (REQ AC-11, AC-27): durations are ``lastSuccessfulPollAt - usagePeriodStart`` while the
  status is Unknown, and ``now - usagePeriodStart`` otherwise;
- ``HH:MM:SS`` durations with unbounded hours and the " (approx.)" suffix (REQ AC-10);
- ``Unknown`` for missing client fields (REQ AC-9, AC-25), "unknown" for a null
  ``disconnectedSince`` (TASK-005 F-note);
- config warning lines (REQ AC-15) and notices (REQ AC-17, AC-18).

All user- and OS-derived text passes through ``sanitize`` (Unicode control and format
characters removed). The page renders it only through dataframe cells, ``st.text`` or, for
markdown-rendering elements, ``escapeMarkdown`` (Security constraint).
"""

import os
import string
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo

from pc_handoff.detection.provider import ConnectionType
from pc_handoff.domain.models import PcStatus, SessionState, TrackedSession
from pc_handoff.domain.reservations import (
    HandoffState,
    HandoffStatus,
    NoteCategory,
    QueueState,
    Reservation,
    ReservationStatus,
    StatusNote,
    estimateQueueWait,
)
from pc_handoff.persistence.paths import CONFIG_FILE_NAME
from pc_handoff.runtime.state_store import DashboardSnapshot

BADGE_SUCCESS = "success"
BADGE_ERROR = "error"
BADGE_NEUTRAL = "neutral"
BADGE_KINDS = {
    PcStatus.AVAILABLE: BADGE_SUCCESS,
    PcStatus.IN_USE: BADGE_ERROR,
}

UNKNOWN_TEXT = "Unknown"
NOT_APPLICABLE_TEXT = "N/A"
UNKNOWN_DISCONNECTED_SINCE = "unknown"
APPROX_SUFFIX = " (approx.)"
UNAVAILABLE_BANNER = "Session information is currently unavailable"
LAST_KNOWN_NONE = "Last known status: none"
ACTIVE_HEADING = "Active sessions"
LAST_KNOWN_HEADING = "Last known sessions"
EMPTY_ACTIVE_MESSAGE = "No active user sessions"
DISCONNECTED_HEADING = "Logged on but disconnected (not in use)"
CONFIG_WARNING_PREFIX = f"Configuration ({CONFIG_FILE_NAME}): "
# For a warning that already names the file (config_store's unreadable-file warning).
CONFIG_FILE_WARNING_PREFIX = "Configuration: "
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

ACTIVE_COLUMNS = (
    "User",
    "Connection type",
    "Client name",
    "Client IP",
    "Usage period start",
    "Duration",
)
DISCONNECTED_COLUMNS = ("User", "Disconnected since")
RESERVATION_HEADING = "Reservation queue"
EMPTY_QUEUE_MESSAGE = "No active or queued reservations"
STATUS_NOTES_HEADING = "Status notes"
DEFAULT_WAIT_ESTIMATE_LINE = "Estimated wait: ~0 min (Ready now)"
DEFAULT_COMPACT_WAIT_TEXT = "Est. wait: Ready now (~0 min)"
NOTE_CATEGORY_LABELS: dict[str, str] = {
    NoteCategory.GENERAL.value: "General",
    NoteCategory.URGENT.value: "Urgent / Do Not Interrupt",
    NoteCategory.MAINTENANCE.value: "Maintenance",
    NoteCategory.LONG_RUNNING.value: "Long-Running Job",
}
RESERVATION_COLUMNS = (
    "Position",
    "User",
    "Status",
    "Requested duration",
    "Queued at",
    "Claim expires in",
    "Notes",
)

# Unicode "Other, control" (C0, DEL, C1) and "Other, format" (bidi overrides and isolates,
# zero-width characters, BOM, soft hyphen): invisible, and able to reorder or hide text.
REMOVED_CATEGORIES = frozenset({"Cc", "Cf"})
MARKDOWN_SPECIALS = frozenset(string.punctuation)


EXPAND_ACTIVE_LINK = "#active-sessions"
EXPAND_QUEUE_LINK = "#reservation-queue"
EXPAND_NOTES_LINK = "#status-notes"


def formatDuration(seconds: float) -> str:
    """``HH:MM:SS``; hours are zero-padded to two digits and never capped (``27:03:09``).

    Fractions are truncated and a negative value (wall clock moved back) shows as zero.
    """
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def localZone() -> tzinfo | None:
    """The zone times are shown in; None means the host's local zone (``astimezone``).

    A seam for tests, so the conversion is checked whatever the test host's zone is.
    """
    return None


def formatTime(dt: datetime) -> str:
    """Host-local ``YYYY-MM-DD HH:MM:SS`` (ADR-008 time display)."""
    return dt.astimezone(localZone()).strftime(TIME_FORMAT)


def utcOffsetLabel(offset: timedelta | None) -> str:
    """``UTC+09:00`` style label for the page footer."""
    if offset is None:
        return "UTC"
    sign = "-" if offset < timedelta(0) else "+"
    minutes = abs(int(offset.total_seconds())) // 60
    return f"UTC{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def sanitize(text: str) -> str:
    """Remove control and format characters (Unicode categories Cc and Cf).

    That covers ``\\x00``-``\\x1f``, ``\\x7f``, the C1 controls, bidi overrides and isolates
    (U+202A-U+202E, U+2066-U+2069), zero-width characters and U+FEFF. A Remote Desktop
    client chooses its own client name, so these could otherwise make it display as another
    name or address (Critic TASK-010 N1). Letters of every script, accents, spaces and single
    emoji are kept. Sequences that rely on Cf joiners or tags are not preserved: a ZWJ emoji
    sequence shows as its separate members, a ZWNJ inside a Persian word is dropped, and
    subdivision flags lose their tag characters. Only the display is affected, not stored data.
    """
    return "".join(char for char in text if unicodedata.category(char) not in REMOVED_CATEGORIES)


def displayOrUnknown(value: str | None) -> str:
    """The sanitized value, or ``Unknown`` when it is None, empty or blank (REQ AC-9)."""
    if value is None:
        return UNKNOWN_TEXT
    cleaned = sanitize(value)
    return cleaned if cleaned.strip() else UNKNOWN_TEXT


def escapeMarkdown(text: str) -> str:
    """Backslash-escape every ASCII punctuation character, so markdown shows ``text`` literally.

    A superset of the ADR-008 list: it also covers ``$`` (LaTeX), ``:`` (emoji shortcodes
    and colour directives) and ``&`` (HTML entities). CommonMark allows a backslash escape
    of any ASCII punctuation character, so the rendered text is unchanged.
    """
    return "".join("\\" + char if char in MARKDOWN_SPECIALS else char for char in text)


def configWarningLine(warning: str) -> str:
    """One banner line per config warning: "<field>: <problem>; default <value> applied"."""
    cleaned = sanitize(warning)
    if cleaned.startswith(f"{CONFIG_FILE_NAME}:"):  # already names the file (Critic N3)
        return CONFIG_FILE_WARNING_PREFIX + cleaned
    return CONFIG_WARNING_PREFIX + cleaned


# --- the view model -----------------------------------------------------------------------


@dataclass(frozen=True)
class ActiveRow:
    user: str
    connectionType: str
    clientName: str
    clientIp: str
    periodStart: str
    duration: str

    def cells(self) -> tuple[str, ...]:
        """The row in ``ACTIVE_COLUMNS`` order."""
        return (
            self.user,
            self.connectionType,
            self.clientName,
            self.clientIp,
            self.periodStart,
            self.duration,
        )


@dataclass(frozen=True)
class DisconnectedRow:
    user: str
    disconnectedSince: str

    def cells(self) -> tuple[str, ...]:
        """The row in ``DISCONNECTED_COLUMNS`` order."""
        return (self.user, self.disconnectedSince)


@dataclass(frozen=True)
class NoticeItem:
    noticeId: str
    text: str


@dataclass(frozen=True)
class ReservationRow:
    reservationId: str
    position: str
    user: str
    status: str
    requestedDuration: str
    queuedAt: str
    claimCountdown: str
    notes: str
    canClaim: bool
    canCancel: bool
    canEdit: bool = False
    durationMinutes: int = 60
    rawNotes: str = ""

    def cells(self) -> tuple[str, ...]:
        """The row in ``RESERVATION_COLUMNS`` order."""
        return (
            self.position,
            self.user,
            self.status,
            self.requestedDuration,
            self.queuedAt,
            self.claimCountdown,
            self.notes,
        )


@dataclass(frozen=True)
class StatusNoteItem:
    noteId: str
    user: str
    message: str
    postedAt: str
    expiresAt: str
    line: str
    category: str = NoteCategory.GENERAL.value
    categoryLabel: str = "General"


@dataclass(frozen=True)
class DashboardViewModel:
    """Display strings for one render. Optional texts are None when not shown."""

    badgeText: str
    badgeKind: str
    unavailableBanner: str | None
    lastKnownLine: str | None
    activeHeading: str
    activeRows: tuple[ActiveRow, ...]
    emptyActiveMessage: str | None
    disconnectedHeading: str
    disconnectedRows: tuple[DisconnectedRow, ...]
    lastUpdated: str
    dataUpdatedAt: str
    lastUpdatedLine: str
    configWarningLines: tuple[str, ...]
    noticeItems: tuple[NoticeItem, ...]
    footer: str
    reservationHeading: str = RESERVATION_HEADING
    reservationRows: tuple[ReservationRow, ...] = ()
    emptyQueueMessage: str | None = EMPTY_QUEUE_MESSAGE
    handoffBanner: str | None = None
    statusNotesHeading: str = STATUS_NOTES_HEADING
    statusNoteItems: tuple[StatusNoteItem, ...] = ()
    waitEstimateLine: str = DEFAULT_WAIT_ESTIMATE_LINE
    nextInQueueText: str = "Next in queue: None"
    compactWaitText: str = DEFAULT_COMPACT_WAIT_TEXT
    badgeLink: str | None = None
    nextInQueueLink: str | None = None
    nextInQueueUser: str | None = None
    statusRowWeights: tuple[float, float, float, float] = (20.0, 30.0, 35.0, 20.0)


def buildViewModel(snapshot: DashboardSnapshot, now: datetime) -> DashboardViewModel:
    """Turn ``snapshot`` into display text as of ``now`` (pure; no I/O)."""
    unknown = snapshot.status is PcStatus.UNKNOWN
    pollAt = snapshot.lastSuccessfulPollAt
    # While Unknown, durations are frozen at the last successful poll (REQ AC-11); without
    # one there is nothing to freeze at.
    durationsAt: datetime | None = pollAt if unknown else now

    activeRows = tuple(
        activeRow(s, durationsAt) for s in snapshot.sessions if s.state is SessionState.ACTIVE
    )
    disconnectedRows = tuple(
        disconnectedRow(s) for s in snapshot.sessions if s.state is SessionState.DISCONNECTED
    )
    resRows = reservationRows(snapshot.queueState, now)
    noteItems = statusNoteItems(snapshot.queueState.statusNotes, now)
    nextQText, nextQUser, nextQLink = nextInQueueInfo(snapshot.queueState, now)
    badgeLink = EXPAND_ACTIVE_LINK if snapshot.status is PcStatus.IN_USE else None
    badgeText = badgeTextFor(snapshot.status, activeRows)
    compactWait = compactWaitLine(snapshot.queueState, now)
    colWeights = statusRowColumnWeights(badgeText, nextQText, compactWait)
    lastUpdated = formatTime(now)
    dataUpdatedAt = formatTime(snapshot.updatedAt)
    return DashboardViewModel(
        badgeText=badgeText,
        badgeKind=BADGE_KINDS.get(snapshot.status, BADGE_NEUTRAL),
        badgeLink=badgeLink,
        unavailableBanner=UNAVAILABLE_BANNER if unknown else None,
        lastKnownLine=lastKnownLine(snapshot) if unknown else None,
        activeHeading=activeHeading(unknown, pollAt),
        activeRows=activeRows,
        emptyActiveMessage=None if activeRows else EMPTY_ACTIVE_MESSAGE,
        disconnectedHeading=DISCONNECTED_HEADING,
        disconnectedRows=disconnectedRows,
        lastUpdated=lastUpdated,
        dataUpdatedAt=dataUpdatedAt,
        lastUpdatedLine=f"Last updated: {lastUpdated} (data as of {dataUpdatedAt})",
        configWarningLines=tuple(configWarningLine(w) for w in snapshot.configWarnings),
        noticeItems=tuple(NoticeItem(n.noticeId, sanitize(n.text)) for n in snapshot.notices),
        footer=f"Times are shown in this PC's local time ({utcOffsetLabel(localOffset(now))}).",
        reservationHeading=RESERVATION_HEADING,
        reservationRows=resRows,
        emptyQueueMessage=None if resRows else EMPTY_QUEUE_MESSAGE,
        handoffBanner=handoffBannerLine(snapshot.queueState.handoff, now),
        statusNotesHeading=STATUS_NOTES_HEADING,
        statusNoteItems=noteItems,
        waitEstimateLine=waitEstimateText(snapshot.queueState, now),
        nextInQueueText=nextQText,
        nextInQueueUser=nextQUser,
        nextInQueueLink=nextQLink,
        compactWaitText=compactWait,
        statusRowWeights=colWeights,
    )


def localOffset(now: datetime) -> timedelta | None:
    return now.astimezone(localZone()).utcoffset()


def lastKnownLine(snapshot: DashboardSnapshot) -> str:
    if snapshot.lastKnownStatus is None:
        return LAST_KNOWN_NONE
    at = (
        UNKNOWN_TEXT
        if snapshot.lastKnownStatusAt is None
        else formatTime(snapshot.lastKnownStatusAt)
    )
    return f"Last known status: {snapshot.lastKnownStatus.value} at {at}"


def activeHeading(unknown: bool, pollAt: datetime | None) -> str:
    if not unknown:
        return ACTIVE_HEADING
    if pollAt is None:
        return LAST_KNOWN_HEADING
    return f"{LAST_KNOWN_HEADING} (as of {formatTime(pollAt)})"


def badgeTextFor(status: PcStatus, activeRows: Sequence[ActiveRow]) -> str:
    """Format the primary status badge text (includes icon and ``user (ip)`` when ``In Use``)."""
    if status is PcStatus.IN_USE:
        users = ", ".join(f"{row.user} ({row.clientIp})" for row in activeRows)
        return f"🔴 {status.value} — {users}"
    if status is PcStatus.AVAILABLE:
        return f"🟢 {status.value}"
    return status.value


def activeRow(session: TrackedSession, durationsAt: datetime | None) -> ActiveRow:
    period = session.openPeriod
    if period is None:  # defensive: an Active session always has an open period
        periodStart = duration = UNKNOWN_TEXT
    else:
        periodStart = formatTime(period.usagePeriodStart)
        if durationsAt is None:
            duration = UNKNOWN_TEXT
        else:
            elapsed = (durationsAt - period.usagePeriodStart).total_seconds()
            duration = formatDuration(elapsed)
            if period.durationApproximate:
                duration += APPROX_SUFFIX
    isConsole = session.connectionType is ConnectionType.CONSOLE
    return ActiveRow(
        user=displayOrUnknown(session.userName),
        connectionType=displayOrUnknown(session.connectionType.value),
        clientName=NOT_APPLICABLE_TEXT if isConsole else displayOrUnknown(session.clientName),
        clientIp=NOT_APPLICABLE_TEXT if isConsole else displayOrUnknown(session.clientAddress),
        periodStart=periodStart,
        duration=duration,
    )


def disconnectedRow(session: TrackedSession) -> DisconnectedRow:
    since = session.disconnectedSince
    return DisconnectedRow(
        user=displayOrUnknown(session.userName),
        disconnectedSince=UNKNOWN_DISCONNECTED_SINCE if since is None else formatTime(since),
    )


def visibleNotices(
    items: Iterable[NoticeItem], dismissed: Iterable[str] | None
) -> tuple[NoticeItem, ...]:
    """The notices this browser session has not dismissed (ADR-008)."""
    hidden = set(dismissed or ())
    return tuple(item for item in items if item.noticeId not in hidden)


def reservationRows(queueState: QueueState, now: datetime) -> tuple[ReservationRow, ...]:
    """Build display rows for all non-terminal reservations in queue order."""
    nonTerminal = queueState.nonTerminalReservations
    headQueuedId = (
        queueState.queuedReservations[0].reservationId
        if (
            queueState.activeReservation is None
            and queueState.readyReservation is None
            and queueState.queuedReservations
        )
        else None
    )
    rows: list[ReservationRow] = []
    for index, res in enumerate(nonTerminal, start=1):
        rows.append(reservationRow(res, index, now, headQueuedId=headQueuedId))
    return tuple(rows)


def reservationRow(
    res: Reservation,
    position: int,
    now: datetime,
    *,
    headQueuedId: str | None,
) -> ReservationRow:
    if res.status is ReservationStatus.READY and res.claimExpiresAt is not None:
        countdown = formatDuration((res.claimExpiresAt - now).total_seconds())
    else:
        countdown = "-"
    canClaim = res.status is ReservationStatus.READY or res.reservationId == headQueuedId
    rawNotes = sanitize(res.notes) if res.notes else ""
    return ReservationRow(
        reservationId=res.reservationId,
        position=str(position),
        user=displayOrUnknown(res.userName),
        status=res.status.value,
        requestedDuration=f"{res.requestedDurationMinutes} min",
        queuedAt=formatTime(res.queuedAt),
        claimCountdown=countdown,
        notes=rawNotes if rawNotes else "-",
        canClaim=canClaim,
        canCancel=True,
        canEdit=res.status is ReservationStatus.QUEUED,
        durationMinutes=res.requestedDurationMinutes,
        rawNotes=rawNotes,
    )


def handoffBannerLine(handoff: HandoffState, now: datetime) -> str | None:
    """Banner text for an active ``OFFERED`` or ``TIMED_OUT`` handoff, or ``None``."""
    if handoff.status is HandoffStatus.OFFERED and handoff.targetUserName is not None:
        target = displayOrUnknown(handoff.targetUserName)
        countdown = (
            formatDuration((handoff.expiresAt - now).total_seconds())
            if handoff.expiresAt is not None
            else UNKNOWN_TEXT
        )
        if handoff.offeredByUserName is not None:
            offerer = displayOrUnknown(handoff.offeredByUserName)
            return f"Handoff offered by {offerer} to {target} — claim within {countdown}"
        return f"PC is ready for {target} — claim within {countdown}"
    if handoff.status is HandoffStatus.TIMED_OUT and handoff.targetUserName is not None:
        target = displayOrUnknown(handoff.targetUserName)
        return f"Handoff to {target} timed out"
    return None


def statusNoteItems(
    notes: Iterable[StatusNote],
    now: datetime,
) -> tuple[StatusNoteItem, ...]:
    """Build display items for non-expired broadcast status notes."""
    items: list[StatusNoteItem] = []
    for note in notes:
        if note.expiresAt > now:
            user = displayOrUnknown(note.userName)
            message = sanitize(note.message)
            postedAt = formatTime(note.postedAt)
            expiresAt = formatTime(note.expiresAt)
            category = note.category.value
            categoryLabel = NOTE_CATEGORY_LABELS.get(category, "General")
            items.append(
                StatusNoteItem(
                    noteId=note.noteId,
                    user=user,
                    message=message,
                    postedAt=postedAt,
                    expiresAt=expiresAt,
                    line=(
                        f"[{user}] {message} [{categoryLabel}] "
                        f"(posted {postedAt}, expires {expiresAt})"
                    ),
                    category=category,
                    categoryLabel=categoryLabel,
                )
            )
    return tuple(items)


def waitEstimateText(queueState: QueueState, now: datetime) -> str:
    """Build the human-readable wait estimate line for ``queueState`` at ``now`` (REQ-003)."""
    est = estimateQueueWait(queueState, now)
    if est.waitMinutes <= 0:
        return DEFAULT_WAIT_ESTIMATE_LINE
    return (
        f"Estimated start time: ~{formatTime(est.estimatedStartAt)} (Wait: ~{est.waitMinutes} min)"
    )


def nextInQueueInfo(queueState: QueueState, now: datetime) -> tuple[str, str | None, str | None]:
    """Return ``(displayText, userOrNone, linkOrNone)`` for the top status strip."""
    if queueState.readyReservation is not None:
        user = displayOrUnknown(queueState.readyReservation.userName)
        if queueState.readyReservation.claimExpiresAt is not None:
            remaining = formatDuration(
                (queueState.readyReservation.claimExpiresAt - now).total_seconds()
            )
            text = f"Next in queue: {user} (Ready: {remaining})"
        else:
            text = f"Next in queue: {user} (Ready)"
        return text, user, EXPAND_QUEUE_LINK
    if queueState.queuedReservations:
        user = displayOrUnknown(queueState.queuedReservations[0].userName)
        return f"Next in queue: {user}", user, EXPAND_QUEUE_LINK
    return "Next in queue: None", None, None


def compactWaitLine(queueState: QueueState, now: datetime) -> str:
    """Build the compact 'Est. wait: ...' summary pill for the top status strip."""
    est = estimateQueueWait(queueState, now)
    if est.waitMinutes <= 0:
        return DEFAULT_COMPACT_WAIT_TEXT
    return f"Est. wait: ~{est.waitMinutes} min"


def statusRowColumnWeights(
    statusText: str,
    queueText: str,
    waitText: str,
    targetTotal: float = 105.0,
) -> tuple[float, float, float, float]:
    """Calculate proportional column weights so boxes hug content without looking stretched."""
    wStatus = max(14.0, float(len(statusText) + 8))
    wQueue = max(16.0, float(len(queueText) + 8))
    wWait = max(20.0, float(len(waitText) + 8))
    contentSum = wStatus + wQueue + wWait
    spacer = max(6.0, targetTotal - contentSum)
    return (round(wStatus, 1), round(wQueue, 1), round(wWait, 1), round(spacer, 1))


def resolveRdpHost() -> str:
    """Return the local PC host identifier for generated ``.rdp`` files without network I/O."""
    return os.environ.get("COMPUTERNAME", "").strip() or "localhost"


def generateRdpFileContent(host: str = "localhost", port: int = 3389) -> str:
    """Generate a safe in-memory ``.rdp`` connection file for ``host:port`` (REQ-003)."""
    safeHost = sanitize(host).strip() or "localhost"
    safePort = int(port) if 1 <= int(port) <= 65535 else 3389
    return (
        f"full address:s:{safeHost}:{safePort}\n"
        "prompt for credentials:i:1\n"
        "administrative session:i:0\n"
    )
