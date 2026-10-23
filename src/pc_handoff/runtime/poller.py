"""The single poller thread: a fixed-rate schedule that never overlaps polls (ADR-002).

Tick k is due at ``t0 + k * intervalSeconds`` on the monotonic clock; tick 0 runs at start.
At a due tick:

1. If the timed source is still busy with an enumeration abandoned by a timed-out poll, the
   tick is skipped and logged as ``poll skipped: previous enumeration still running``.
2. Otherwise ``runtime.pollOnce`` runs synchronously on this thread.
3. Every tick that became due while that poll ran is logged as
   ``poll skipped: previous poll still running``; the next tick is the first one due after
   the poll completed.

One thread runs every poll, so polls cannot overlap; a non-blocking guard lock around
``pollOnce`` asserts it. Each tick (run or skipped) gets a fresh traceId. The loop body is
wrapped so that no exception can end the thread (requirement Resilience).
"""

import logging
import math
import threading

from pc_handoff.app_logging import newTraceId, traceScope
from pc_handoff.clock import Clock
from pc_handoff.detection.timed_source import TimedSessionSource
from pc_handoff.runtime.app_runtime import AppRuntime

THREAD_NAME = "pc-handoff-poller"
STOP_TIMEOUT_SECONDS = 5.0
SKIPPED_BUSY_ENUMERATION = "poll skipped: previous enumeration still running"
SKIPPED_POLL_RUNNING = "poll skipped: previous poll still running"


class Poller:
    """Runs ``runtime.pollOnce`` every ``intervalSeconds`` on one daemon thread."""

    def __init__(
        self,
        runtime: AppRuntime,
        source: TimedSessionSource,
        intervalSeconds: float,
        clock: Clock,
        logger: logging.Logger,
        stopEvent: threading.Event | None = None,
    ) -> None:
        if intervalSeconds <= 0:
            raise ValueError(f"intervalSeconds must be positive, got {intervalSeconds}")
        self.runtime = runtime
        self.source = source
        self.intervalSeconds = intervalSeconds
        self.clock = clock
        self.logger = logger
        self.stopEvent = stopEvent or threading.Event()
        self.pollGuard = threading.Lock()
        self.pollCount = 0
        self.skippedCount = 0
        self.refusedCount = 0  # pollOnce attempts refused by the guard; always 0 by design
        self.thread = threading.Thread(target=self.run, name=THREAD_NAME, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self, timeout: float = STOP_TIMEOUT_SECONDS) -> bool:
        """Ask the thread to stop and wait up to ``timeout`` s; True when it has ended.

        A poll in progress is allowed to finish (its writes are atomic either way).
        """
        self.stopEvent.set()
        if self.thread.ident is not None:
            self.thread.join(timeout)
        stopped = not self.thread.is_alive()
        if not stopped:
            self.logger.warning("poller did not stop within %s s", timeout)
        return stopped

    def isAlive(self) -> bool:
        return self.thread.is_alive()

    # --- the loop -------------------------------------------------------------------------

    def run(self) -> None:
        start = self.clock.monotonic()
        tick = 0
        while not self.stopEvent.is_set():
            try:
                remaining = start + tick * self.intervalSeconds - self.clock.monotonic()
                if remaining > 0:
                    self.stopEvent.wait(remaining)
                    continue  # re-check stop and the due time (wait may wake early)
                self.runTick()
                tick = self.nextTick(start, tick, logSkipped=True)
            except Exception:
                self.logger.exception("poller loop error; polling continues")
                tick = self.nextTick(start, tick, logSkipped=False)

    def runTick(self) -> None:
        traceId = newTraceId()
        if self.source.isBusy():
            self.skip(SKIPPED_BUSY_ENUMERATION, traceId)
            return
        if not self.pollGuard.acquire(blocking=False):
            self.refusedCount += 1
            with traceScope(traceId):
                self.logger.error("poll refused: another poll is running")
            return
        try:
            self.pollCount += 1
            self.runtime.pollOnce(traceId)
        finally:
            self.pollGuard.release()

    def nextTick(self, start: float, tick: int, *, logSkipped: bool) -> int:
        """The first tick due after now; ticks that came due meanwhile are skipped."""
        elapsed = self.clock.monotonic() - start
        following = max(tick + 1, math.floor(elapsed / self.intervalSeconds) + 1)
        if logSkipped:
            for _ in range(tick + 1, following):
                self.skip(SKIPPED_POLL_RUNNING, newTraceId())
        return following

    def skip(self, message: str, traceId: str) -> None:
        self.skippedCount += 1
        with traceScope(traceId):
            self.logger.info(message)
