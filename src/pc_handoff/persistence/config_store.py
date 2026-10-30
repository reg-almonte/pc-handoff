"""``config.json`` loading: read once at startup, with per-field fallback (ADR-005).

- **Missing**: written with the documented defaults (REQ AC-16).
- **Whole-file corrupt** (unparseable, root not an object, ``schemaVersion`` missing or not
  the integer 1): backed up as ``config.json.corrupt-<ts>`` and replaced with the defaults,
  with a startup notice naming the backup (REQ AC-18).
- **Otherwise**: each setting that is missing, of the wrong type (bool and float are not
  integers) or out of range falls back to its default, and unknown keys are ignored. Every
  case is a warning, logged at WARNING; the file is never rewritten, renamed or touched
  (REQ AC-15).

There is no reload API: the runtime holds the ``Config`` returned at startup (REQ AC-14).
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from pc_handoff.clock import Clock
from pc_handoff.persistence.atomic import (
    PersistenceError,
    atomicWriteBytes,
    backupCorrupt,
    readBytes,
)
from pc_handoff.persistence.notices import StartupNotice
from pc_handoff.persistence.paths import CONFIG_FILE_NAME, DataPaths
from pc_handoff.persistence.schemas import (
    SCHEMA_VERSION,
    UnparseableError,
    decodeJson,
    isStrictInt,
)

# (name, minimum, maximum, default), requirement section 5.
SETTINGS: tuple[tuple[str, int, int, int], ...] = (
    ("dashboardRefreshIntervalSeconds", 5, 300, 10),
    ("sessionPollIntervalSeconds", 2, 60, 5),
    ("dashboardPort", 1024, 65535, 8501),
)
SCHEMA_VERSION_KEY = "schemaVersion"

HOST_NOTIFICATION_METHODS: tuple[str, ...] = ("toast", "msg", "both", "none")
DEFAULT_ENABLE_HOST_NOTIFICATIONS = True
DEFAULT_HOST_NOTIFICATION_METHOD = "both"
DEFAULT_HOST_NOTIFICATION_TIMEOUT_SECONDS = 10
MIN_HOST_NOTIFICATION_TIMEOUT_SECONDS = 2
MAX_HOST_NOTIFICATION_TIMEOUT_SECONDS = 60

DEFAULT_CLAIM_GRACE_MINUTES = 60
MIN_CLAIM_GRACE_MINUTES = 1
MAX_CLAIM_GRACE_MINUTES = 1440

DEFAULT_ENABLE_TEAMS_NOTIFICATIONS = False
DEFAULT_TEAMS_WEBHOOK_URL = ""

OPTIONAL_KEYS: frozenset[str] = frozenset(
    {
        "enableHostNotifications",
        "hostNotificationMethod",
        "hostNotificationTimeoutSeconds",
        "claimGraceMinutes",
        "enableTeamsNotifications",
        "teamsWebhookUrl",
    }
)


@dataclass(frozen=True)
class Config:
    """The settings in effect for this process lifetime."""

    dashboardRefreshIntervalSeconds: int = 10
    sessionPollIntervalSeconds: int = 5
    dashboardPort: int = 8501
    schemaVersion: int = SCHEMA_VERSION
    enableHostNotifications: bool = DEFAULT_ENABLE_HOST_NOTIFICATIONS
    hostNotificationMethod: str = DEFAULT_HOST_NOTIFICATION_METHOD
    hostNotificationTimeoutSeconds: int = DEFAULT_HOST_NOTIFICATION_TIMEOUT_SECONDS
    claimGraceMinutes: int = DEFAULT_CLAIM_GRACE_MINUTES
    enableTeamsNotifications: bool = DEFAULT_ENABLE_TEAMS_NOTIFICATIONS
    teamsWebhookUrl: str = DEFAULT_TEAMS_WEBHOOK_URL


DEFAULT_CONFIG_DOCUMENT: dict[str, int] = {
    SCHEMA_VERSION_KEY: SCHEMA_VERSION,
    **{name: default for name, _, _, default in SETTINGS},
}
DEFAULT_CONFIG_BYTES = (json.dumps(DEFAULT_CONFIG_DOCUMENT, indent=2) + "\n").encode("utf-8")


@dataclass(frozen=True)
class ConfigLoadResult:
    """``created`` is True when ``config.json`` did not exist and was written with defaults."""

    config: Config
    warnings: list[str] = field(default_factory=list)
    notice: StartupNotice | None = None
    created: bool = False


def corruptionReason(doc: object) -> str | None:
    """Why a parsed document is whole-file corrupt (AC-18), or None if it is not."""
    if not isinstance(doc, dict):
        return "root is not a JSON object"
    if SCHEMA_VERSION_KEY not in doc:
        return "schemaVersion is missing"
    version = doc[SCHEMA_VERSION_KEY]
    if not isStrictInt(version) or version != SCHEMA_VERSION:
        return f"schemaVersion is {json.dumps(version)}, not 1"
    return None


def parseConfig(doc: dict[str, Any]) -> tuple[Config, list[str]]:
    """Apply per-field fallback to a document that is not whole-file corrupt (AC-15).

    Warnings come in ``SETTINGS`` order, then optional host notification settings, then one per
    unknown key in file order.
    """
    values: dict[str, Any] = {}
    warnings: list[str] = []
    for name, minimum, maximum, default in SETTINGS:
        value = doc.get(name)
        if name not in doc:
            warnings.append(f"{name}: missing; default {default} applied")
        elif not isStrictInt(value):
            warnings.append(f"{name}: wrong type (expected integer); default {default} applied")
        elif not minimum <= value <= maximum:
            warnings.append(
                f"{name}: {value} out of range {minimum}-{maximum}; default {default} applied"
            )
        else:
            values[name] = value

    if "enableHostNotifications" in doc:
        enabled = doc["enableHostNotifications"]
        if not isinstance(enabled, bool):
            warnings.append(
                "enableHostNotifications: wrong type (expected boolean); "
                f"default {DEFAULT_ENABLE_HOST_NOTIFICATIONS} applied"
            )
        else:
            values["enableHostNotifications"] = enabled

    if "hostNotificationMethod" in doc:
        method = doc["hostNotificationMethod"]
        if not isinstance(method, str):
            warnings.append(
                "hostNotificationMethod: wrong type (expected string); "
                f"default {DEFAULT_HOST_NOTIFICATION_METHOD!r} applied"
            )
        elif method not in HOST_NOTIFICATION_METHODS:
            allowed = ", ".join(repr(m) for m in HOST_NOTIFICATION_METHODS)
            warnings.append(
                f"hostNotificationMethod: {method!r} is not one of {allowed}; "
                f"default {DEFAULT_HOST_NOTIFICATION_METHOD!r} applied"
            )
        else:
            values["hostNotificationMethod"] = method

    if "hostNotificationTimeoutSeconds" in doc:
        timeout = doc["hostNotificationTimeoutSeconds"]
        if not isStrictInt(timeout):
            warnings.append(
                "hostNotificationTimeoutSeconds: wrong type (expected integer); "
                f"default {DEFAULT_HOST_NOTIFICATION_TIMEOUT_SECONDS} applied"
            )
        elif not (
            MIN_HOST_NOTIFICATION_TIMEOUT_SECONDS
            <= timeout
            <= MAX_HOST_NOTIFICATION_TIMEOUT_SECONDS
        ):
            warnings.append(
                f"hostNotificationTimeoutSeconds: {timeout} out of range "
                f"{MIN_HOST_NOTIFICATION_TIMEOUT_SECONDS}-"
                f"{MAX_HOST_NOTIFICATION_TIMEOUT_SECONDS}; "
                f"default {DEFAULT_HOST_NOTIFICATION_TIMEOUT_SECONDS} applied"
            )
        else:
            values["hostNotificationTimeoutSeconds"] = timeout

    if "claimGraceMinutes" in doc:
        grace = doc["claimGraceMinutes"]
        if not isStrictInt(grace):
            warnings.append(
                "claimGraceMinutes: wrong type (expected integer); "
                f"default {DEFAULT_CLAIM_GRACE_MINUTES} applied"
            )
        elif not (MIN_CLAIM_GRACE_MINUTES <= grace <= MAX_CLAIM_GRACE_MINUTES):
            warnings.append(
                f"claimGraceMinutes: {grace} out of range "
                f"{MIN_CLAIM_GRACE_MINUTES}-{MAX_CLAIM_GRACE_MINUTES}; "
                f"default {DEFAULT_CLAIM_GRACE_MINUTES} applied"
            )
        else:
            values["claimGraceMinutes"] = grace

    if "enableTeamsNotifications" in doc:
        teamsEnabled = doc["enableTeamsNotifications"]
        if not isinstance(teamsEnabled, bool):
            warnings.append(
                "enableTeamsNotifications: wrong type (expected boolean); "
                f"default {DEFAULT_ENABLE_TEAMS_NOTIFICATIONS} applied"
            )
        else:
            values["enableTeamsNotifications"] = teamsEnabled

    if "teamsWebhookUrl" in doc:
        url = doc["teamsWebhookUrl"]
        if not isinstance(url, str):
            warnings.append(
                "teamsWebhookUrl: wrong type (expected string); "
                f"default {DEFAULT_TEAMS_WEBHOOK_URL!r} applied"
            )
        elif url and not (url.startswith("https://") or url.startswith("http://")):
            warnings.append(
                "teamsWebhookUrl: invalid URL (expected http:// or https://); "
                f"default {DEFAULT_TEAMS_WEBHOOK_URL!r} applied"
            )
        else:
            values["teamsWebhookUrl"] = url.strip()

    known = {SCHEMA_VERSION_KEY, *(name for name, _, _, _ in SETTINGS), *OPTIONAL_KEYS}
    warnings.extend(f"{key}: unknown setting ignored" for key in doc if key not in known)
    return Config(**values), warnings


def loadConfig(paths: DataPaths, clock: Clock, logger: logging.Logger) -> ConfigLoadResult:
    """Load ``config.json`` once at startup. Never raises for file problems; the worst case is
    the defaults, with the reason logged."""
    try:
        # Inside the try: resolveInside raises ValueError when config.json resolves outside
        # the data directory (a link), which is treated like an unreadable file.
        raw = readBytes(paths.config)
    except (OSError, ValueError) as exc:
        logger.error("%s could not be read (%s); defaults applied", CONFIG_FILE_NAME, exc)
        return ConfigLoadResult(
            Config(), warnings=[f"{CONFIG_FILE_NAME}: could not be read; defaults applied"]
        )

    if raw is None:
        created = writeDefaults(paths, logger)
        if created:
            logger.info("%s was missing and has been created with defaults", CONFIG_FILE_NAME)
        return ConfigLoadResult(Config(), created=created)

    try:
        doc: object = decodeJson(raw)
        reason = corruptionReason(doc)
    except UnparseableError as exc:
        reason = f"cannot be parsed ({exc})"
    if reason is not None:
        return resetCorrupt(paths, clock, logger, reason)

    assert isinstance(doc, dict)  # corruptionReason returned None
    config, warnings = parseConfig(doc)
    for warning in warnings:
        logger.warning("%s: %s", CONFIG_FILE_NAME, warning)
    return ConfigLoadResult(config, warnings=warnings)


def resetCorrupt(
    paths: DataPaths, clock: Clock, logger: logging.Logger, reason: str
) -> ConfigLoadResult:
    """Back up a whole-file corrupt config, then write the defaults (backup before replace)."""
    try:
        backup = backupCorrupt(paths.config, clock)
    except OSError as exc:
        logger.error(
            "%s is corrupt (%s) and could not be backed up (%s); it is left unchanged and "
            "defaults are in use",
            CONFIG_FILE_NAME,
            reason,
            exc,
        )
        text = f"{CONFIG_FILE_NAME} is corrupt and could not be backed up; defaults are in use"
        return ConfigLoadResult(Config(), notice=StartupNotice(CONFIG_FILE_NAME, None, text))

    written = writeDefaults(paths, logger)
    outcome = "has been reset to defaults" if written else "was moved aside; defaults are in use"
    logger.warning(
        "%s was corrupt (%s) and %s (backup: %s)",
        CONFIG_FILE_NAME,
        reason,
        outcome,
        backup.name,
    )
    if written:
        text = f"{CONFIG_FILE_NAME} was corrupt and has been reset (backup: {backup.name})"
    else:
        text = (
            f"{CONFIG_FILE_NAME} was corrupt and has been moved to {backup.name}; "
            "defaults are in use"
        )
    return ConfigLoadResult(Config(), notice=StartupNotice(CONFIG_FILE_NAME, backup.name, text))


def writeDefaults(paths: DataPaths, logger: logging.Logger) -> bool:
    """Write the default ``config.json``; on failure log an ERROR and return False."""
    try:
        paths.dataDir.mkdir(parents=True, exist_ok=True)
        atomicWriteBytes(paths.config, DEFAULT_CONFIG_BYTES)
    except (OSError, PersistenceError) as exc:
        logger.error("%s could not be written with defaults: %s", CONFIG_FILE_NAME, exc)
        return False
    return True
