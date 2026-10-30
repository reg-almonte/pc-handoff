"""End-to-end acceptance tests for host PC notifications (REQ-003, TASK-022)."""

import json
import logging
import subprocess
from pathlib import Path

from pc_handoff import launcher
from pc_handoff.domain.reservations import ReservationEventType
from pc_handoff.notifications.windows_notifier import WindowsNotifier
from tests.conftest import FAKE_CLOCK_START
from tests.fakes import FakeClock, FakeSessionProvider
from tests.fakes.builders import osSession

T0 = FAKE_CLOCK_START


def writeConfig(
    dataDir: Path,
    *,
    enabled: bool = True,
    method: str = "toast",
    timeoutSeconds: int = 10,
) -> None:
    dataDir.mkdir(parents=True, exist_ok=True)
    (dataDir / "config.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "dashboardRefreshIntervalSeconds": 10,
                "sessionPollIntervalSeconds": 5,
                "dashboardPort": 8501,
                "enableHostNotifications": enabled,
                "hostNotificationMethod": method,
                "hostNotificationTimeoutSeconds": timeoutSeconds,
            }
        ),
        encoding="utf-8",
    )


class TestHostNotificationAcceptance:
    def test_toast_and_msg_notifications_on_queue_join_handoff_and_expiration(
        self, tmp_path: Path
    ) -> None:
        dispatchedCommands: list[list[str]] = []

        def fakeRunner(args, **kwargs):
            dispatchedCommands.append(list(args))
            return subprocess.CompletedProcess(args, 0)

        dataDir = tmp_path / "data"
        writeConfig(dataDir, enabled=True, method="msg", timeoutSeconds=12)
        clock = FakeClock(T0)
        provider = FakeSessionProvider([osSession(1, "alice", connectTime=T0)])

        notifier = WindowsNotifier(
            logging.getLogger("test.notifications"),
            method="msg",
            timeoutSeconds=12,
            runner=fakeRunner,
            asyncDispatch=False,
        )
        runtime = launcher.buildRuntime(dataDir, provider, clock, notifier=notifier)
        runtime.pollOnce("t-1")

        # 1. Bob and Carol join queue -> RESERVATION_QUEUED notifications dispatched
        runtime.reservationManager.enqueueReservation("bob", 45, notes="Test suite")
        runtime.reservationManager.enqueueReservation("carol", 30)
        assert len(dispatchedCommands) == 2
        assert dispatchedCommands[0][:3] == ["msg.exe", "*", "/time:12"]
        assert "bob joined the reservation queue (45 min)" in dispatchedCommands[0][3]

        # 2. Alice offers handoff -> HANDOFF_INITIATED notification dispatched
        runtime.reservationManager.initiateHandoff("alice")
        assert len(dispatchedCommands) == 3
        assert "alice offered handoff to bob" in dispatchedCommands[2][3]

        # 3. Bob's claim window expires -> RESERVATION_REQUEUED + Carol HANDOFF_INITIATED
        clock.advance(runtime.reservationManager.claimGraceSeconds + 1)
        provider.setSessions([])
        runtime.pollOnce("t-2")
        assert len(dispatchedCommands) == 5
        assert "bob's claim window expired; handoff moved to carol" in dispatchedCommands[3][3]
        assert "PC is ready for carol" in dispatchedCommands[4][3]

    def test_disabled_notifications_and_subprocess_failure_containment(
        self, tmp_path: Path
    ) -> None:
        # 1. Disabled in config -> NullNotifier configured by buildRuntime
        disabledDir = tmp_path / "disabled"
        writeConfig(disabledDir, enabled=False, method="none")
        clock = FakeClock(T0)
        provider = FakeSessionProvider([osSession(1, "alice", connectTime=T0)])
        runtimeDisabled = launcher.buildRuntime(disabledDir, provider, clock)
        runtimeDisabled.pollOnce("t-1")
        res = runtimeDisabled.reservationManager.enqueueReservation("bob", 30)
        assert res.userName == "bob"

        # 2. Subprocess timeout/OS error never interrupts ReservationManager or poller
        def failingRunner(args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args, timeout=5)

        failingNotifier = WindowsNotifier(
            logging.getLogger("test.notifications"),
            method="toast",
            timeoutSeconds=5,
            runner=failingRunner,
            asyncDispatch=False,
        )
        activeDir = tmp_path / "active"
        writeConfig(activeDir, enabled=True, method="toast", timeoutSeconds=5)
        runtimeFailing = launcher.buildRuntime(activeDir, provider, clock, notifier=failingNotifier)
        runtimeFailing.pollOnce("t-1")
        rBob = runtimeFailing.reservationManager.enqueueReservation("bob", 60)
        assert rBob.userName == "bob"
        actDoc = json.loads((activeDir / "activity_log.json").read_text(encoding="utf-8"))
        assert any(
            e["eventType"] == ReservationEventType.RESERVATION_QUEUED.value
            for e in actDoc["events"]
        )
