"""Streamlit AppTest suite for reservation queue, handoff, and status notes UI (TASK-015)."""

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from pc_handoff import launcher
from pc_handoff.domain.reservations import ReservationStatus
from pc_handoff.runtime import registry
from pc_handoff.ui.view_model import RESERVATION_COLUMNS
from tests.conftest import FAKE_CLOCK_START
from tests.fakes import FakeClock, FakeSessionProvider
from tests.fakes.builders import osSession

SCRIPT = str(launcher.DASHBOARD_SCRIPT)
RESERVATION_CONTROLS_KEY = "showReservationControls"
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


class TestDashboardReservationControls:
    def test_self_service_queue_join_claim_handoff_cancel_and_validation_error(
        self, tmp_path: Path
    ) -> None:
        clock = FakeClock(T0)
        provider = FakeSessionProvider([osSession(1, "dave", connectTime=T0)])
        runtime = launcher.buildRuntime(tmp_path / "data", provider, clock)
        runtime.pollOnce()
        registry.registerRuntime(runtime)

        app = AppTest.from_file(SCRIPT, default_timeout=TIMEOUT).run()
        assert not app.exception

        # Enable self-service reservation controls
        app.checkbox(key=RESERVATION_CONTROLS_KEY).check().run()
        assert not app.exception
        assert "No active or queued reservations" in [t.value for t in app.text]

        # Validation error: empty user name on Join Queue
        app.button(key="joinQueueBtn").click().run()
        assert any("userName must not be empty" in w for w in shown(app.warning))

        # Join Queue for alice (QUEUED while dave is using the PC)
        app.text_input(key="joinUserName").set_value("alice")
        app.number_input(key="joinDurationMinutes").set_value(45)
        app.text_input(key="joinNotes").set_value("Training **v2**")
        app.button(key="joinQueueBtn").click().run()
        assert not app.exception

        # Join Queue for bob (2nd in line)
        app.text_input(key="joinUserName").set_value("bob")
        app.number_input(key="joinDurationMinutes").set_value(30)
        app.text_input(key="joinNotes").set_value("")
        app.button(key="joinQueueBtn").click().run()
        assert not app.exception

        assert "Reservation queue" in shown(app.subheader)
        resFrames = [f for f in app.dataframe if tuple(f.value.columns) == RESERVATION_COLUMNS]
        assert len(resFrames) == 1
        tableRows = resFrames[0].value.astype(str).values.tolist()
        assert len(tableRows) == 2
        assert tableRows[0][:4] == ["1", "alice", "QUEUED", "45 min"]
        assert tableRows[1][:4] == ["2", "bob", "QUEUED", "30 min"]

        # Dave initiates handoff -> alice becomes READY and handoff banner appears
        app.text_input(key="handoffOfferer").set_value("dave")
        app.button(key="initiateHandoffBtn").click().run()
        assert not app.exception
        assert any("Handoff offered by dave to alice" in m for m in shown(app.markdown))

        # Alice claims handoff via Claim Now button -> alice becomes ACTIVE (canClaim=False)
        resAlice = runtime.reservationManager.currentState().findByUser("alice")
        assert resAlice is not None
        app.button(key=f"claim-{resAlice.reservationId}").click().run()
        assert not app.exception
        assert (
            runtime.reservationManager.currentState().findById(resAlice.reservationId).status
            is ReservationStatus.ACTIVE
        )

        # Bob cancels his queued reservation via Cancel button
        resBob = runtime.reservationManager.currentState().findByUser("bob")
        assert resBob is not None
        app.button(key=f"cancel-{resBob.reservationId}").click().run()
        assert not app.exception
        assert (
            runtime.reservationManager.currentState().findById(resBob.reservationId).status
            is ReservationStatus.CANCELLED
        )

    def test_status_note_post_escape_and_clear(self, tmp_path: Path) -> None:
        clock = FakeClock(T0)
        provider = FakeSessionProvider([])
        runtime = launcher.buildRuntime(tmp_path / "data", provider, clock)
        runtime.pollOnce()
        registry.registerRuntime(runtime)

        app = AppTest.from_file(SCRIPT, default_timeout=TIMEOUT).run()
        app.checkbox(key=RESERVATION_CONTROLS_KEY).check().run()

        app.text_input(key="noteUserName").set_value("alice")
        app.text_input(key="noteMessage").set_value("Running **benchmark** <script>x</script>")
        app.number_input(key="noteTtlMinutes").set_value(60)
        app.button(key="postStatusNoteBtn").click().run()
        assert not app.exception

        assert "Status notes" in shown(app.subheader)
        assert any(
            "[alice] Running **benchmark** <script>x</script>" in m for m in shown(app.markdown)
        )

        # Clear the status note
        [note] = runtime.reservationManager.currentState().statusNotes
        app.button(key=f"clear-note-{note.noteId}").click().run()
        assert not app.exception
        assert runtime.reservationManager.currentState().statusNotes == ()
        assert "Status notes" not in shown(app.subheader)
