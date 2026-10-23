"""The placeholder dashboard page (TASK-009; replaced by TASK-010), run headless with AppTest."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from pc_handoff import launcher
from pc_handoff.runtime import registry
from tests.conftest import FAKE_CLOCK_START
from tests.fakes import FakeClock, FakeSessionProvider
from tests.fakes.builders import osSession

SCRIPT = str(launcher.DASHBOARD_SCRIPT)


@pytest.fixture(autouse=True)
def cleanRegistry():
    registry.clearRuntime()
    yield
    registry.clearRuntime()


def test_without_a_registered_runtime_the_page_says_how_to_start(tmp_path):
    app = AppTest.from_file(SCRIPT).run()
    assert not app.exception
    [error] = app.error
    assert "python -m pc_handoff" in error.value
    assert not app.text


def test_with_a_registered_runtime_the_page_shows_the_published_status(tmp_path: Path):
    provider = FakeSessionProvider([osSession(2, "alice")])
    runtime = launcher.buildRuntime(tmp_path / "data", provider, FakeClock(FAKE_CLOCK_START))
    registry.registerRuntime(runtime)
    app = AppTest.from_file(SCRIPT).run()
    assert not app.exception
    assert [t.value for t in app.text] == ["Status: Unknown"]
    runtime.pollOnce()
    app = AppTest.from_file(SCRIPT).run()
    assert [t.value for t in app.text] == ["Status: In Use"]
    assert not app.error
