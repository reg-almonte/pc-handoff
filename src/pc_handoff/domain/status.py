"""PC status derivation (requirement section 4)."""

from collections.abc import Iterable

from pc_handoff.domain.models import PcStatus, SessionState, TrackedSession


def deriveStatus(sessions: Iterable[TrackedSession]) -> PcStatus:
    """IN_USE if any user session is Active, otherwise AVAILABLE.

    Only called after a successful poll; a failed poll is reported as UNKNOWN by the caller.
    Disconnected sessions never make the PC In Use (A1).
    """
    if any(s.state is SessionState.ACTIVE for s in sessions):
        return PcStatus.IN_USE
    return PcStatus.AVAILABLE
