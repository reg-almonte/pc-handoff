"""Unit tests for notification configuration settings in ``config_store`` (TASK-018)."""

from typing import Any

import pytest

from pc_handoff.persistence.config_store import (
    DEFAULT_ENABLE_HOST_NOTIFICATIONS,
    DEFAULT_HOST_NOTIFICATION_METHOD,
    DEFAULT_HOST_NOTIFICATION_TIMEOUT_SECONDS,
    HOST_NOTIFICATION_METHODS,
    MAX_HOST_NOTIFICATION_TIMEOUT_SECONDS,
    MIN_HOST_NOTIFICATION_TIMEOUT_SECONDS,
    Config,
    parseConfig,
)


def baseDoc(**extra: Any) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "dashboardRefreshIntervalSeconds": 10,
        "sessionPollIntervalSeconds": 5,
        "dashboardPort": 8501,
        **extra,
    }


def test_config_default_notification_fields() -> None:
    cfg = Config()
    assert cfg.enableHostNotifications is DEFAULT_ENABLE_HOST_NOTIFICATIONS
    assert cfg.hostNotificationMethod == DEFAULT_HOST_NOTIFICATION_METHOD
    assert cfg.hostNotificationTimeoutSeconds == DEFAULT_HOST_NOTIFICATION_TIMEOUT_SECONDS
    assert HOST_NOTIFICATION_METHODS == ("toast", "msg", "none")


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("method", ["toast", "msg", "none"])
@pytest.mark.parametrize(
    "timeout",
    [
        MIN_HOST_NOTIFICATION_TIMEOUT_SECONDS,
        10,
        MAX_HOST_NOTIFICATION_TIMEOUT_SECONDS,
    ],
)
def test_parse_config_accepts_valid_notification_settings(
    enabled: bool,
    method: str,
    timeout: int,
) -> None:
    cfg, warnings = parseConfig(
        baseDoc(
            enableHostNotifications=enabled,
            hostNotificationMethod=method,
            hostNotificationTimeoutSeconds=timeout,
        )
    )
    assert warnings == []
    assert cfg.enableHostNotifications is enabled
    assert cfg.hostNotificationMethod == method
    assert cfg.hostNotificationTimeoutSeconds == timeout


def test_parse_config_notification_wrong_types_and_bounds_fall_back_with_warnings() -> None:
    cfg, warnings = parseConfig(
        baseDoc(
            enableHostNotifications="yes",
            hostNotificationMethod=123,
            hostNotificationTimeoutSeconds=True,
        )
    )
    assert cfg.enableHostNotifications is True
    assert cfg.hostNotificationMethod == "toast"
    assert cfg.hostNotificationTimeoutSeconds == 10
    assert warnings == [
        "enableHostNotifications: wrong type (expected boolean); default True applied",
        "hostNotificationMethod: wrong type (expected string); default 'toast' applied",
        "hostNotificationTimeoutSeconds: wrong type (expected integer); default 10 applied",
    ]

    cfg2, warnings2 = parseConfig(
        baseDoc(
            hostNotificationMethod="webhook",
            hostNotificationTimeoutSeconds=1,
        )
    )
    assert cfg2.hostNotificationMethod == "toast"
    assert cfg2.hostNotificationTimeoutSeconds == 10
    assert warnings2 == [
        "hostNotificationMethod: 'webhook' is not one of 'toast', 'msg', 'none'; "
        "default 'toast' applied",
        "hostNotificationTimeoutSeconds: 1 out of range 2-60; default 10 applied",
    ]

    cfg3, warnings3 = parseConfig(baseDoc(hostNotificationTimeoutSeconds=61))
    assert cfg3.hostNotificationTimeoutSeconds == 10
    assert warnings3 == [
        "hostNotificationTimeoutSeconds: 61 out of range 2-60; default 10 applied",
    ]
