"""Helper process for the multi-process instance-lock tests (TASK-007).

Usage: ``python lock_holder_script.py <dataDir> [maxSeconds]``

Acquires ``InstanceLock(dataDir)``, prints ``READY <pid>`` and then sleeps until it is killed. If
the lock is already held it prints ``HELD`` and exits with ``EXIT_ALREADY_RUNNING``. As a
safety net against stray processes it exits by itself after ``maxSeconds`` (default 120).
"""

import os
import sys
import time

from pc_handoff.clock import SystemClock
from pc_handoff.persistence.instance_lock import (
    EXIT_ALREADY_RUNNING,
    InstanceLock,
    LockHeldError,
)


def main(argv: list[str]) -> int:
    dataDir = argv[1]
    maxSeconds = float(argv[2]) if len(argv) > 2 else 120.0
    lock = InstanceLock(dataDir, SystemClock())
    try:
        lock.acquire()
    except LockHeldError:
        print("HELD", flush=True)
        return EXIT_ALREADY_RUNNING
    print(f"READY {os.getpid()}", flush=True)
    deadline = time.monotonic() + maxSeconds
    while time.monotonic() < deadline:
        time.sleep(0.1)
    lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
