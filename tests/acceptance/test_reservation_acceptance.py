"""End-to-end acceptance tests for reservation queue and status notes (REQ-002, TASK-016)."""

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

from pc_handoff import launcher
from pc_handoff.domain.reservations import ReservationEventType, ReservationStatus
from pc_handoff.persistence.schemas import (
    validateActivityDocument,
    validateReservationsDocument,
)
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


def unescape(text: str) -> str:
    return re.sub(r"\\(.)", r"\1", text)


def shown(elements) -> list[str]:
    return [unescape(e.value) for e in elements]


def readJson(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


class TestReservationAcceptance:
    def test_crash_and_restart_mid_queue_restores_state_and_reconciles_sessions(
        self, tmp_path: Path
    ) -> None:
        dataDir = tmp_path / "data"
        dataDir.mkdir()
        clock = FakeClock(T0)
        provider = FakeSessionProvider([osSession(1, "alice", connectTime=T0)])

        # 1. Start runtime with alice connected; bob and carol join the queue; alice posts a note
        runtime1 = launcher.buildRuntime(dataDir, provider, clock)
        runtime1.pollOnce("t-boot-1")
        rBob = runtime1.reservationManager.enqueueReservation("bob", 60, notes="Firmware build")
        rCarol = runtime1.reservationManager.enqueueReservation("carol", 45)
        note = runtime1.reservationManager.postStatusNote(
            "alice", "Finishing regression run", ttlMinutes=30
        )
        assert rBob.status is ReservationStatus.QUEUED
        assert rCarol.status is ReservationStatus.QUEUED

        # Verify on-disk schemas before restart
        assert validateReservationsDocument(readJson(dataDir / "reservations.json")) == []
        assert validateActivityDocument(readJson(dataDir / "activity_log.json")) == []

        # 2. Simulate crash & restart while bob has connected over RDP during the downtime
        registry.clearRuntime()
        clock.advance(60)
        provider.setSessions([osSession(2, "bob", connectTime=clock.now())])

        runtime2 = launcher.buildRuntime(dataDir, provider, clock)
        registry.registerRuntime(runtime2)

        # Before first poll, loaded queueState is preserved in the pre-poll snapshot
        prePollQueue = runtime2.stateStore.current().queueState
        assert len(prePollQueue.queuedReservations) == 2
        assert prePollQueue.statusNotes == (note,)

        # First poll after restart reconciles bob's active RDP session with bob's head reservation
        runtime2.pollOnce("t-boot-2")
        reconciledQueue = runtime2.stateStore.current().queueState
        assert reconciledQueue.findById(rBob.reservationId).status is ReservationStatus.ACTIVE
        assert reconciledQueue.findById(rCarol.reservationId).status is ReservationStatus.QUEUED
        assert reconciledQueue.statusNotes == (note,)

        # Render dashboard and verify both the active queue and status note are displayed
        app = AppTest.from_file(SCRIPT, default_timeout=TIMEOUT).run()
        assert not app.exception
        assert "Reservation queue" in shown(app.subheader)
        assert "Status notes" in shown(app.subheader)
        assert any("Finishing regression run" in m for m in shown(app.markdown))

        events = readJson(dataDir / "activity_log.json")["events"]
        claimedEvents = [
            e for e in events if e["eventType"] == ReservationEventType.HANDOFF_CLAIMED.value
        ]
        assert len(claimedEvents) == 1
        assert claimedEvents[0]["userName"] == "bob"
        assert claimedEvents[0]["reason"] == "detected_at_startup"

    def test_corrupt_reservations_json_at_startup_is_backed_up_and_shown_as_notice(
        self, tmp_path: Path
    ) -> None:
        dataDir = tmp_path / "data"
        dataDir.mkdir()
        (dataDir / "reservations.json").write_text("{corrupt json", encoding="utf-8")
        clock = FakeClock(T0)
        provider = FakeSessionProvider([])

        runtime = launcher.buildRuntime(dataDir, provider, clock)
        runtime.pollOnce("t-corrupt")
        registry.registerRuntime(runtime)

        app = AppTest.from_file(SCRIPT, default_timeout=TIMEOUT).run()
        assert not app.exception
        assert any(
            w.startswith("Reservation data was reset (backup: reservations.json.corrupt-")
            for w in shown(app.warning)
        )
        backups = list(dataDir.glob("reservations.json.corrupt-*"))
        assert len(backups) == 1
