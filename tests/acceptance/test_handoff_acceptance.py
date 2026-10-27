"""End-to-end acceptance tests for ownership handoff and timeout auto-advance (TASK-016)."""

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

from pc_handoff import launcher
from pc_handoff.domain.reservations import (
    CLAIM_GRACE_SECONDS,
    HandoffStatus,
    ReservationEventType,
    ReservationStatus,
)
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


class TestHandoffAcceptance:
    def test_full_queue_and_handoff_lifecycle_through_ui_and_poller(self, tmp_path: Path) -> None:
        dataDir = tmp_path / "data"
        dataDir.mkdir()
        clock = FakeClock(T0)
        provider = FakeSessionProvider([osSession(1, "alice", connectTime=T0)])

        runtime = launcher.buildRuntime(dataDir, provider, clock)
        runtime.pollOnce("t-1")
        registry.registerRuntime(runtime)

        app = AppTest.from_file(SCRIPT, default_timeout=TIMEOUT).run()
        app.button(key="nav_to_manage").click().run()

        # 1. Bob joins the queue via the UI while Alice is using the PC
        app.text_input(key="joinUserName").set_value("bob")
        app.number_input(key="joinDurationMinutes").set_value(60)
        app.text_input(key="joinNotes").set_value("Hardware verification")
        app.button(key="joinQueueBtn").click().run()
        assert not app.exception

        resBob = runtime.reservationManager.currentState().findByUser("bob")
        assert resBob is not None and resBob.status is ReservationStatus.QUEUED

        # 2. Alice clicks "Ready to Handoff" on the dashboard
        clock.advance(10)
        app.text_input(key="handoffOfferer").set_value("alice")
        app.button(key="initiateHandoffBtn").click().run()
        assert not app.exception
        assert any("Handoff offered by alice to bob" in m for m in shown(app.markdown))

        # 3. Alice disconnects and Bob connects over RDP -> poller correlates Bob into ACTIVE
        clock.advance(15)
        provider.setSessions([osSession(2, "bob", connectTime=clock.now())])
        runtime.pollOnce("t-2")
        app.run()
        assert not app.exception

        qState = runtime.reservationManager.currentState()
        assert qState.findById(resBob.reservationId).status is ReservationStatus.ACTIVE
        assert qState.handoff.status is HandoffStatus.CLAIMED

        # 4. Bob finishes and logs off -> poller marks Bob's reservation COMPLETED
        clock.advance(1800)
        provider.setSessions([])
        runtime.pollOnce("t-3")
        assert (
            runtime.reservationManager.currentState().findById(resBob.reservationId).status
            is ReservationStatus.COMPLETED
        )

        # 5. Validate on-disk audit trail and reservation document schemas
        actDoc = readJson(dataDir / "activity_log.json")
        resDoc = readJson(dataDir / "reservations.json")
        assert validateActivityDocument(actDoc) == []
        assert validateReservationsDocument(resDoc) == []

        resEvents = [
            (e["eventType"], e["userName"], e.get("offeredByUserName"))
            for e in actDoc["events"]
            if e["eventType"] not in ("session_start", "session_end")
        ]
        assert resEvents == [
            (ReservationEventType.RESERVATION_QUEUED.value, "bob", None),
            (ReservationEventType.HANDOFF_INITIATED.value, "bob", "alice"),
            (ReservationEventType.HANDOFF_CLAIMED.value, "bob", "alice"),
        ]

    def test_handoff_claim_timeout_auto_advances_to_next_queued_user(self, tmp_path: Path) -> None:
        dataDir = tmp_path / "data"
        dataDir.mkdir()
        clock = FakeClock(T0)
        provider = FakeSessionProvider([osSession(1, "alice", connectTime=T0)])

        runtime = launcher.buildRuntime(dataDir, provider, clock)
        runtime.pollOnce("t-1")
        registry.registerRuntime(runtime)

        # Bob (1st) and Carol (2nd) join the queue
        rBob = runtime.reservationManager.enqueueReservation("bob", 30)
        rCarol = runtime.reservationManager.enqueueReservation("carol", 45)

        # Alice logs off -> Bob is promoted to READY with a 5-minute claim window
        clock.advance(5)
        provider.setSessions([])
        runtime.pollOnce("t-2")
        assert (
            runtime.reservationManager.currentState().findById(rBob.reservationId).status
            is ReservationStatus.READY
        )

        # 5 minutes elapse without Bob claiming -> Bob expires, Carol auto-advances to READY
        clock.advance(CLAIM_GRACE_SECONDS + 1)
        runtime.pollOnce("t-3")
        qState = runtime.reservationManager.currentState()
        assert qState.findById(rBob.reservationId).status is ReservationStatus.EXPIRED
        assert qState.findById(rCarol.reservationId).status is ReservationStatus.READY
        assert qState.handoff.status is HandoffStatus.OFFERED
        assert qState.handoff.targetUserName == "carol"

        app = AppTest.from_file(SCRIPT, default_timeout=TIMEOUT).run()
        assert not app.exception
        assert any("PC is ready for carol" in m for m in shown(app.markdown))

        # Carol claims via UI button -> Carol becomes ACTIVE
        app.button(key=f"claim-{rCarol.reservationId}").click().run()
        assert not app.exception
        assert (
            runtime.reservationManager.currentState().findById(rCarol.reservationId).status
            is ReservationStatus.ACTIVE
        )

        actDoc = readJson(dataDir / "activity_log.json")
        assert validateActivityDocument(actDoc) == []
        resEventTypes = [
            (e["eventType"], e["userName"])
            for e in actDoc["events"]
            if e["eventType"] not in ("session_start", "session_end")
        ]
        assert resEventTypes == [
            (ReservationEventType.RESERVATION_QUEUED.value, "bob"),
            (ReservationEventType.RESERVATION_QUEUED.value, "carol"),
            (ReservationEventType.HANDOFF_INITIATED.value, "bob"),
            (ReservationEventType.RESERVATION_EXPIRED.value, "bob"),
            (ReservationEventType.HANDOFF_INITIATED.value, "carol"),
            (ReservationEventType.HANDOFF_CLAIMED.value, "carol"),
        ]
