"""The Streamlit bootstrap adapter with ``bootstrap.run`` monkeypatched (TASK-009 AC-9).

``server_launcher.py`` is omitted from coverage (ADR-006); this test pins the arguments it
hands to Streamlit's semi-internal API, and ``test_server_smoke.py`` runs the real server.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
import streamlit
from streamlit import config as streamlitConfig
from streamlit import net_util
from streamlit.web import bootstrap

from pc_handoff import server_launcher

EXPECTED_FLAGS = {
    "server.port": 18555,
    "server.address": "0.0.0.0",
    "server.headless": True,
    "server.fileWatcherType": "none",
    "server.runOnSave": False,
    "browser.gatherUsageStats": False,
    "logger.hideWelcomeMessage": True,
}


@pytest.fixture
def loadedFlags() -> Iterator[None]:
    """Restore Streamlit's process-wide config after a test applies real flag options."""
    yield
    streamlitConfig.get_config_options(force_reparse=True)


def test_streamlit_version_is_inside_the_pin():
    major, minor = (int(part) for part in streamlit.__version__.split(".")[:2])
    assert major == 1 and minor >= 40


def test_flags_bind_all_interfaces_on_the_port_and_disable_the_file_watcher():
    assert server_launcher.buildFlagOptions(18555) == EXPECTED_FLAGS


def test_config_options_are_loaded_before_run_on_the_calling_thread(monkeypatch, tmp_path):
    calls: list[tuple] = []
    monkeypatch.setattr(
        bootstrap, "load_config_options", lambda flag_options: calls.append(("load", flag_options))
    )
    monkeypatch.setattr(bootstrap, "run", lambda *args: calls.append(("run", *args)))
    script = tmp_path / "dashboard.py"

    server_launcher.runStreamlitServer(script, 18555)

    assert calls == [
        ("load", EXPECTED_FLAGS),
        ("run", str(script), False, [], EXPECTED_FLAGS),
    ]
    flags = calls[1][4]
    assert flags["server.address"] == "0.0.0.0"
    assert flags["server.port"] == 18555


def test_run_alone_when_load_config_options_does_not_exist(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.delattr(bootstrap, "load_config_options")
    monkeypatch.setattr(bootstrap, "run", lambda *args: calls.append(args))
    server_launcher.runStreamlitServer(Path("x.py"), 2000)
    assert calls == [("x.py", False, [], server_launcher.buildFlagOptions(2000))]


def test_the_installed_bootstrap_api_has_the_expected_shape():
    # A Streamlit upgrade that changes these breaks the adapter; fail here first.
    import inspect

    params = list(inspect.signature(bootstrap.run).parameters)
    assert params[:4] == ["main_script_path", "is_hello", "args", "flag_options"]
    assert list(inspect.signature(bootstrap.load_config_options).parameters) == ["flag_options"]


@pytest.mark.parametrize("key", list(EXPECTED_FLAGS))
def test_every_flag_is_a_real_streamlit_option(key):
    from streamlit import config

    assert key in config._config_options_template


# --- no outbound request (Critic TASK-009 B2). Part of the A8 internal-API guard: re-run it on
# every Streamlit upgrade, because it calls the private bootstrap._print_url.


def test_the_startup_banner_never_looks_up_ip_addresses(monkeypatch, loadedFlags):
    # get_external_ip is an HTTP GET to checkip.amazonaws.com; with our flags the banner path
    # must not call it (nor the internal lookup next to it).
    calls: list[str] = []

    def forbidden(name: str):
        def call(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"streamlit.net_util.{name} must not be called")

        return call

    monkeypatch.setattr(net_util, "get_external_ip", forbidden("get_external_ip"))
    monkeypatch.setattr(net_util, "get_internal_ip", forbidden("get_internal_ip"))
    bootstrap.load_config_options(flag_options=server_launcher.buildFlagOptions(18555))
    assert streamlitConfig.get_option("logger.hideWelcomeMessage") is True

    bootstrap._print_url(False)

    assert calls == []


def test_without_the_flag_the_banner_would_look_up_the_external_ip(monkeypatch, loadedFlags):
    # Control for the test above: the recorders do see the lookup when the flag is off, so
    # the empty result there is not vacuous. No traffic: both lookups are replaced.
    calls: list[str] = []
    monkeypatch.setattr(net_util, "get_external_ip", lambda: calls.append("external") or None)
    monkeypatch.setattr(net_util, "get_internal_ip", lambda: calls.append("internal") or None)
    flags = {**server_launcher.buildFlagOptions(18555), "logger.hideWelcomeMessage": False}
    bootstrap.load_config_options(flag_options=flags)

    bootstrap._print_url(False)

    assert calls == ["internal", "external"]
