"""Microsoft Teams / Power Automate webhook notifier using standard library (SD003 3.4)."""

import contextlib
import json
import logging
import urllib.request
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from pc_handoff.domain.reservations import (
    CLAIM_GRACE_SECONDS,
    QueueState,
    ReservationAuditEvent,
    ReservationEventType,
)
from pc_handoff.notifications.notifier import (
    HostNotification,
    formatEventNotification,
    sanitizeNotificationText,
)


def buildTeamsAdaptiveCard(title: str, body: str) -> dict[str, Any]:
    """Construct an Adaptive Card v1.4 payload compatible with Power Automate workflows."""
    safeTitle = sanitizeNotificationText(title)
    safeBody = sanitizeNotificationText(body)
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "version": "1.4",
                    "type": "AdaptiveCard",
                    "msteams": {"width": "Full"},
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": safeTitle,
                            "weight": "Bolder",
                            "size": "Medium",
                            "color": "Accent",
                        },
                        {
                            "type": "TextBlock",
                            "text": safeBody,
                            "wrap": True,
                        },
                    ],
                },
            }
        ],
    }


class TeamsNotifier:
    """Dispatches asynchronous webhooks to Microsoft Teams via Power Automate."""

    def __init__(
        self,
        webhookUrl: str,
        logger: logging.Logger,
        *,
        enabled: bool = True,
        timeoutSeconds: int = 10,
        claimGraceSeconds: int = CLAIM_GRACE_SECONDS,
        sender: Callable[[str, bytes, float], Any] | None = None,
        asyncDispatch: bool = True,
    ) -> None:
        self.webhookUrl = webhookUrl.strip()
        self.logger = logger
        self.enabled = enabled and bool(self.webhookUrl)
        self.timeoutSeconds = timeoutSeconds
        self.claimGraceSeconds = claimGraceSeconds
        self._sender = sender or self._defaultSender
        self._asyncDispatch = asyncDispatch
        self._executor: ThreadPoolExecutor | None = (
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="pc-handoff-teams")
            if asyncDispatch and self.enabled
            else None
        )
        self._futures: list[Future[None]] = []

    @staticmethod
    def _defaultSender(url: str, payloadBytes: bytes, timeout: float) -> None:
        req = urllib.request.Request(
            url,
            data=payloadBytes,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            _ = response.read()

    def notify(self, notification: HostNotification) -> None:
        """Schedule or execute Teams card delivery; never raises."""
        if not self.enabled:
            return
        if self._executor is not None:
            try:
                future = self._executor.submit(
                    self._dispatchSync,
                    notification.title,
                    notification.body,
                )
                self._futures = [f for f in self._futures if not f.done()]
                self._futures.append(future)
            except RuntimeError as exc:
                self.logger.warning(
                    "teams notification skipped because executor is shut down",
                    extra={"detail": str(exc)},
                )
        else:
            self._dispatchSync(notification.title, notification.body)

    def notifyEvents(
        self,
        events: Sequence[ReservationAuditEvent],
        state: QueueState | None = None,
    ) -> None:
        """Format and dispatch notifications for eligible milestones (QUEUED and READY)."""
        if not self.enabled:
            return
        for event in events:
            if event.eventType in (
                ReservationEventType.RESERVATION_QUEUED,
                ReservationEventType.HANDOFF_INITIATED,
            ):
                notification = formatEventNotification(
                    event,
                    state,
                    claimGraceSeconds=self.claimGraceSeconds,
                )
                if notification is not None:
                    self.notify(notification)

    def _dispatchSync(self, title: str, body: str) -> None:
        card = buildTeamsAdaptiveCard(title, body)
        data = json.dumps(card).encode("utf-8")
        try:
            self._sender(self.webhookUrl, data, float(self.timeoutSeconds))
            self.logger.info(
                "teams notification dispatched successfully",
                extra={"detail": {"title": title}},
            )
        except Exception as exc:
            self.logger.warning(
                "teams notification dispatch failed",
                extra={
                    "detail": {
                        "url": self.webhookUrl,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                },
            )

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
