"""Application launcher.

Placeholder until TASK-009, which implements lock acquisition, config and data loading,
the poller thread and the Streamlit server start-up.
"""

from collections.abc import Sequence

NOT_IMPLEMENTED_MESSAGE = "pc_handoff: not yet implemented (launcher arrives in TASK-009)"


def main(argv: Sequence[str] | None = None) -> int:
    """Run the application. Currently a stub that reports it is not implemented."""
    print(NOT_IMPLEMENTED_MESSAGE)
    return 0
