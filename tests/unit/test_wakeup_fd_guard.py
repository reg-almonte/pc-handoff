"""The signal wakeup-fd guard in ``tests/conftest.py`` (TASK-011; Critic TASK-011 BLOCKING-1, N5).

An ``AppTest`` run leaves the signal wakeup fd pointing at its asyncio loop's socket, which
is closed with the loop. The root-conftest guard must survive that state in setup and
teardown, whatever ran before it (for example ``tests/ui`` before ``tests/acceptance``).
"""

import signal
import socket
import threading

import pytest

from tests.conftest import wakeupFdGuard

pytestmark = pytest.mark.skipif(
    threading.current_thread() is not threading.main_thread(),
    reason="signal.set_wakeup_fd works on the main thread only",
)


def staleWakeupFd() -> int:
    """Install a closed socket's fd as the wakeup fd, as a finished AppTest loop leaves it."""
    sock = socket.socket()
    sock.setblocking(False)
    fd = sock.fileno()
    signal.set_wakeup_fd(fd)
    sock.close()
    return fd


@pytest.fixture
def cleanWakeupFd():
    yield
    signal.set_wakeup_fd(-1)


def test_a_stale_wakeup_fd_left_before_the_guard_is_dropped_not_reinstalled(cleanWakeupFd):
    staleWakeupFd()
    with wakeupFdGuard():  # setup must not raise OSError (WinError 6 / 10038)
        assert signal.set_wakeup_fd(-1) == -1
    assert signal.set_wakeup_fd(-1) == -1  # teardown restored "none", not the dead fd


def test_a_stale_wakeup_fd_left_by_the_test_itself_is_cleared_at_teardown(cleanWakeupFd):
    with wakeupFdGuard():
        staleWakeupFd()  # the test body (an AppTest run) leaves a dead fd
    assert signal.set_wakeup_fd(-1) == -1


def test_a_live_wakeup_fd_is_kept(cleanWakeupFd):
    sock = socket.socket()
    sock.setblocking(False)
    try:
        signal.set_wakeup_fd(sock.fileno())
        with wakeupFdGuard():
            signal.set_wakeup_fd(-1)
        assert signal.set_wakeup_fd(-1) == sock.fileno()  # a valid fd is restored as found
    finally:
        sock.close()
