"""Domain types and pure state machines for REQ-002: reservations, handoff and status notes.

All types are frozen dataclasses with no I/O. State transition functions are pure and
deterministic: given a ``QueueState``, inputs, and ``now``, they return a ``TransitionOutcome``
containing the updated ``QueueState`` and a tuple of ``ReservationAuditEvent`` records to
append to ``activity_log.json`` (DEC-010, SD001).
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from pc_handoff.clock import toIso
from pc_handoff.domain.models import (
    SCHEMA_VERSION,
    EventReason,
    SessionState,
    TrackedSession,
    convertJson,
    isoOrNone,
    parseIsoOrNone,
    parseIsoStrict,
)

MIN_DURATION_MINUTES = 5
MAX_DURATION_MINUTES = 480
DEFAULT_DURATION_MINUTES = 60

MAX_USER_NAME_LENGTH = 64
MAX_NOTES_LENGTH = 280
MAX_MESSAGE_LENGTH = 280

MIN_NOTE_TTL_MINUTES = 5
MAX_NOTE_TTL_MINUTES = 1440
DEFAULT_NOTE_TTL_MINUTES = 120

CLAIM_GRACE_SECONDS = 300
MAX_RETAINED_TERMINAL = 20
MAX_STATUS_NOTES = 10


class ReservationStatus(StrEnum):
    """Lifecycle status of a single reservation (SD001 section 2.1)."""

    QUEUED = "QUEUED"
    READY = "READY"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    REQUEUED = "REQUEUED"

    @property
    def isTerminal(self) -> bool:
        return self in (
            ReservationStatus.COMPLETED,
            ReservationStatus.CANCELLED,
            ReservationStatus.EXPIRED,
        )


class HandoffStatus(StrEnum):
    """Lifecycle status of an ownership handoff offer (SD001 section 2.1)."""

    IDLE = "IDLE"
    OFFERED = "OFFERED"
    CLAIMED = "CLAIMED"
    TIMED_OUT = "TIMED_OUT"


class NoteCategory(StrEnum):
    """Category tag for a broadcast status note (SD002 section 5.1)."""

    GENERAL = "general"
    URGENT = "urgent"
    MAINTENANCE = "maintenance"
    LONG_RUNNING = "long_running"


class ReservationEventType(StrEnum):
    """Audit event types for reservation, handoff, and status note actions (SD001/SD002)."""

    RESERVATION_QUEUED = "reservation_queued"
    RESERVATION_UPDATED = "reservation_updated"
    RESERVATION_CANCELLED = "reservation_cancelled"
    HANDOFF_INITIATED = "handoff_initiated"
    HANDOFF_CLAIMED = "handoff_claimed"
    RESERVATION_EXPIRED = "reservation_expired"
    RESERVATION_REQUEUED = "reservation_requeued"
    STATUS_NOTE_POSTED = "status_note_posted"


# --- validation helpers -------------------------------------------------------------------


def normalizeUserKey(userName: str) -> str:
    """Case-insensitive canonical key for matching user names across queue and OS sessions."""
    return userName.strip().casefold()


def validateUserName(userName: str) -> str:
    if not isinstance(userName, str):
        raise ValueError("userName must be a string")
    cleaned = userName.strip()
    if not cleaned:
        raise ValueError("userName must not be empty")
    if len(cleaned) > MAX_USER_NAME_LENGTH:
        raise ValueError(f"userName exceeds {MAX_USER_NAME_LENGTH} characters")
    return cleaned


def validateDurationMinutes(minutes: int) -> int:
    if not isinstance(minutes, int) or isinstance(minutes, bool):
        raise ValueError("requestedDurationMinutes must be an integer")
    if minutes < MIN_DURATION_MINUTES or minutes > MAX_DURATION_MINUTES:
        raise ValueError(
            f"requestedDurationMinutes must be between {MIN_DURATION_MINUTES} "
            f"and {MAX_DURATION_MINUTES}"
        )
    return minutes


def validateNotes(notes: str | None) -> str | None:
    if notes is None:
        return None
    if not isinstance(notes, str):
        raise ValueError("notes must be a string or None")
    cleaned = notes.strip()
    if not cleaned:
        return None
    if len(cleaned) > MAX_NOTES_LENGTH:
        raise ValueError(f"notes exceeds {MAX_NOTES_LENGTH} characters")
    return cleaned


def validateMessage(message: str) -> str:
    if not isinstance(message, str):
        raise ValueError("message must be a string")
    cleaned = message.strip()
    if not cleaned:
        raise ValueError("message must not be empty")
    if len(cleaned) > MAX_MESSAGE_LENGTH:
        raise ValueError(f"message exceeds {MAX_MESSAGE_LENGTH} characters")
    return cleaned


def validateTtlMinutes(ttlMinutes: int) -> int:
    if not isinstance(ttlMinutes, int) or isinstance(ttlMinutes, bool):
        raise ValueError("ttlMinutes must be an integer")
    if ttlMinutes < MIN_NOTE_TTL_MINUTES or ttlMinutes > MAX_NOTE_TTL_MINUTES:
        raise ValueError(
            f"ttlMinutes must be between {MIN_NOTE_TTL_MINUTES} and {MAX_NOTE_TTL_MINUTES}"
        )
    return ttlMinutes


def validateCategory(category: NoteCategory | str | None) -> NoteCategory:
    if category is None:
        return NoteCategory.GENERAL
    if isinstance(category, NoteCategory):
        return category
    if not isinstance(category, str):
        raise ValueError("category must be a string or NoteCategory")
    return NoteCategory(category.strip().lower())


# --- domain models ------------------------------------------------------------------------


@dataclass(frozen=True)
class Reservation:
    """One reservation request in the queue or active slot (SD001 section 2.2)."""

    reservationId: str
    userName: str
    requestedDurationMinutes: int
    queuedAt: datetime
    status: ReservationStatus = ReservationStatus.QUEUED
    notes: str | None = None
    readyAt: datetime | None = None
    claimExpiresAt: datetime | None = None
    activatedAt: datetime | None = None
    endedAt: datetime | None = None
    clientAddress: str | None = None

    def toJson(self) -> dict[str, Any]:
        return {
            "reservationId": self.reservationId,
            "userName": self.userName,
            "requestedDurationMinutes": self.requestedDurationMinutes,
            "queuedAt": toIso(self.queuedAt),
            "status": self.status.value,
            "notes": self.notes,
            "readyAt": isoOrNone(self.readyAt),
            "claimExpiresAt": isoOrNone(self.claimExpiresAt),
            "activatedAt": isoOrNone(self.activatedAt),
            "endedAt": isoOrNone(self.endedAt),
            "clientAddress": self.clientAddress,
        }

    @classmethod
    def fromJson(cls, doc: dict[str, Any]) -> "Reservation":
        def build() -> Reservation:
            return cls(
                reservationId=doc["reservationId"],
                userName=validateUserName(doc["userName"]),
                requestedDurationMinutes=validateDurationMinutes(doc["requestedDurationMinutes"]),
                queuedAt=parseIsoStrict(doc["queuedAt"]),
                status=ReservationStatus(doc["status"]),
                notes=validateNotes(doc["notes"]),
                readyAt=parseIsoOrNone(doc["readyAt"]),
                claimExpiresAt=parseIsoOrNone(doc["claimExpiresAt"]),
                activatedAt=parseIsoOrNone(doc["activatedAt"]),
                endedAt=parseIsoOrNone(doc["endedAt"]),
                clientAddress=doc.get("clientAddress"),
            )

        return convertJson("reservation", build)


@dataclass(frozen=True)
class HandoffState:
    """Current ownership handoff state (SD001 section 2.3)."""

    status: HandoffStatus = HandoffStatus.IDLE
    reservationId: str | None = None
    offeredByUserName: str | None = None
    targetUserName: str | None = None
    offeredAt: datetime | None = None
    expiresAt: datetime | None = None

    @classmethod
    def idle(cls) -> "HandoffState":
        return cls()

    def toJson(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "reservationId": self.reservationId,
            "offeredByUserName": self.offeredByUserName,
            "targetUserName": self.targetUserName,
            "offeredAt": isoOrNone(self.offeredAt),
            "expiresAt": isoOrNone(self.expiresAt),
        }

    @classmethod
    def fromJson(cls, doc: dict[str, Any]) -> "HandoffState":
        def build() -> HandoffState:
            status = HandoffStatus(doc["status"])
            offeredBy = doc["offeredByUserName"]
            target = doc["targetUserName"]
            return cls(
                status=status,
                reservationId=doc["reservationId"],
                offeredByUserName=None if offeredBy is None else validateUserName(offeredBy),
                targetUserName=None if target is None else validateUserName(target),
                offeredAt=parseIsoOrNone(doc["offeredAt"]),
                expiresAt=parseIsoOrNone(doc["expiresAt"]),
            )

        return convertJson("handoff state", build)


HandoffSession = HandoffState


@dataclass(frozen=True)
class StatusNote:
    """A broadcast user-to-user status note with time-to-live (SD001 section 2.4, SD002 5.1)."""

    noteId: str
    userName: str
    message: str
    postedAt: datetime
    expiresAt: datetime
    category: NoteCategory = NoteCategory.GENERAL

    def toJson(self) -> dict[str, Any]:
        return {
            "noteId": self.noteId,
            "userName": self.userName,
            "message": self.message,
            "postedAt": toIso(self.postedAt),
            "expiresAt": toIso(self.expiresAt),
            "category": self.category.value,
        }

    @classmethod
    def fromJson(cls, doc: dict[str, Any]) -> "StatusNote":
        def build() -> StatusNote:
            return cls(
                noteId=doc["noteId"],
                userName=validateUserName(doc["userName"]),
                message=validateMessage(doc["message"]),
                postedAt=parseIsoStrict(doc["postedAt"]),
                expiresAt=parseIsoStrict(doc["expiresAt"]),
                category=validateCategory(doc.get("category")),
            )

        return convertJson("status note", build)


@dataclass(frozen=True)
class QueueState:
    """Aggregate state of the reservation queue, handoff, and status notes (`reservations.json`)."""

    updatedAt: datetime
    reservations: tuple[Reservation, ...] = ()
    handoff: HandoffState = HandoffState()
    statusNotes: tuple[StatusNote, ...] = ()
    schemaVersion: int = SCHEMA_VERSION

    @property
    def activeReservation(self) -> Reservation | None:
        for res in self.reservations:
            if res.status is ReservationStatus.ACTIVE:
                return res
        return None

    @property
    def readyReservation(self) -> Reservation | None:
        for res in self.reservations:
            if res.status is ReservationStatus.READY or (
                res.status is ReservationStatus.REQUEUED and res.claimExpiresAt is not None
            ):
                return res
        return None

    @property
    def queuedReservations(self) -> tuple[Reservation, ...]:
        return tuple(
            r
            for r in self.reservations
            if r.status is ReservationStatus.QUEUED
            or (r.status is ReservationStatus.REQUEUED and r.claimExpiresAt is None)
        )

    @property
    def nonTerminalReservations(self) -> tuple[Reservation, ...]:
        return tuple(r for r in self.reservations if not r.status.isTerminal)

    def findByUser(self, userName: str) -> Reservation | None:
        """Find a non-terminal reservation matching ``userName`` (case-insensitive)."""
        key = normalizeUserKey(userName)
        for res in self.reservations:
            if not res.status.isTerminal and normalizeUserKey(res.userName) == key:
                return res
        return None

    def findById(self, reservationId: str) -> Reservation | None:
        for res in self.reservations:
            if res.reservationId == reservationId:
                return res
        return None

    def toJson(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schemaVersion,
            "updatedAt": toIso(self.updatedAt),
            "handoff": self.handoff.toJson(),
            "reservations": [r.toJson() for r in self.reservations],
            "statusNotes": [n.toJson() for n in self.statusNotes],
        }

    @classmethod
    def fromJson(cls, doc: dict[str, Any]) -> "QueueState":
        def build() -> QueueState:
            return cls(
                schemaVersion=doc["schemaVersion"],
                updatedAt=parseIsoStrict(doc["updatedAt"]),
                handoff=HandoffState.fromJson(doc["handoff"]),
                reservations=tuple(Reservation.fromJson(r) for r in doc["reservations"]),
                statusNotes=tuple(StatusNote.fromJson(n) for n in doc["statusNotes"]),
            )

        return convertJson("reservations document", build)


ReservationsDocument = QueueState


# --- audit events -------------------------------------------------------------------------


@dataclass(frozen=True)
class ReservationAuditEvent:
    """One reservation, handoff, or status-note audit event in ``activity_log.json``."""

    eventId: str
    eventType: ReservationEventType
    timestamp: datetime
    userName: str
    reason: EventReason
    reservationId: str | None = None
    requestedDurationMinutes: int | None = None
    offeredByUserName: str | None = None
    notes: str | None = None
    noteId: str | None = None
    message: str | None = None
    expiresAt: datetime | None = None
    category: NoteCategory | None = None
    clientAddress: str | None = None

    def toJson(self) -> dict[str, Any]:
        if self.eventType is ReservationEventType.STATUS_NOTE_POSTED:
            if self.noteId is None or self.message is None or self.expiresAt is None:
                raise ValueError("status_note_posted event requires noteId, message, and expiresAt")
            return {
                "eventId": self.eventId,
                "eventType": self.eventType.value,
                "timestamp": toIso(self.timestamp),
                "noteId": self.noteId,
                "userName": self.userName,
                "message": self.message,
                "expiresAt": toIso(self.expiresAt),
                "category": (self.category or NoteCategory.GENERAL).value,
                "reason": self.reason.value,
            }
        if self.reservationId is None or self.requestedDurationMinutes is None:
            raise ValueError(
                f"{self.eventType.value} event requires reservationId and requestedDurationMinutes"
            )
        return {
            "eventId": self.eventId,
            "eventType": self.eventType.value,
            "timestamp": toIso(self.timestamp),
            "reservationId": self.reservationId,
            "userName": self.userName,
            "requestedDurationMinutes": self.requestedDurationMinutes,
            "reason": self.reason.value,
            "offeredByUserName": self.offeredByUserName,
            "notes": self.notes,
            "clientAddress": self.clientAddress,
        }

    @classmethod
    def fromJson(cls, doc: dict[str, Any]) -> "ReservationAuditEvent":
        def build() -> ReservationAuditEvent:
            eventType = ReservationEventType(doc["eventType"])
            if eventType is ReservationEventType.STATUS_NOTE_POSTED:
                return cls(
                    eventId=doc["eventId"],
                    eventType=eventType,
                    timestamp=parseIsoStrict(doc["timestamp"]),
                    userName=validateUserName(doc["userName"]),
                    reason=EventReason(doc["reason"]),
                    noteId=doc["noteId"],
                    message=validateMessage(doc["message"]),
                    expiresAt=parseIsoStrict(doc["expiresAt"]),
                    category=validateCategory(doc.get("category")),
                )
            offeredBy = doc["offeredByUserName"]
            return cls(
                eventId=doc["eventId"],
                eventType=eventType,
                timestamp=parseIsoStrict(doc["timestamp"]),
                userName=validateUserName(doc["userName"]),
                reason=EventReason(doc["reason"]),
                reservationId=doc["reservationId"],
                requestedDurationMinutes=validateDurationMinutes(doc["requestedDurationMinutes"]),
                offeredByUserName=None if offeredBy is None else validateUserName(offeredBy),
                notes=validateNotes(doc["notes"]),
                clientAddress=doc.get("clientAddress"),
            )

        return convertJson("reservation audit event", build)


def newReservationEvent(
    reservation: Reservation,
    eventType: ReservationEventType,
    now: datetime,
    reason: EventReason = EventReason.OBSERVED,
    offeredByUserName: str | None = None,
) -> ReservationAuditEvent:
    return ReservationAuditEvent(
        eventId=str(uuid.uuid4()),
        eventType=eventType,
        timestamp=now,
        userName=reservation.userName,
        reason=reason,
        reservationId=reservation.reservationId,
        requestedDurationMinutes=reservation.requestedDurationMinutes,
        offeredByUserName=offeredByUserName,
        notes=reservation.notes,
        clientAddress=reservation.clientAddress,
    )


def newStatusNoteEvent(
    note: StatusNote,
    now: datetime,
    reason: EventReason = EventReason.OBSERVED,
) -> ReservationAuditEvent:
    return ReservationAuditEvent(
        eventId=str(uuid.uuid4()),
        eventType=ReservationEventType.STATUS_NOTE_POSTED,
        timestamp=now,
        userName=note.userName,
        reason=reason,
        noteId=note.noteId,
        message=note.message,
        expiresAt=note.expiresAt,
        category=note.category,
    )


# --- pure state transitions ---------------------------------------------------------------


@dataclass(frozen=True)
class TransitionOutcome:
    """Result of a pure queue transition: new ``QueueState`` and emitted audit events."""

    state: QueueState
    events: tuple[ReservationAuditEvent, ...] = ()


def emptyQueueState(now: datetime) -> QueueState:
    return QueueState(updatedAt=now)


def _pruneAndBound(
    reservations: Iterable[Reservation],
    statusNotes: Iterable[StatusNote],
    now: datetime,
) -> tuple[tuple[Reservation, ...], tuple[StatusNote, ...]]:
    """Keep all non-terminal reservations (in order) plus the last ``MAX_RETAINED_TERMINAL``
    terminal ones, and keep non-expired status notes bounded to ``MAX_STATUS_NOTES``."""
    nonTerminal: list[Reservation] = []
    terminal: list[Reservation] = []
    for res in reservations:
        if res.status.isTerminal:
            terminal.append(res)
        else:
            nonTerminal.append(res)
    keptReservations = tuple(nonTerminal + terminal[-MAX_RETAINED_TERMINAL:])
    keptNotes = tuple(n for n in statusNotes if n.expiresAt > now)[:MAX_STATUS_NOTES]
    return keptReservations, keptNotes


def _promoteHeadQueued(
    reservations: list[Reservation],
    now: datetime,
    *,
    offeredByUserName: str | None,
    claimGraceSeconds: int,
    reason: EventReason,
    excludeReservationId: str | None = None,
) -> tuple[list[Reservation], HandoffState, list[ReservationAuditEvent]]:
    """Promote the first waiting (QUEUED or REQUEUED) reservation to READY."""
    for index, res in enumerate(reservations):
        if (
            res.status in (ReservationStatus.QUEUED, ReservationStatus.REQUEUED)
            and res.claimExpiresAt is None
            and (excludeReservationId is None or res.reservationId != excludeReservationId)
        ):
            expiresAt = now + timedelta(seconds=claimGraceSeconds)
            promoted = replace(
                res,
                status=ReservationStatus.READY,
                readyAt=now,
                claimExpiresAt=expiresAt,
            )
            updated = list(reservations)
            updated[index] = promoted
            handoff = HandoffState(
                status=HandoffStatus.OFFERED,
                reservationId=promoted.reservationId,
                offeredByUserName=offeredByUserName,
                targetUserName=promoted.userName,
                offeredAt=now,
                expiresAt=expiresAt,
            )
            event = newReservationEvent(
                promoted,
                ReservationEventType.HANDOFF_INITIATED,
                now,
                reason=reason,
                offeredByUserName=offeredByUserName,
            )
            return updated, handoff, [event]

    return reservations, HandoffState.idle(), []


def enqueueReservation(
    state: QueueState,
    userName: str,
    requestedDurationMinutes: int,
    now: datetime,
    *,
    notes: str | None = None,
    clientAddress: str | None = None,
    pcAvailable: bool = False,
    claimGraceSeconds: int = CLAIM_GRACE_SECONDS,
) -> TransitionOutcome:
    """Add a new reservation for ``userName`` to the queue.

    If ``pcAvailable`` is True and no reservation is currently ``ACTIVE`` or ``READY``, the
    new reservation immediately advances to ``READY`` with an ``OFFERED`` handoff window.
    """
    cleanUser = validateUserName(userName)
    cleanDuration = validateDurationMinutes(requestedDurationMinutes)
    cleanNotes = validateNotes(notes)
    cleanAddress = clientAddress.strip() if clientAddress and clientAddress.strip() else None

    if state.findByUser(cleanUser) is not None:
        raise ValueError(f"user {cleanUser!r} already has an active or queued reservation")

    created = Reservation(
        reservationId=str(uuid.uuid4()),
        userName=cleanUser,
        requestedDurationMinutes=cleanDuration,
        queuedAt=now,
        status=ReservationStatus.QUEUED,
        notes=cleanNotes,
        clientAddress=cleanAddress,
    )
    events: list[ReservationAuditEvent] = [
        newReservationEvent(created, ReservationEventType.RESERVATION_QUEUED, now)
    ]
    working = [*state.reservations, created]
    handoff = state.handoff

    if pcAvailable and state.activeReservation is None and state.readyReservation is None:
        working, handoff, promoEvents = _promoteHeadQueued(
            working,
            now,
            offeredByUserName=None,
            claimGraceSeconds=claimGraceSeconds,
            reason=EventReason.OBSERVED,
        )
        events.extend(promoEvents)

    keptReservations, keptNotes = _pruneAndBound(working, state.statusNotes, now)
    return TransitionOutcome(
        state=replace(
            state,
            updatedAt=now,
            reservations=keptReservations,
            handoff=handoff,
            statusNotes=keptNotes,
        ),
        events=tuple(events),
    )


@dataclass(frozen=True)
class WaitEstimate:
    """Estimated wait duration and start time for joining or waiting in the queue (SD002 3.2)."""

    waitMinutes: int
    estimatedStartAt: datetime


def _remainingActiveMinutes(active: Reservation, now: datetime) -> int:
    if active.activatedAt is None:
        return active.requestedDurationMinutes
    remainingSeconds = (
        active.activatedAt + timedelta(minutes=active.requestedDurationMinutes) - now
    ).total_seconds()
    if remainingSeconds <= 0:
        return 0
    return int((remainingSeconds + 59) // 60)


def estimateWaitMinutes(
    state: QueueState,
    now: datetime,
    *,
    reservationId: str | None = None,
) -> int:
    """Calculate estimated wait time in minutes before a new or existing reservation starts.

    When ``reservationId`` is ``None``, returns the wait time for a new user joining at the tail
    of the queue (active remaining + ready duration + all queued durations). When
    ``reservationId`` identifies a ``QUEUED`` reservation, sums only slots ahead of it; for an
    ``ACTIVE`` or ``READY`` reservation, returns ``0``.
    """
    if reservationId is not None:
        target = state.findById(reservationId)
        if target is None or target.status.isTerminal:
            raise ValueError(f"active or queued reservation not found: {reservationId!r}")
        if target.status in (ReservationStatus.ACTIVE, ReservationStatus.READY) or (
            target.status is ReservationStatus.REQUEUED and target.claimExpiresAt is not None
        ):
            return 0

    total = 0
    if state.activeReservation is not None:
        total += _remainingActiveMinutes(state.activeReservation, now)
    if state.readyReservation is not None:
        total += state.readyReservation.requestedDurationMinutes

    for queued in state.queuedReservations:
        if reservationId is not None and queued.reservationId == reservationId:
            break
        total += queued.requestedDurationMinutes
    return total


def estimateQueueWait(
    state: QueueState,
    now: datetime,
    *,
    reservationId: str | None = None,
) -> WaitEstimate:
    """Return a ``WaitEstimate`` with ``waitMinutes`` and ``estimatedStartAt``."""
    minutes = estimateWaitMinutes(state, now, reservationId=reservationId)
    return WaitEstimate(
        waitMinutes=minutes,
        estimatedStartAt=now + timedelta(minutes=minutes),
    )


def updateReservation(
    state: QueueState,
    reservationId: str,
    requestedDurationMinutes: int,
    now: datetime,
    *,
    notes: str | None = None,
) -> TransitionOutcome:
    """Update ``requestedDurationMinutes`` and ``notes`` on a waiting reservation without
    forfeiting its position in the queue (SD002 section 5.2)."""
    target = state.findById(reservationId)
    if target is None or target.status not in (
        ReservationStatus.QUEUED,
        ReservationStatus.REQUEUED,
    ):
        raise ValueError(f"queued reservation not found: {reservationId!r}")

    cleanDuration = validateDurationMinutes(requestedDurationMinutes)
    cleanNotes = validateNotes(notes)

    updated = replace(
        target,
        requestedDurationMinutes=cleanDuration,
        notes=cleanNotes,
    )
    working = [updated if r.reservationId == reservationId else r for r in state.reservations]
    event = newReservationEvent(updated, ReservationEventType.RESERVATION_UPDATED, now)
    keptReservations, keptNotes = _pruneAndBound(working, state.statusNotes, now)
    return TransitionOutcome(
        state=replace(
            state,
            updatedAt=now,
            reservations=keptReservations,
            statusNotes=keptNotes,
        ),
        events=(event,),
    )


def cancelReservation(
    state: QueueState,
    reservationId: str,
    now: datetime,
    *,
    pcAvailable: bool = False,
    claimGraceSeconds: int = CLAIM_GRACE_SECONDS,
) -> TransitionOutcome:
    """Cancel a non-terminal reservation by ``reservationId`` and advance the queue if needed."""
    target = state.findById(reservationId)
    if target is None or target.status.isTerminal:
        raise ValueError(f"active or queued reservation not found: {reservationId!r}")

    cancelled = replace(
        target,
        status=ReservationStatus.CANCELLED,
        claimExpiresAt=None,
        endedAt=now,
    )
    working = [cancelled if r.reservationId == reservationId else r for r in state.reservations]
    events: list[ReservationAuditEvent] = [
        newReservationEvent(cancelled, ReservationEventType.RESERVATION_CANCELLED, now)
    ]

    wasReadyOrActive = target.status in (
        ReservationStatus.READY,
        ReservationStatus.ACTIVE,
    ) or (target.status is ReservationStatus.REQUEUED and target.claimExpiresAt is not None)
    handoff = state.handoff
    if state.handoff.reservationId == reservationId:
        handoff = HandoffState.idle()

    if (wasReadyOrActive or pcAvailable) and not any(
        r.status in (ReservationStatus.ACTIVE, ReservationStatus.READY)
        or (r.status is ReservationStatus.REQUEUED and r.claimExpiresAt is not None)
        for r in working
    ):
        working, handoff, promoEvents = _promoteHeadQueued(
            working,
            now,
            offeredByUserName=None,
            claimGraceSeconds=claimGraceSeconds,
            reason=EventReason.OBSERVED,
        )
        events.extend(promoEvents)

    keptReservations, keptNotes = _pruneAndBound(working, state.statusNotes, now)
    return TransitionOutcome(
        state=replace(
            state,
            updatedAt=now,
            reservations=keptReservations,
            handoff=handoff,
            statusNotes=keptNotes,
        ),
        events=tuple(events),
    )


def initiateHandoff(
    state: QueueState,
    offeredByUserName: str | None,
    now: datetime,
    *,
    claimGraceSeconds: int = CLAIM_GRACE_SECONDS,
) -> TransitionOutcome:
    """Complete any current ``ACTIVE`` reservation and offer handoff to the next queued user."""
    cleanOfferer = None if offeredByUserName is None else validateUserName(offeredByUserName)
    working: list[Reservation] = []
    for res in state.reservations:
        if res.status is ReservationStatus.ACTIVE:
            working.append(
                replace(
                    res,
                    status=ReservationStatus.COMPLETED,
                    claimExpiresAt=None,
                    endedAt=now,
                )
            )
        else:
            working.append(res)

    events: list[ReservationAuditEvent] = []
    ready = next(
        (
            r
            for r in working
            if r.status is ReservationStatus.READY
            or (r.status is ReservationStatus.REQUEUED and r.claimExpiresAt is not None)
        ),
        None,
    )
    if ready is not None:
        expiresAt = now + timedelta(seconds=claimGraceSeconds)
        refreshed = replace(ready, readyAt=now, claimExpiresAt=expiresAt)
        working = [refreshed if r.reservationId == ready.reservationId else r for r in working]
        handoff = HandoffState(
            status=HandoffStatus.OFFERED,
            reservationId=refreshed.reservationId,
            offeredByUserName=cleanOfferer,
            targetUserName=refreshed.userName,
            offeredAt=now,
            expiresAt=expiresAt,
        )
        events.append(
            newReservationEvent(
                refreshed,
                ReservationEventType.HANDOFF_INITIATED,
                now,
                offeredByUserName=cleanOfferer,
            )
        )
    else:
        working, handoff, promoEvents = _promoteHeadQueued(
            working,
            now,
            offeredByUserName=cleanOfferer,
            claimGraceSeconds=claimGraceSeconds,
            reason=EventReason.OBSERVED,
        )
        events.extend(promoEvents)

    keptReservations, keptNotes = _pruneAndBound(working, state.statusNotes, now)
    return TransitionOutcome(
        state=replace(
            state,
            updatedAt=now,
            reservations=keptReservations,
            handoff=handoff,
            statusNotes=keptNotes,
        ),
        events=tuple(events),
    )


def claimHandoff(
    state: QueueState,
    reservationId: str,
    now: datetime,
    *,
    reason: EventReason = EventReason.OBSERVED,
) -> TransitionOutcome:
    """Claim a ``READY`` reservation (or head waiting when none is active/ready) into
    ``ACTIVE``."""
    target = state.findById(reservationId)
    if target is None or target.status not in (
        ReservationStatus.READY,
        ReservationStatus.QUEUED,
        ReservationStatus.REQUEUED,
    ):
        raise ValueError(f"reservation {reservationId!r} cannot be claimed")
    if (
        target.status in (ReservationStatus.QUEUED, ReservationStatus.REQUEUED)
        and target.claimExpiresAt is None
        and (
            state.activeReservation is not None
            or state.readyReservation is not None
            or (
                state.queuedReservations
                and state.queuedReservations[0].reservationId != reservationId
            )
        )
    ):
        raise ValueError(f"reservation {reservationId!r} is not at the head of the queue")

    claimed = replace(
        target,
        status=ReservationStatus.ACTIVE,
        readyAt=target.readyAt or now,
        claimExpiresAt=None,
        activatedAt=now,
    )
    working = [claimed if r.reservationId == reservationId else r for r in state.reservations]
    offeredBy = (
        state.handoff.offeredByUserName if state.handoff.reservationId == reservationId else None
    )
    offeredAt = state.handoff.offeredAt or now
    expiresAt = state.handoff.expiresAt or now
    handoff = HandoffState(
        status=HandoffStatus.CLAIMED,
        reservationId=claimed.reservationId,
        offeredByUserName=offeredBy,
        targetUserName=claimed.userName,
        offeredAt=offeredAt,
        expiresAt=expiresAt,
    )
    event = newReservationEvent(
        claimed,
        ReservationEventType.HANDOFF_CLAIMED,
        now,
        reason=reason,
        offeredByUserName=offeredBy,
    )
    keptReservations, keptNotes = _pruneAndBound(working, state.statusNotes, now)
    return TransitionOutcome(
        state=replace(
            state,
            updatedAt=now,
            reservations=keptReservations,
            handoff=handoff,
            statusNotes=keptNotes,
        ),
        events=(event,),
    )


def postStatusNote(
    state: QueueState,
    userName: str,
    message: str,
    now: datetime,
    *,
    ttlMinutes: int = DEFAULT_NOTE_TTL_MINUTES,
    category: NoteCategory | str = NoteCategory.GENERAL,
) -> TransitionOutcome:
    """Post a new broadcast ``StatusNote`` and prune expired notes."""
    cleanUser = validateUserName(userName)
    cleanMessage = validateMessage(message)
    cleanTtl = validateTtlMinutes(ttlMinutes)
    cleanCategory = validateCategory(category)

    note = StatusNote(
        noteId=str(uuid.uuid4()),
        userName=cleanUser,
        message=cleanMessage,
        postedAt=now,
        expiresAt=now + timedelta(minutes=cleanTtl),
        category=cleanCategory,
    )
    event = newStatusNoteEvent(note, now)
    keptReservations, keptNotes = _pruneAndBound(
        state.reservations,
        [note, *state.statusNotes],
        now,
    )
    return TransitionOutcome(
        state=replace(
            state,
            updatedAt=now,
            reservations=keptReservations,
            statusNotes=keptNotes,
        ),
        events=(event,),
    )


def clearStatusNote(
    state: QueueState,
    noteId: str,
    now: datetime,
) -> TransitionOutcome:
    """Remove an active status note by ``noteId``."""
    if not any(n.noteId == noteId for n in state.statusNotes):
        raise ValueError(f"status note not found: {noteId!r}")
    remaining = [n for n in state.statusNotes if n.noteId != noteId]
    keptReservations, keptNotes = _pruneAndBound(state.reservations, remaining, now)
    return TransitionOutcome(
        state=replace(
            state,
            updatedAt=now,
            reservations=keptReservations,
            statusNotes=keptNotes,
        ),
        events=(),
    )


def evaluateTick(
    state: QueueState,
    activeSessions: Iterable[TrackedSession],
    pcAvailable: bool,
    now: datetime,
    *,
    reason: EventReason = EventReason.OBSERVED,
    claimGraceSeconds: int = CLAIM_GRACE_SECONDS,
) -> TransitionOutcome:
    """Evaluate session correlation, claim grace timeouts, queue advance, and note TTLs at
    ``now``."""
    activeUserKeys = {
        normalizeUserKey(s.userName) for s in activeSessions if s.state is SessionState.ACTIVE
    }
    activeClientAddresses = {
        s.clientAddress.strip()
        for s in activeSessions
        if s.state is SessionState.ACTIVE and s.clientAddress and s.clientAddress.strip()
    }
    working = list(state.reservations)
    handoff = state.handoff
    events: list[ReservationAuditEvent] = []
    changed = False

    # 1. Complete ACTIVE reservation if its user is no longer connected and the PC is Available.
    for idx, res in enumerate(working):
        if res.status is ReservationStatus.ACTIVE and pcAvailable:
            userActive = normalizeUserKey(res.userName) in activeUserKeys
            ipActive = bool(
                res.clientAddress and res.clientAddress.strip() in activeClientAddresses
            )
            if not userActive and not ipActive:
                working[idx] = replace(
                    res,
                    status=ReservationStatus.COMPLETED,
                    claimExpiresAt=None,
                    endedAt=now,
                )
                if handoff.reservationId == res.reservationId:
                    handoff = HandoffState.idle()
                changed = True

    # 2. Correlate READY / sole REQUEUED reservation with an Active OS session.
    for idx, res in enumerate(working):
        isReadyOrSoleRequeued = res.status is ReservationStatus.READY or (
            res.status is ReservationStatus.REQUEUED and res.claimExpiresAt is not None
        )
        if isReadyOrSoleRequeued:
            userMatches = normalizeUserKey(res.userName) in activeUserKeys
            ipMatches = bool(
                res.clientAddress and res.clientAddress.strip() in activeClientAddresses
            )
            if userMatches or ipMatches:
                claimed = replace(
                    res,
                    status=ReservationStatus.ACTIVE,
                    claimExpiresAt=None,
                    activatedAt=now,
                )
                working[idx] = claimed
                offeredBy = (
                    handoff.offeredByUserName
                    if handoff.reservationId == res.reservationId
                    else None
                )
                handoff = HandoffState(
                    status=HandoffStatus.CLAIMED,
                    reservationId=claimed.reservationId,
                    offeredByUserName=offeredBy,
                    targetUserName=claimed.userName,
                    offeredAt=handoff.offeredAt or now,
                    expiresAt=handoff.expiresAt or now,
                )
                events.append(
                    newReservationEvent(
                        claimed,
                        ReservationEventType.HANDOFF_CLAIMED,
                        now,
                        reason=reason,
                        offeredByUserName=offeredBy,
                    )
                )
                changed = True
                break

    # 3. Direct correlation of head waiting reservation when no ACTIVE/READY exists.
    hasActiveOrReady = any(
        r.status in (ReservationStatus.ACTIVE, ReservationStatus.READY)
        or (r.status is ReservationStatus.REQUEUED and r.claimExpiresAt is not None)
        for r in working
    )
    if not hasActiveOrReady:
        for idx, res in enumerate(working):
            if res.status in (ReservationStatus.QUEUED, ReservationStatus.REQUEUED):
                userMatches = normalizeUserKey(res.userName) in activeUserKeys
                ipMatches = bool(
                    res.clientAddress and res.clientAddress.strip() in activeClientAddresses
                )
                if userMatches or ipMatches:
                    claimed = replace(
                        res,
                        status=ReservationStatus.ACTIVE,
                        readyAt=now,
                        claimExpiresAt=None,
                        activatedAt=now,
                    )
                    working[idx] = claimed
                    handoff = HandoffState(
                        status=HandoffStatus.CLAIMED,
                        reservationId=claimed.reservationId,
                        offeredByUserName=None,
                        targetUserName=claimed.userName,
                        offeredAt=now,
                        expiresAt=now,
                    )
                    events.append(
                        newReservationEvent(
                            claimed,
                            ReservationEventType.HANDOFF_CLAIMED,
                            now,
                            reason=reason,
                        )
                    )
                    changed = True
                break

    # 4. Re-queue READY (or sole active REQUEUED) reservation if claim grace window has elapsed.
    for idx, res in enumerate(working):
        isReadyToClaim = res.status is ReservationStatus.READY or (
            res.status is ReservationStatus.REQUEUED and res.claimExpiresAt is not None
        )
        if isReadyToClaim and res.claimExpiresAt is not None and now >= res.claimExpiresAt:
            otherWaiting = [
                r
                for r in working
                if r.reservationId != res.reservationId
                and r.status in (ReservationStatus.QUEUED, ReservationStatus.REQUEUED)
            ]
            if not otherWaiting:
                requeued = replace(
                    res,
                    status=ReservationStatus.REQUEUED,
                    claimExpiresAt=None,
                )
                working[idx] = requeued
                offeredBy = (
                    handoff.offeredByUserName
                    if handoff.reservationId == res.reservationId
                    else None
                )
                handoff = HandoffState(
                    status=HandoffStatus.TIMED_OUT,
                    reservationId=requeued.reservationId,
                    offeredByUserName=offeredBy,
                    targetUserName=requeued.userName,
                    offeredAt=handoff.offeredAt or res.readyAt or now,
                    expiresAt=res.claimExpiresAt,
                )
                events.append(
                    newReservationEvent(
                        requeued,
                        ReservationEventType.RESERVATION_REQUEUED,
                        now,
                        reason=reason,
                        offeredByUserName=offeredBy,
                    )
                )
                changed = True
            else:
                requeued = replace(
                    res,
                    status=ReservationStatus.REQUEUED,
                    claimExpiresAt=None,
                )
                nonTerminals = [
                    r
                    for r in working
                    if not r.status.isTerminal and r.reservationId != res.reservationId
                ]
                terminals = [r for r in working if r.status.isTerminal]
                working = nonTerminals + [requeued] + terminals
                offeredBy = (
                    handoff.offeredByUserName
                    if handoff.reservationId == res.reservationId
                    else None
                )
                handoff = HandoffState(
                    status=HandoffStatus.TIMED_OUT,
                    reservationId=requeued.reservationId,
                    offeredByUserName=offeredBy,
                    targetUserName=requeued.userName,
                    offeredAt=handoff.offeredAt or res.readyAt or now,
                    expiresAt=res.claimExpiresAt,
                )
                events.append(
                    newReservationEvent(
                        requeued,
                        ReservationEventType.RESERVATION_REQUEUED,
                        now,
                        reason=reason,
                        offeredByUserName=offeredBy,
                    )
                )
                changed = True
            break

    # 5. Advance queue head to READY if no ACTIVE/READY exists and PC is Available
    # (or timed out with next waiting).
    hasActiveOrReady = any(
        r.status in (ReservationStatus.ACTIVE, ReservationStatus.READY)
        or (r.status is ReservationStatus.REQUEUED and r.claimExpiresAt is not None)
        for r in working
    )
    if not hasActiveOrReady and (pcAvailable or handoff.status is HandoffStatus.TIMED_OUT):
        hasEligibleQueued = any(
            r.status in (ReservationStatus.QUEUED, ReservationStatus.REQUEUED)
            and r.claimExpiresAt is None
            and (
                handoff.status is not HandoffStatus.TIMED_OUT
                or r.reservationId != handoff.reservationId
            )
            for r in working
        )
        if hasEligibleQueued:
            working, handoff, promoEvents = _promoteHeadQueued(
                working,
                now,
                offeredByUserName=None,
                claimGraceSeconds=claimGraceSeconds,
                reason=reason,
                excludeReservationId=handoff.reservationId
                if handoff.status is HandoffStatus.TIMED_OUT
                else None,
            )
            events.extend(promoEvents)
            changed = True

    keptReservations, keptNotes = _pruneAndBound(working, state.statusNotes, now)
    if keptNotes != state.statusNotes or keptReservations != state.reservations:
        changed = True

    if not changed and not events:
        return TransitionOutcome(state=state, events=())

    return TransitionOutcome(
        state=replace(
            state,
            updatedAt=now,
            reservations=keptReservations,
            handoff=handoff,
            statusNotes=keptNotes,
        ),
        events=tuple(events),
    )
