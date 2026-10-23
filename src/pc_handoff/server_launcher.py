"""Streamlit server bootstrap adapter (ADR-002): the only use of Streamlit's internal API.

``streamlit.web.bootstrap`` is semi-internal and has changed between minor versions; the
pin ``streamlit>=1.40,<2`` (ADR-006) and the server smoke test guard this module. It is
omitted from coverage: no decision logic lives here.
"""

from pathlib import Path
from typing import Any

BIND_ALL_INTERFACES = "0.0.0.0"  # REQ AC-13: reachable from the LAN


def buildFlagOptions(port: int) -> dict[str, Any]:
    """Streamlit config options for the in-process server.

    ``fileWatcherType = "none"`` keeps Streamlit from reloading modules, so the runtime
    registry stays the one the launcher filled. ``logger.hideWelcomeMessage = True`` skips the
    console URL banner: with a headless server on the wildcard address, that banner looks up
    the external IP with an HTTP request to checkip.amazonaws.com (requirement System
    Boundary: no external network service; Critic TASK-009 B2). The launcher logs the port
    to ``app.log`` instead. ``client.toolbarMode = "viewer"`` hides the Deploy button and the
    developer options from every LAN viewer (Critic TASK-010 N2a).
    """
    return {
        "server.port": port,
        "server.address": BIND_ALL_INTERFACES,
        "server.headless": True,
        "server.fileWatcherType": "none",
        "server.runOnSave": False,
        "browser.gatherUsageStats": False,
        "logger.hideWelcomeMessage": True,
        "client.toolbarMode": "viewer",
    }


def runStreamlitServer(scriptPath: str | Path, port: int) -> None:
    """Serve ``scriptPath`` on ``0.0.0.0:port``; blocks until the server stops.

    Must run on the main thread: ``bootstrap.run`` installs the SIGINT/SIGTERM/SIGBREAK
    handlers that stop the server.
    """
    from streamlit.web import bootstrap

    flagOptions = buildFlagOptions(port)
    # bootstrap.run does not apply flag options itself; the CLI loads them first.
    loadConfigOptions = getattr(bootstrap, "load_config_options", None)
    if loadConfigOptions is not None:
        loadConfigOptions(flag_options=flagOptions)
    bootstrap.run(str(scriptPath), False, [], flagOptions)
