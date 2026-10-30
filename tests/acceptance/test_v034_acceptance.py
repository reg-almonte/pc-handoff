"""End-to-end acceptance tests for v0.3.4 (REQ-004, SD003).

Covers:
1. Configurable claim grace window and re-queue rotation across multiple users.
2. Single-user queue timeout retaining reservation as REQUEUED.
3. Silent handoff auto-claim via client IP address correlation.
4. Status View and Active Session table real-name enrichment.
5. Multi-channel host and Teams webhook notification delivery.
"""

import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

from pc_handoff import launcher
from pc_handoff.detection.provider import ConnectionType
from pc_handoff.domain.reservations import (
    HandoffStatus,
    ReservationStatus,
)
from pc_handoff.persistence.known_users_store import KnownUsersStore
from pc_handoff.runtime import registry
from tests.conftest import FAKE_CLOCK_START
from tests.fakes import FakeClock, FakeSessionProvider
from tests.fakes.builders import osSession

SCRIPT = str(launcher.DASHBOARD_SCRIPT)
T0 = FAKE_CLOCK_START
TIMEOUT = 30


@pytest.fixture(autouse=True)
def cleanRegistry() -> Iterator[None]:
    registry.clearRuntime()
    yield
    registry.clearRuntime()


def shown(elements) -> list[str]:
    return [e.value for e in elements]


class TestV034Acceptance:
    def test_configurable_grace_and_requeue_workflow(self, tmp_path: Path) -> None:
        dataDir = tmp_path / "data"
        dataDir.mkdir()
        configPath = dataDir / "config.json"
        configPath.write_text(
            json.dumps(
                {"schemaVersion": 1, "claimGraceMinutes": 120, "hostNotificationMethod": "none"}
            ),
            encoding="utf-8",
        )
        clock = FakeClock(T0)
        provider = FakeSessionProvider([osSession(1, "alice", connectTime=T0)])

        runtime = launcher.buildRuntime(dataDir, provider, clock)
        assert runtime.reservationManager.claimGraceSeconds == 120 * 60
        runtime.pollOnce("t-1")
        registry.registerRuntime(runtime)

        # Bob (1st) and Carol (2nd) join queue
        rBob = runtime.reservationManager.enqueueReservation("bob", 60)
        rCarol = runtime.reservationManager.enqueueReservation("carol", 45)

        # Alice disconnects -> Bob promoted to READY with 2-hour claim window
        clock.advance(5)
        provider.setSessions([])
        runtime.pollOnce("t-2")
        qState = runtime.reservationManager.currentState()
        assert qState.findById(rBob.reservationId).status is ReservationStatus.READY
        assert qState.findById(rBob.reservationId).claimExpiresAt == clock.now() + timedelta(
            hours=2
        )

        # Bob does not claim within 2 hours -> Bob re-queues to tail; Carol advances to READY
        clock.advance(120 * 60 + 1)
        runtime.pollOnce("t-3")
        qState = runtime.reservationManager.currentState()
        assert qState.findById(rBob.reservationId).status is ReservationStatus.REQUEUED
        assert qState.findById(rBob.reservationId).claimExpiresAt is None
        assert qState.findById(rCarol.reservationId).status is ReservationStatus.READY
        assert [r.userName for r in qState.nonTerminalReservations] == ["carol", "bob"]

        # Carol does not claim either -> Carol re-queues to tail; Bob is at head and READY again
        clock.advance(120 * 60 + 1)
        runtime.pollOnce("t-4")
        qState = runtime.reservationManager.currentState()
        assert qState.findById(rCarol.reservationId).status is ReservationStatus.REQUEUED
        assert qState.findById(rBob.reservationId).status is ReservationStatus.READY
        assert [r.userName for r in qState.nonTerminalReservations] == ["bob", "carol"]

    def test_single_user_queue_expiration_remains_requeued(self, tmp_path: Path) -> None:
        dataDir = tmp_path / "data"
        dataDir.mkdir()
        clock = FakeClock(T0)
        provider = FakeSessionProvider([])

        runtime = launcher.buildRuntime(dataDir, provider, clock)
        runtime.pollOnce("t-1")
        registry.registerRuntime(runtime)

        rDave = runtime.reservationManager.enqueueReservation("dave", 30, pcAvailable=True)
        assert rDave.status is ReservationStatus.READY

        # Dave's claim window expires -> stays in queue alone with status REQUEUED
        clock.advance(runtime.reservationManager.claimGraceSeconds + 1)
        runtime.pollOnce("t-2")
        qState = runtime.reservationManager.currentState()
        dave = qState.findById(rDave.reservationId)
        assert dave.status is ReservationStatus.REQUEUED
        assert dave.claimExpiresAt is None
        assert qState.handoff.status is HandoffStatus.TIMED_OUT
        assert len(qState.nonTerminalReservations) == 1

        # Dave can still claim directly via dashboard
        claimed = runtime.reservationManager.claimHandoff(rDave.reservationId)
        assert claimed.status is ReservationStatus.ACTIVE
        assert runtime.reservationManager.currentState().activeReservation.userName == "dave"

    def test_silent_handoff_ip_autoclaim_and_real_name_display(self, tmp_path: Path) -> None:
        dataDir = tmp_path / "data"
        dataDir.mkdir()
        clock = FakeClock(T0)
        provider = FakeSessionProvider([])

        runtime = launcher.buildRuntime(dataDir, provider, clock)
        runtime.pollOnce("t-1")
        registry.registerRuntime(runtime)

        # 1. User enqueues with Real Name and IP address
        rJohn = runtime.reservationManager.enqueueReservation(
            "John Doe",
            60,
            clientAddress="192.168.1.150",
            pcAvailable=True,
        )
        assert rJohn.status is ReservationStatus.READY

        # Known users directory was populated
        known = KnownUsersStore(runtime.reservationManager.reservationStore.paths, clock).load()
        assert "192.168.1.150" in known
        assert known["192.168.1.150"].displayName == "John Doe"

        # 2. Silent handoff: John logs in via RDP using generic shared account 'operator1'
        clock.advance(30)
        provider.setSessions(
            [
                osSession(
                    2,
                    "operator1",
                    clientAddress="192.168.1.150",
                    connectTime=clock.now(),
                    connectionType=ConnectionType.RDP,
                )
            ]
        )
        runtime.pollOnce("t-2")

        # Reservation was automatically claimed via matching IP!
        qState = runtime.reservationManager.currentState()
        assert qState.findById(rJohn.reservationId).status is ReservationStatus.ACTIVE
        assert qState.handoff.status is HandoffStatus.CLAIMED

        # 3. Status View renders enriched status badge and table
        app = AppTest.from_file(SCRIPT, default_timeout=TIMEOUT).run()
        assert not app.exception
        # Primary status badge includes Real Name [OS Account] (IP)
        assert any("John Doe [operator1] (192.168.1.150)" in b.label for b in app.button)

    def test_multi_channel_and_teams_notification_delivery(self, tmp_path: Path) -> None:
        import logging

        from pc_handoff.notifications.windows_notifier import createNotifier
        from pc_handoff.persistence.config_store import loadConfig
        from pc_handoff.persistence.paths import DataPaths

        dataDir = tmp_path / "data"
        dataDir.mkdir()
        configPath = dataDir / "config.json"
        configPath.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "enableHostNotifications": True,
                    "hostNotificationMethod": "both",
                    "enableTeamsNotifications": True,
                    "teamsWebhookUrl": "https://company.webhook.office.com/webhookb2/test",
                }
            ),
            encoding="utf-8",
        )
        clock = FakeClock(T0)
        provider = FakeSessionProvider([])

        dispatchedHost: list[list[str]] = []
        sentTeams: list[dict[str, Any]] = []

        def fakeHostRunner(cmd: list[str], **kwargs: Any):
            dispatchedHost.append(cmd)
            import subprocess

            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        def fakeTeamsSender(url: str, data: bytes, timeout: float):
            sentTeams.append(json.loads(data.decode("utf-8")))

        logger = logging.getLogger("test")
        paths = DataPaths(dataDir)
        cfg = loadConfig(paths, clock, logger).config
        notifier = createNotifier(
            cfg,
            logger,
            runner=fakeHostRunner,
            teamsSender=fakeTeamsSender,
            asyncDispatch=False,
        )

        runtime = launcher.buildRuntime(dataDir, provider, clock, notifier=notifier)
        runtime.pollOnce("t-1")

        # 1. Queue join triggers host broadcast (both msg and toast) AND Teams Adaptive Card
        runtime.reservationManager.enqueueReservation("Alice", 60)
        assert len(sentTeams) >= 1
        assert "Queue Joined" in sentTeams[0]["attachments"][0]["content"]["body"][0]["text"]
        hostCommands = [cmd[0] for cmd in dispatchedHost]
        assert "msg.exe" in hostCommands
        assert "powershell.exe" in hostCommands

        notifier.close()
