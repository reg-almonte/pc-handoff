"""Enumeration with a hard timeout and a busy signal for no-overlap scheduling (ADR-001/002).

ctypes calls cannot be cancelled, so each enumeration runs in a daemon worker thread and the
caller waits at most ``timeoutSeconds``. A worker that misses the deadline is abandoned and
kept: ``isBusy()`` stays true until it finishes, so the poller never starts an overlapping
enumeration and at most one worker can be left behind.

The worker runs in a copy of the caller's ``contextvars`` context, so every log line it
emits (including the provider's) carries the poll's traceId (REQ AC-11).
"""

import contextvars
import logging
import threading
import time

from pc_handoff.detection.provider import OsSession, SessionEnumerationError, SessionProvider

ENUMERATION_TIMEOUT_SECONDS = 3

logger = logging.getLogger(__name__)


class WorkerOutcome:
    """Result hand-over between one worker thread and its caller."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.done = False
        self.abandoned = False
        self.sessions: list[OsSession] = []
        self.error: BaseException | None = None


class TimedSessionSource:
    """Run ``provider.enumerateSessions()`` with a timeout in a worker thread."""

    def __init__(
        self, provider: SessionProvider, timeoutSeconds: float = ENUMERATION_TIMEOUT_SECONDS
    ) -> None:
        self.provider = provider
        self.timeoutSeconds = timeoutSeconds
        self.abandonedWorker: threading.Thread | None = None

    def isBusy(self) -> bool:
        """True while a worker abandoned by a timed-out enumeration is still running."""
        worker = self.abandonedWorker
        if worker is None:
            return False
        if worker.is_alive():
            return True
        self.abandonedWorker = None
        return False

    def enumerate(self) -> list[OsSession]:
        """Return the provider's snapshot or raise ``SessionEnumerationError``.

        Raises ``RuntimeError`` if called while ``isBusy()`` (the poller checks first).
        """
        if self.isBusy():
            raise RuntimeError("previous session enumeration is still running")
        outcome = WorkerOutcome()
        context = contextvars.copy_context()
        worker = threading.Thread(
            target=context.run,
            args=(self.runWorker, outcome),
            name="pc-handoff-session-enumeration",
            daemon=True,
        )
        worker.start()
        worker.join(self.timeoutSeconds)
        with outcome.lock:
            finished = outcome.done
            if not finished:
                outcome.abandoned = True
        if not finished:
            self.abandonedWorker = worker
            raise SessionEnumerationError(
                "timeout", f"session enumeration did not return within {self.timeoutSeconds} s"
            )
        if outcome.error is None:
            return outcome.sessions
        if isinstance(outcome.error, SessionEnumerationError):
            raise outcome.error
        raise SessionEnumerationError(
            "error", f"{type(outcome.error).__name__}: {outcome.error}"
        ) from outcome.error

    def runWorker(self, outcome: WorkerOutcome) -> None:
        started = time.monotonic()
        sessions: list[OsSession] = []
        error: BaseException | None = None
        try:
            sessions = self.provider.enumerateSessions()
        except BaseException as exc:  # handed to the caller, never lost in the thread
            error = exc
        with outcome.lock:
            outcome.sessions = sessions
            outcome.error = error
            outcome.done = True
            abandoned = outcome.abandoned
        if abandoned:
            logger.info(
                "abandoned session enumeration finished after timeout",
                extra={
                    "detail": {
                        "elapsedSeconds": round(time.monotonic() - started, 3),
                        "outcome": "ok" if error is None else f"{type(error).__name__}: {error}",
                    }
                },
            )
