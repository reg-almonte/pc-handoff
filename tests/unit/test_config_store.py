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
    assert HOST_NOTIFICATION_METHODS == ("toast", "msg", "both", "none")
    assert cfg.claimGraceMinutes == 60
    assert cfg.enableTeamsNotifications is False
    assert cfg.teamsWebhookUrl == ""


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("method", ["toast", "msg", "both", "none"])
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
    assert cfg.hostNotificationMethod == "both"
    assert cfg.hostNotificationTimeoutSeconds == 10
    assert warnings == [
        "enableHostNotifications: wrong type (expected boolean); default True applied",
        "hostNotificationMethod: wrong type (expected string); default 'both' applied",
        "hostNotificationTimeoutSeconds: wrong type (expected integer); default 10 applied",
    ]

    cfg2, warnings2 = parseConfig(
        baseDoc(
            hostNotificationMethod="webhook",
            hostNotificationTimeoutSeconds=1,
        )
    )
    assert cfg2.hostNotificationMethod == "both"
    assert cfg2.hostNotificationTimeoutSeconds == 10
    assert warnings2 == [
        "hostNotificationMethod: 'webhook' is not one of 'toast', 'msg', 'both', 'none'; "
        "default 'both' applied",
        "hostNotificationTimeoutSeconds: 1 out of range 2-60; default 10 applied",
    ]

    cfg3, warnings3 = parseConfig(baseDoc(hostNotificationTimeoutSeconds=61))
    assert cfg3.hostNotificationTimeoutSeconds == 10
    assert warnings3 == [
        "hostNotificationTimeoutSeconds: 61 out of range 2-60; default 10 applied",
    ]


def test_parse_config_claim_grace_and_teams_settings() -> None:
    cfg, warnings = parseConfig(
        baseDoc(
            claimGraceMinutes=120,
            enableTeamsNotifications=True,
            teamsWebhookUrl="https://prod.westus.logic.azure.com/workflows/123",
        )
    )
    assert warnings == []
    assert cfg.claimGraceMinutes == 120
    assert cfg.enableTeamsNotifications is True
    assert cfg.teamsWebhookUrl == "https://prod.westus.logic.azure.com/workflows/123"

    cfgBad, badWarnings = parseConfig(
        baseDoc(
            claimGraceMinutes="60",
            enableTeamsNotifications="true",
            teamsWebhookUrl=999,
        )
    )
    assert cfgBad.claimGraceMinutes == 60
    assert cfgBad.enableTeamsNotifications is False
    assert cfgBad.teamsWebhookUrl == ""
    assert badWarnings == [
        "claimGraceMinutes: wrong type (expected integer); default 60 applied",
        "enableTeamsNotifications: wrong type (expected boolean); default False applied",
        "teamsWebhookUrl: wrong type (expected string); default '' applied",
    ]

    cfgRange, rangeWarnings = parseConfig(
        baseDoc(
            claimGraceMinutes=0,
            teamsWebhookUrl="ftp://invalid.com",
        )
    )
    assert cfgRange.claimGraceMinutes == 60
    assert rangeWarnings == [
        "claimGraceMinutes: 0 out of range 1-1440; default 60 applied",
        "teamsWebhookUrl: invalid URL (expected http:// or https://); default '' applied",
    ]
