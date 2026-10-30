"""Unit tests for the host PC notification subsystem (TASK-019)."""

import logging
import subprocess
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from pc_handoff.domain.reservations import (
    NoteCategory,
    Reservation,
    ReservationEventType,
    ReservationStatus,
    StatusNote,
    emptyQueueState,
    enqueueReservation,
    newReservationEvent,
    newStatusNoteEvent,
)
from pc_handoff.notifications import (
    CompositeNotifier,
    HostNotification,
    Notifier,
    NullNotifier,
    WindowsNotifier,
    buildMsgCommand,
    buildToastCommand,
    createNotifier,
    formatEventNotification,
)
from pc_handoff.notifications.teams_notifier import TeamsNotifier, buildTeamsAdaptiveCard
from pc_handoff.persistence.config_store import Config

TZ = timezone(timedelta(hours=9))
T0 = datetime(2026, 10, 26, 14, 0, 0, tzinfo=TZ)
RES_ID = "7f9c2ba0-4d3b-4c6e-8112-90a2b3c4d5e6"


def makeRes(
    userName: str = "Bob",
    status: ReservationStatus = ReservationStatus.QUEUED,
    notes: str | None = None,
) -> Reservation:
    return Reservation(
        reservationId=RES_ID,
        userName=userName,
        requestedDurationMinutes=45,
        queuedAt=T0,
        status=status,
        notes=notes,
        readyAt=T0 if status is ReservationStatus.READY else None,
        claimExpiresAt=T0 + timedelta(minutes=5) if status is ReservationStatus.READY else None,
    )


# --- formatEventNotification --------------------------------------------------------------


def test_format_event_notification_covers_all_trigger_and_non_trigger_events() -> None:
    # 1. RESERVATION_QUEUED with and without notes
    resNoNotes = makeRes("Bob\x00", notes=None)
    evQueued = newReservationEvent(resNoNotes, ReservationEventType.RESERVATION_QUEUED, T0)
    notifQueued = formatEventNotification(evQueued)
    assert notifQueued == HostNotification(
        title="PC Handoff: Queue Joined",
        body="Bob joined the reservation queue (45 min).",
        eventType=ReservationEventType.RESERVATION_QUEUED,
        reservationId=RES_ID,
    )

    resWithNotes = makeRes("Bob", notes="Need GPU\nfor benchmark")
    evQueuedNotes = newReservationEvent(resWithNotes, ReservationEventType.RESERVATION_QUEUED, T0)
    notifQueuedNotes = formatEventNotification(evQueuedNotes)
    assert notifQueuedNotes is not None
    assert notifQueuedNotes.body == (
        "Bob joined the reservation queue (45 min). Note: Need GPU for benchmark"
    )

    # 2. HANDOFF_INITIATED with and without offeredByUserName
    resReady = makeRes("Bob", status=ReservationStatus.READY)
    evHandoffAuto = newReservationEvent(resReady, ReservationEventType.HANDOFF_INITIATED, T0)
    notifHandoffAuto = formatEventNotification(evHandoffAuto, claimGraceSeconds=300)
    assert notifHandoffAuto is not None
    assert notifHandoffAuto.title == "PC Handoff: Ready to Claim"
    assert notifHandoffAuto.body == "PC is ready for Bob — 5 min grace period to claim."

    evHandoffOffered = newReservationEvent(
        resReady,
        ReservationEventType.HANDOFF_INITIATED,
        T0,
        offeredByUserName="Alice",
    )
    notifHandoffOffered = formatEventNotification(evHandoffOffered, claimGraceSeconds=300)
    assert notifHandoffOffered is not None
    assert notifHandoffOffered.body == (
        "Alice offered handoff to Bob — 5 min grace period to claim."
    )

    # 3. RESERVATION_EXPIRED with and without a next READY user
    evExpired = newReservationEvent(resReady, ReservationEventType.RESERVATION_EXPIRED, T0)
    notifExpiredAlone = formatEventNotification(evExpired, state=emptyQueueState(T0))
    assert notifExpiredAlone is not None
    assert notifExpiredAlone.title == "PC Handoff: Claim Expired"
    assert notifExpiredAlone.body == "Bob's reservation claim window expired."

    stateWithNext = enqueueReservation(
        emptyQueueState(T0),
        "Carol",
        30,
        T0,
        pcAvailable=True,
    ).state
    notifExpiredMoved = formatEventNotification(evExpired, state=stateWithNext)
    assert notifExpiredMoved is not None
    assert notifExpiredMoved.body == "Bob's claim window expired; handoff moved to Carol."

    # 4. Non-trigger events return None
    for eventType in (
        ReservationEventType.RESERVATION_UPDATED,
        ReservationEventType.RESERVATION_CANCELLED,
        ReservationEventType.HANDOFF_CLAIMED,
    ):
        ev = newReservationEvent(resNoNotes, eventType, T0)
        assert formatEventNotification(ev) is None

    note = StatusNote(
        noteId=RES_ID,
        userName="Alice",
        message="Hi",
        postedAt=T0,
        expiresAt=T0 + timedelta(minutes=30),
        category=NoteCategory.GENERAL,
    )
    assert formatEventNotification(newStatusNoteEvent(note, T0)) is None


# --- command builders & escaping ----------------------------------------------------------


def test_build_toast_and_msg_commands_escape_special_characters() -> None:
    toastCmd = buildToastCommand("PC's <Title>", "Bob & 'Alice' > 5m")
    assert toastCmd[0] == "powershell.exe"
    script = toastCmd[-1]
    assert "PC''s &lt;Title&gt;" in script
    assert "Bob &amp; ''Alice'' &gt; 5m" in script
    assert "HKCU:\\Software\\Classes\\AppUserModelId\\PC.Handoff" in script
    assert "WindowsPowerShell\\v1.0\\powershell.exe" in script

    msgCmd = buildMsgCommand("Title\nLine", "Body\tText", timeoutSeconds=12)
    assert msgCmd == ["msg.exe", "*", "/time:12", "Title Line - Body Text"]


# --- NullNotifier & createNotifier --------------------------------------------------------


def test_null_notifier_and_create_notifier_factory() -> None:
    logger = logging.getLogger("pc_handoff.test.notifications")
    null = NullNotifier()
    assert isinstance(null, Notifier)
    null.notify(HostNotification(title="T", body="B"))
    null.notifyEvents([])
    null.close()

    cfgDisabled = replace(Config(), enableHostNotifications=False)
    assert isinstance(createNotifier(cfgDisabled, logger), NullNotifier)

    cfgNone = replace(Config(), hostNotificationMethod="none")
    assert isinstance(createNotifier(cfgNone, logger), NullNotifier)

    cfgToast = replace(Config(), enableHostNotifications=True, hostNotificationMethod="toast")
    notifier = createNotifier(cfgToast, logger, asyncDispatch=False)
    assert isinstance(notifier, WindowsNotifier)
    notifier.close()


# --- WindowsNotifier sync, async, and failure containment ---------------------------------


def test_windows_notifier_sync_and_async_dispatch() -> None:
    calls: list[list[str]] = []

    def fakeRunner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    logger = logging.getLogger("pc_handoff.test.notifications")

    # 1. Synchronous toast and msg dispatch
    syncToast = WindowsNotifier(
        logger,
        method="toast",
        runner=fakeRunner,
        asyncDispatch=False,
    )
    evQueued = newReservationEvent(makeRes("Bob"), ReservationEventType.RESERVATION_QUEUED, T0)
    evCancelled = newReservationEvent(
        makeRes("Bob"),
        ReservationEventType.RESERVATION_CANCELLED,
        T0,
    )
    syncToast.notifyEvents([evQueued, evCancelled])
    assert len(calls) == 1
    assert calls[0][0] == "powershell.exe"

    syncMsg = WindowsNotifier(
        logger,
        method="msg",
        timeoutSeconds=8,
        runner=fakeRunner,
        asyncDispatch=False,
    )
    syncMsg.notify(HostNotification(title="Alert", body="Ready"))
    assert len(calls) == 2
    assert calls[1] == ["msg.exe", "*", "/time:8", "Alert - Ready"]

    # Disabled or unknown method is a no-op
    disabled = WindowsNotifier(logger, enabled=False, runner=fakeRunner, asyncDispatch=False)
    disabled.notify(HostNotification(title="Alert", body="Ignored"))
    disabled.notifyEvents([evQueued])
    unknownMethod = WindowsNotifier(
        logger,
        method="unknown",
        runner=fakeRunner,
        asyncDispatch=False,
    )
    unknownMethod.notify(HostNotification(title="Alert", body="Ignored"))
    assert len(calls) == 2

    # 2. Asynchronous background worker dispatch + flush + close
    asyncNotifier = WindowsNotifier(
        logger,
        method="toast",
        runner=fakeRunner,
        asyncDispatch=True,
    )
    asyncNotifier.notify(HostNotification(title="Async", body="First"))
    asyncNotifier.notify(HostNotification(title="Async", body="Second"))
    asyncNotifier.flush()
    assert len(calls) == 4
    asyncNotifier.close()


def test_windows_notifier_contains_subprocess_errors_and_nonzero_exit(
    capturedAppLog: Callable[[], list[dict[str, Any]]],
) -> None:
    logger = logging.getLogger("pc_handoff.notifications")

    def nonzeroRunner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="access denied")

    n1 = WindowsNotifier(logger, method="msg", runner=nonzeroRunner, asyncDispatch=False)
    n1.notify(HostNotification(title="T", body="B"))

    def raisingRunner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=5)

    n2 = WindowsNotifier(logger, method="toast", runner=raisingRunner, asyncDispatch=False)
    n2.notify(HostNotification(title="T", body="B"))

    # Also test submitting after executor is manually shut down
    n3 = WindowsNotifier(logger, method="toast", runner=nonzeroRunner, asyncDispatch=True)
    assert n3._executor is not None
    n3._executor.shutdown(wait=True)
    n3.notify(HostNotification(title="T", body="B"))
    n3.close()

    warnings = [line["message"] for line in capturedAppLog() if line["level"] == "WARNING"]
    assert "host notification command exited with non-zero status" in warnings
    assert "host notification delivery failed" in warnings
    assert "host notification skipped because executor is shut down" in warnings


def test_default_runner_uses_noop_in_pytest_and_invokes_monkeypatched_subprocess_run(
    monkeypatch,
) -> None:
    logger = logging.getLogger("pc_handoff.notifications")
    # Default runner under pytest does not spawn a real process
    nDefault = WindowsNotifier(logger, method="toast", asyncDispatch=False)
    nDefault.notify(HostNotification(title="T", body="B"))

    # When subprocess.run is monkeypatched, _defaultRunner delegates to it
    recorded: list[list[str]] = []

    def fakeSubprocessRun(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        recorded.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fakeSubprocessRun)
    nDefault.notify(HostNotification(title="T", body="B"))
    assert len(recorded) == 1


def test_windows_notifier_both_method_executes_msg_and_toast() -> None:
    logger = logging.getLogger("pc_handoff.notifications")
    dispatched: list[list[str]] = []

    def fakeRunner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        dispatched.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    notifier = WindowsNotifier(
        logger,
        method="both",
        timeoutSeconds=15,
        runner=fakeRunner,
        asyncDispatch=False,
    )
    notifier.notify(HostNotification(title="Queue Alert", body="Alice is ready"))
    assert len(dispatched) == 2
    assert dispatched[0][0] == "msg.exe"
    assert "/time:15" in dispatched[0]
    assert dispatched[1][0] == "powershell.exe"


def test_teams_notifier_builds_card_and_filters_events() -> None:
    card = buildTeamsAdaptiveCard("PC Ready", "Bob can claim")
    assert card["type"] == "message"
    assert card["attachments"][0]["content"]["body"][0]["text"] == "PC Ready"
    assert card["attachments"][0]["content"]["body"][1]["text"] == "Bob can claim"

    logger = logging.getLogger("pc_handoff.notifications")
    sent_payloads: list[dict[str, Any]] = []

    def fakeSender(url: str, data: bytes, timeout: float) -> None:
        import json

        sent_payloads.append(json.loads(data.decode("utf-8")))

    teams = TeamsNotifier(
        webhookUrl="https://example.com/webhook",
        logger=logger,
        enabled=True,
        sender=fakeSender,
        asyncDispatch=False,
    )

    # 1. Triggered events: QUEUED and HANDOFF_INITIATED
    res = makeRes("Bob")
    evQueued = newReservationEvent(res, ReservationEventType.RESERVATION_QUEUED, T0)
    evReady = newReservationEvent(res, ReservationEventType.HANDOFF_INITIATED, T0)
    evCancelled = newReservationEvent(res, ReservationEventType.RESERVATION_CANCELLED, T0)

    teams.notifyEvents([evQueued, evReady, evCancelled])
    assert len(sent_payloads) == 2
    assert "Queue Joined" in sent_payloads[0]["attachments"][0]["content"]["body"][0]["text"]
    assert "Ready to Claim" in sent_payloads[1]["attachments"][0]["content"]["body"][0]["text"]


def test_teams_notifier_failure_containment_and_async(
    capturedAppLog: Callable[[], list[dict[str, Any]]],
) -> None:
    logger = logging.getLogger("pc_handoff.notifications")

    def failingSender(url: str, data: bytes, timeout: float) -> None:
        raise OSError("network down")

    teams = TeamsNotifier(
        webhookUrl="https://example.com/webhook",
        logger=logger,
        enabled=True,
        sender=failingSender,
        asyncDispatch=True,
    )
    teams.notify(HostNotification(title="Alert", body="Test"))
    teams.flush()
    teams.close()

    warnings = [line["message"] for line in capturedAppLog() if line["level"] == "WARNING"]
    assert "teams notification dispatch failed" in warnings


def test_create_notifier_composite_wiring() -> None:
    logger = logging.getLogger("pc_handoff.notifications")

    # Both host and teams enabled -> CompositeNotifier
    cfg = Config(
        enableHostNotifications=True,
        hostNotificationMethod="both",
        enableTeamsNotifications=True,
        teamsWebhookUrl="https://example.com/webhook",
    )
    notifier = createNotifier(cfg, logger, asyncDispatch=False)
    assert isinstance(notifier, CompositeNotifier)
    assert len(notifier.notifiers) == 2
    notifier.flush()
    notifier.close()

    # Only teams enabled
    cfgTeamsOnly = Config(
        enableHostNotifications=False,
        enableTeamsNotifications=True,
        teamsWebhookUrl="https://example.com/webhook",
    )
    nTeams = createNotifier(cfgTeamsOnly, logger, asyncDispatch=False)
    assert isinstance(nTeams, TeamsNotifier)
    nTeams.close()

    # Neither enabled
    cfgNone = Config(
        enableHostNotifications=False,
        enableTeamsNotifications=False,
    )
    nNone = createNotifier(cfgNone, logger)
    assert isinstance(nNone, NullNotifier)
    nNone.flush()
    nNone.close()
