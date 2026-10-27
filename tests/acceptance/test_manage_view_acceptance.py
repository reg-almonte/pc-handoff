"""End-to-end acceptance tests for the Manage View, tabs, wait estimates, and .rdp (TASK-022)."""

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

from pc_handoff import launcher
from pc_handoff.domain.reservations import (
    NoteCategory,
    ReservationEventType,
    ReservationStatus,
)
from pc_handoff.persistence.schemas import (
    validateActivityDocument,
    validateReservationsDocument,
)
from pc_handoff.runtime import registry
from pc_handoff.ui.navigation import NAV_TO_MANAGE_KEY, NAV_TO_STATUS_KEY
from pc_handoff.ui.view_model import generateRdpFileContent
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


class TestManageViewAcceptance:
    def test_two_view_navigation_wait_estimate_slot_editing_and_categorized_notes(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("COMPUTERNAME", "LAB-SHARED-01")
        dataDir = tmp_path / "data"
        dataDir.mkdir()
        clock = FakeClock(T0)
        provider = FakeSessionProvider([osSession(1, "alice", connectTime=T0)])

        runtime = launcher.buildRuntime(dataDir, provider, clock)
        runtime.pollOnce("t-1")
        registry.registerRuntime(runtime)

        # 1. Start on passive Status View: no text inputs on main page, header button present
        app = AppTest.from_file(SCRIPT, default_timeout=TIMEOUT).run()
        assert not app.exception
        assert not app.text_input
        assert any(b.key == NAV_TO_MANAGE_KEY for b in app.button)

        # 2. Navigate to dedicated Manage View via header action button
        app.button(key=NAV_TO_MANAGE_KEY).click().run()
        assert not app.exception
        assert any(b.key == NAV_TO_STATUS_KEY for b in app.button)
        assert any("Estimated wait: ~0 min" in i for i in shown(app.info))

        # 3. Bob joins the queue (30m) while Alice is using the PC
        app.text_input(key="joinUserName").set_value("bob")
        app.number_input(key="joinDurationMinutes").set_value(30)
        app.text_input(key="joinNotes").set_value("Initial run")
        app.button(key="joinQueueBtn").click().run()
        assert not app.exception
        assert any("Wait: ~30 min" in i for i in shown(app.info))

        resBob = runtime.reservationManager.currentState().findByUser("bob")
        assert resBob is not None and resBob.status is ReservationStatus.QUEUED

        # 4. Carol joins 2nd in line (45m) -> wait estimate updates to ~75 min
        app.text_input(key="joinUserName").set_value("carol")
        app.number_input(key="joinDurationMinutes").set_value(45)
        app.text_input(key="joinNotes").set_value("Benchmark")
        app.button(key="joinQueueBtn").click().run()
        assert not app.exception
        assert any("Wait: ~75 min" in i for i in shown(app.info))

        resCarol = runtime.reservationManager.currentState().findByUser("carol")
        assert resCarol is not None

        # 5. Bob edits his queued slot (30m -> 60m) without losing Position #1
        app.number_input(key=f"edit-duration-{resBob.reservationId}").set_value(60)
        app.text_input(key=f"edit-notes-{resBob.reservationId}").set_value("Extended run")
        app.button(key=f"update-{resBob.reservationId}").click().run()
        assert not app.exception

        qState = runtime.reservationManager.currentState()
        assert [r.userName for r in qState.queuedReservations] == ["bob", "carol"]
        assert qState.findById(resBob.reservationId).requestedDurationMinutes == 60
        assert qState.findById(resBob.reservationId).notes == "Extended run"
        assert any("Wait: ~105 min" in i for i in shown(app.info))

        # 6. Post a categorized status note ("maintenance")
        app.text_input(key="noteUserName").set_value("alice")
        app.text_input(key="noteMessage").set_value("Applying driver update")
        app.selectbox(key="noteCategory").select("maintenance")
        app.number_input(key="noteTtlMinutes").set_value(90)
        app.button(key="postStatusNoteBtn").click().run()
        assert not app.exception
        assert any("[Maintenance]" in m for m in shown(app.markdown))
        assert runtime.reservationManager.currentState().statusNotes[0].category is (
            NoteCategory.MAINTENANCE
        )

        # 7. Verify safe .rdp content generation for host
        rdpContent = generateRdpFileContent("LAB-SHARED-01", 3389)
        assert "full address:s:LAB-SHARED-01:3389\n" in rdpContent

        # 8. Navigate back to Status View and verify passive monitoring state & top note banner
        app.button(key=NAV_TO_STATUS_KEY).click().run()
        assert not app.exception
        assert not app.text_input
        assert not app.checkbox
        assert any("[Maintenance]" in m for m in shown(app.markdown))
        assert any(
            "Status Note [Maintenance]" in i and "#status-notes" in i for i in shown(app.info)
        )

        # 9. Validate persisted JSON files against schemas
        actDoc = readJson(dataDir / "activity_log.json")
        resDoc = readJson(dataDir / "reservations.json")
        assert validateActivityDocument(actDoc) == []
        assert validateReservationsDocument(resDoc) == []

        eventTypes = [
            e["eventType"]
            for e in actDoc["events"]
            if e["eventType"] not in ("session_start", "session_end")
        ]
        assert eventTypes == [
            ReservationEventType.RESERVATION_QUEUED.value,
            ReservationEventType.RESERVATION_QUEUED.value,
            ReservationEventType.RESERVATION_UPDATED.value,
            ReservationEventType.STATUS_NOTE_POSTED.value,
        ]
        noteEvent = next(
            e
            for e in actDoc["events"]
            if e["eventType"] == ReservationEventType.STATUS_NOTE_POSTED.value
        )
        assert noteEvent["category"] == "maintenance"
        assert resDoc["statusNotes"][0]["category"] == "maintenance"
