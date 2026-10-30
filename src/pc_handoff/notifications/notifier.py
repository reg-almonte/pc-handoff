"""Protocol, payload model, and event formatter for host PC notifications (SD002 section 4)."""

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pc_handoff.domain.reservations import (
    CLAIM_GRACE_SECONDS,
    QueueState,
    ReservationAuditEvent,
    ReservationEventType,
)


def sanitizeNotificationText(text: str) -> str:
    """Strip Unicode control and format characters (Cc, Cf) and collapse whitespace."""
    cleaned = "".join(" " if unicodedata.category(ch) in ("Cc", "Cf") else ch for ch in text)
    return " ".join(cleaned.split())


@dataclass(frozen=True)
class HostNotification:
    """One host notification message ready for OS delivery."""

    title: str
    body: str
    eventType: ReservationEventType | None = None
    reservationId: str | None = None


@runtime_checkable
class Notifier(Protocol):
    """Abstract interface for dispatching host PC notifications."""

    def notify(self, notification: HostNotification) -> None:
        """Dispatch a single ``HostNotification`` without raising on OS errors."""

    def notifyEvents(
        self,
        events: Sequence[ReservationAuditEvent],
        state: QueueState | None = None,
    ) -> None:
        """Format and dispatch notifications for any eligible reservation audit events."""

    def close(self) -> None:
        """Shut down any background resources owned by the notifier."""


class NullNotifier:
    """No-op notifier used when host notifications are disabled."""

    def notify(self, notification: HostNotification) -> None:
        return None

    def notifyEvents(
        self,
        events: Sequence[ReservationAuditEvent],
        state: QueueState | None = None,
    ) -> None:
        return None

    def close(self) -> None:
        return None


def formatEventNotification(
    event: ReservationAuditEvent,
    state: QueueState | None = None,
    *,
    claimGraceSeconds: int = CLAIM_GRACE_SECONDS,
) -> HostNotification | None:
    """Convert an audit event into a ``HostNotification`` for the three trigger milestones
    defined in SD002 section 4.1, or ``None`` for non-notifying events."""
    user = sanitizeNotificationText(event.userName)

    if event.eventType is ReservationEventType.RESERVATION_QUEUED:
        duration = event.requestedDurationMinutes or 0
        body = f"{user} joined the reservation queue ({duration} min)."
        if event.notes:
            noteText = sanitizeNotificationText(event.notes)
            body = f"{body} Note: {noteText}"
        return HostNotification(
            title="PC Handoff: Queue Joined",
            body=body,
            eventType=event.eventType,
            reservationId=event.reservationId,
        )

    if event.eventType is ReservationEventType.HANDOFF_INITIATED:
        graceMinutes = max(1, claimGraceSeconds // 60)
        if event.offeredByUserName:
            offerer = sanitizeNotificationText(event.offeredByUserName)
            body = (
                f"{offerer} offered handoff to {user} — {graceMinutes} min grace period to claim."
            )
        else:
            body = f"PC is ready for {user} — {graceMinutes} min grace period to claim."
        return HostNotification(
            title="PC Handoff: Ready to Claim",
            body=body,
            eventType=event.eventType,
            reservationId=event.reservationId,
        )

    if event.eventType in (
        ReservationEventType.RESERVATION_EXPIRED,
        ReservationEventType.RESERVATION_REQUEUED,
    ):
        nextReady = state.readyReservation if state is not None else None
        if nextReady is not None and nextReady.reservationId != event.reservationId:
            nextUser = sanitizeNotificationText(nextReady.userName)
            body = f"{user}'s claim window expired; handoff moved to {nextUser}."
        else:
            body = f"{user}'s reservation claim window expired."
        return HostNotification(
            title="PC Handoff: Claim Expired",
            body=body,
            eventType=event.eventType,
            reservationId=event.reservationId,
        )

    return None
