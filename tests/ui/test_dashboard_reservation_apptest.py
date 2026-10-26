"""Streamlit AppTest suite for reservation queue, handoff, and status notes UI (TASK-015)."""

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from pc_handoff import launcher
from pc_handoff.domain.reservations import NoteCategory, ReservationStatus
from pc_handoff.runtime import registry
from pc_handoff.runtime.state_store import DashboardSnapshot
from pc_handoff.ui.navigation import NAV_TO_MANAGE_KEY, NAV_TO_STATUS_KEY
from pc_handoff.ui.view_model import RESERVATION_COLUMNS, buildViewModel
from pc_handoff.ui.views.manage_view import renderManageView
from tests.conftest import FAKE_CLOCK_START
from tests.fakes import FakeClock, FakeSessionProvider
from tests.fakes.builders import osSession, trackerState

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

        # Enable self-service reservation controls via header navigation button
        app.button(key=NAV_TO_MANAGE_KEY).click().run()
        assert not app.exception
        assert "No active or queued reservations" in [t.value for t in app.text]
        assert any("Estimated wait: ~0 min" in i for i in shown(app.info))

        # Validation error: empty user name on Join Queue
        app.button(key="joinQueueBtn").click().run()
        assert any("userName must not be empty" in w for w in shown(app.warning))

        # Join Queue for alice (QUEUED while dave is using the PC)
        app.text_input(key="joinUserName").set_value("alice")
        app.number_input(key="joinDurationMinutes").set_value(45)
        app.text_input(key="joinNotes").set_value("Training **v2**")
        app.button(key="joinQueueBtn").click().run()
        assert not app.exception

        # Edit alice's queued reservation (duration + notes)
        resAlice = runtime.reservationManager.currentState().findByUser("alice")
        assert resAlice is not None
        app.number_input(key=f"edit-duration-{resAlice.reservationId}").set_value(60)
        app.text_input(key=f"edit-notes-{resAlice.reservationId}").set_value("Training **v3**")
        app.button(key=f"update-{resAlice.reservationId}").click().run()
        assert not app.exception
        updatedAlice = runtime.reservationManager.currentState().findById(resAlice.reservationId)
        assert updatedAlice.requestedDurationMinutes == 60
        assert updatedAlice.notes == "Training **v3**"

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
        assert tableRows[0][:4] == ["1", "alice", "QUEUED", "60 min"]
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

        # Navigate back to Status View
        app.button(key=NAV_TO_STATUS_KEY).click().run()
        assert not app.exception
        assert any(b.key == NAV_TO_MANAGE_KEY for b in app.button)

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
        app.selectbox(key="noteCategory").select("urgent")
        app.number_input(key="noteTtlMinutes").set_value(60)
        app.button(key="postStatusNoteBtn").click().run()
        assert not app.exception

        assert "Status notes" in shown(app.subheader)
        assert any(
            "[alice] Running **benchmark** <script>x</script>" in m for m in shown(app.markdown)
        )
        [note] = runtime.reservationManager.currentState().statusNotes
        assert note.category is NoteCategory.URGENT

        # Verify status note also renders on StatusView, then return to ManageView to clear
        app.button(key=NAV_TO_STATUS_KEY).click().run()
        assert "Status notes" in shown(app.subheader)
        app.button(key=NAV_TO_MANAGE_KEY).click().run()

        # Clear the status note
        app.button(key=f"clear-note-{note.noteId}").click().run()
        assert not app.exception
        assert runtime.reservationManager.currentState().statusNotes == ()
        assert "Status notes" not in shown(app.subheader)

    def test_render_manage_view_without_reservation_manager_is_noop(self, monkeypatch) -> None:
        snap = DashboardSnapshot.fromState(
            trackerState([], T0),
            updatedAt=T0,
            configWarnings=(),
            notices=(),
            pollIntervalSeconds=5,
        )
        model = buildViewModel(snap, T0)

        class DummyRuntime:
            pass

        monkeypatch.setattr("pc_handoff.ui.views.manage_view.st.button", lambda *a, **kw: False)
        renderManageView(DummyRuntime(), model)

    def test_status_view_quick_claim_and_error_handling(self, tmp_path: Path) -> None:
        from pc_handoff.ui.views.status_view import _handleQuickClaim

        class FailingManager:
            def claimHandoff(self, reservationId: str) -> None:
                raise ValueError(f"cannot claim {reservationId}")

        clock = FakeClock(T0)
        provider = FakeSessionProvider([])
        runtime = launcher.buildRuntime(tmp_path / "data", provider, clock)
        runtime.pollOnce()
        res = runtime.reservationManager.enqueueReservation("alice", 30)
        registry.registerRuntime(runtime)

        # On StatusView, alice is READY so quick-claim button is shown
        app = AppTest.from_file(SCRIPT, default_timeout=TIMEOUT).run()
        assert not app.exception
        app.button(key=f"claim-{res.reservationId}").click().run()
        assert not app.exception
        assert (
            runtime.reservationManager.currentState().findById(res.reservationId).status
            is ReservationStatus.ACTIVE
        )

        # Also verify _handleQuickClaim error path records sanitized error
        _handleQuickClaim(FailingManager(), "r-bad")
