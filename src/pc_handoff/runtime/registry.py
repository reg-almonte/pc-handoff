"""Process-wide registry of the running ``AppRuntime`` (ADR-002).

The launcher registers the runtime before it starts the Streamlit server. The dashboard
script runs in the same interpreter and finds it here through ``sys.modules``; with
``server.fileWatcherType = "none"`` Streamlit never reloads this module, so the reference
is stable for the life of the process. A plain ``streamlit run`` finds nothing registered.
"""

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from pc_handoff.runtime.app_runtime import AppRuntime

registryLock = threading.Lock()
currentRuntime: "AppRuntime | None" = None


def registerRuntime(runtime: "AppRuntime") -> None:
    """Register the process's runtime. A different second runtime is refused: one runtime,
    hence one poller, per process."""
    global currentRuntime
    with registryLock:
        if currentRuntime is not None and currentRuntime is not runtime:
            raise RuntimeError("a PC Handoff runtime is already registered in this process")
        currentRuntime = runtime


def getRuntime() -> "AppRuntime | None":
    """The registered runtime, or None when the app was not started by the launcher."""
    return currentRuntime


def clearRuntime() -> None:
    """Forget the registered runtime (launcher shutdown and tests)."""
    global currentRuntime
    with registryLock:
        currentRuntime = None
