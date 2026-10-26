"""Zero-dependency Windows host notification implementation (SD002 section 4.2 & 4.3).

Supports two built-in Windows notification channels via subprocess:
- ``"toast"``: PowerShell WinRT ``ToastNotificationManager`` desktop banner.
- ``"msg"``: Terminal Services ``msg.exe *`` broadcast modal.

All notifications execute asynchronously on a single background worker thread (unless
``asyncDispatch=False`` is passed for synchronous testing) and contain all subprocess and OS
errors so notification failures never disrupt the poller or UI threads.
"""

import contextlib
import logging
import subprocess
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any
from xml.sax.saxutils import escape as xmlEscape

from pc_handoff.domain.reservations import (
    CLAIM_GRACE_SECONDS,
    QueueState,
    ReservationAuditEvent,
)
from pc_handoff.notifications.notifier import (
    HostNotification,
    Notifier,
    NullNotifier,
    formatEventNotification,
    sanitizeNotificationText,
)
from pc_handoff.persistence.config_store import (
    DEFAULT_HOST_NOTIFICATION_METHOD,
    DEFAULT_HOST_NOTIFICATION_TIMEOUT_SECONDS,
    Config,
)

APP_ID = "PC.Handoff"


def _psSingleQuote(text: str) -> str:
    """Escape a string for inclusion inside a PowerShell single-quoted literal."""
    return text.replace("'", "''")


def buildToastCommand(title: str, body: str) -> list[str]:
    """Build the PowerShell command line for a native WinRT Toast notification."""
    safeTitle = _psSingleQuote(xmlEscape(sanitizeNotificationText(title)))
    safeBody = _psSingleQuote(xmlEscape(sanitizeNotificationText(body)))
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        "ContentType = WindowsRuntime] > $null; "
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, "
        "ContentType = WindowsRuntime] > $null; "
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument; "
        f'$xml.LoadXml(\'<toast><visual><binding template="ToastGeneric">'
        f"<text>{safeTitle}</text><text>{safeBody}</text>"
        f"</binding></visual></toast>'); "
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml); "
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{APP_ID}')"
        ".Show($toast)"
    )
    return [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        script,
    ]


def buildMsgCommand(title: str, body: str, timeoutSeconds: int) -> list[str]:
    """Build the ``msg.exe`` command line for a Terminal Services session broadcast."""
    safeTitle = sanitizeNotificationText(title)
    safeBody = sanitizeNotificationText(body)
    return [
        "msg.exe",
        "*",
        f"/time:{max(1, int(timeoutSeconds))}",
        f"{safeTitle} - {safeBody}",
    ]


class WindowsNotifier:
    """Dispatches host notifications on Windows with full failure containment."""

    def __init__(
        self,
        logger: logging.Logger,
        *,
        enabled: bool = True,
        method: str = DEFAULT_HOST_NOTIFICATION_METHOD,
        timeoutSeconds: int = DEFAULT_HOST_NOTIFICATION_TIMEOUT_SECONDS,
        claimGraceSeconds: int = CLAIM_GRACE_SECONDS,
        runner: Callable[..., Any] = subprocess.run,
        asyncDispatch: bool = True,
    ) -> None:
        self.logger = logger
        self.enabled = enabled
        self.method = method
        self.timeoutSeconds = timeoutSeconds
        self.claimGraceSeconds = claimGraceSeconds
        self._runner = runner
        self._asyncDispatch = asyncDispatch
        self._executor: ThreadPoolExecutor | None = (
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="pc-handoff-notifier")
            if asyncDispatch and enabled and method != "none"
            else None
        )
        self._futures: list[Future[None]] = []

    def notify(self, notification: HostNotification) -> None:
        """Schedule or execute ``notification``; never raises."""
        if not self.enabled or self.method == "none":
            return
        if self._executor is not None:
            try:
                future = self._executor.submit(self._dispatchSync, notification)
                self._futures = [f for f in self._futures if not f.done()]
                self._futures.append(future)
            except RuntimeError as exc:
                self.logger.warning(
                    "host notification skipped because executor is shut down",
                    extra={"detail": str(exc)},
                )
        else:
            self._dispatchSync(notification)

    def notifyEvents(
        self,
        events: Sequence[ReservationAuditEvent],
        state: QueueState | None = None,
    ) -> None:
        """Format and dispatch notifications for any eligible events in ``events``."""
        if not self.enabled or self.method == "none":
            return
        for event in events:
            notification = formatEventNotification(
                event,
                state,
                claimGraceSeconds=self.claimGraceSeconds,
            )
            if notification is not None:
                self.notify(notification)

    def flush(self, timeout: float = 5.0) -> None:
        """Wait for any currently queued background notifications to finish."""
        for future in list(self._futures):
            with contextlib.suppress(Exception):
                future.result(timeout=timeout)
        self._futures.clear()

    def close(self) -> None:
        """Shut down the background worker thread if one was created."""
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

    def _buildCommand(self, notification: HostNotification) -> list[str] | None:
        if self.method == "toast":
            return buildToastCommand(notification.title, notification.body)
        if self.method == "msg":
            return buildMsgCommand(
                notification.title,
                notification.body,
                self.timeoutSeconds,
            )
        return None

    def _dispatchSync(self, notification: HostNotification) -> None:
        command = self._buildCommand(notification)
        if command is None:
            return
        try:
            completed = self._runner(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeoutSeconds,
                check=False,
            )
            returncode = getattr(completed, "returncode", 0)
            if returncode != 0:
                stderr = getattr(completed, "stderr", "") or ""
                self.logger.warning(
                    "host notification command exited with non-zero status",
                    extra={
                        "detail": {
                            "method": self.method,
                            "returncode": returncode,
                            "stderr": stderr.strip(),
                        }
                    },
                )
        except Exception as exc:
            self.logger.warning(
                "host notification delivery failed",
                extra={
                    "detail": {
                        "method": self.method,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                },
            )


def createNotifier(
    config: Config,
    logger: logging.Logger,
    *,
    claimGraceSeconds: int = CLAIM_GRACE_SECONDS,
    runner: Callable[..., Any] = subprocess.run,
    asyncDispatch: bool = True,
) -> Notifier:
    """Create a configured ``Notifier`` from ``Config``."""
    if not config.enableHostNotifications or config.hostNotificationMethod == "none":
        return NullNotifier()
    return WindowsNotifier(
        logger=logger,
        enabled=config.enableHostNotifications,
        method=config.hostNotificationMethod,
        timeoutSeconds=config.hostNotificationTimeoutSeconds,
        claimGraceSeconds=claimGraceSeconds,
        runner=runner,
        asyncDispatch=asyncDispatch,
    )
