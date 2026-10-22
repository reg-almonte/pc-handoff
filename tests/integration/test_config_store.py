"""Integration tests for config.json loading on a real temp directory (TASK-005 AC-3 to AC-6).

Each test configures the real JSON-lines ``app.log`` so the WARNING lines the requirement asks
for (REQ AC-15) are asserted as written to disk.
"""

import dataclasses
import hashlib
import json
import logging
import re
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from pc_handoff.app_logging import AppLogFileHandler, configureAppLogging
from pc_handoff.persistence import config_store
from pc_handoff.persistence.atomic import PersistenceError
from pc_handoff.persistence.config_store import (
    DEFAULT_CONFIG_BYTES,
    SETTINGS,
    Config,
    ConfigLoadResult,
    loadConfig,
    parseConfig,
)
from pc_handoff.persistence.notices import StartupNotice
from pc_handoff.persistence.paths import DataPaths
from pc_handoff.persistence.schemas import decodeJson
from tests.fakes import FakeClock

JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 10, 21, 9, 15, 2, tzinfo=JST)
BACKUP_NAME = "config.json.corrupt-20261021T091502"
DEFAULTS = Config(
    dashboardRefreshIntervalSeconds=10, sessionPollIntervalSeconds=5, dashboardPort=8501
)
AC3_BYTES = (
    b'{"schemaVersion": 1, "dashboardRefreshIntervalSeconds": 2, '
    b'"sessionPollIntervalSeconds": "fast", "dashboardPort": 80, "foo": true}'
)


@dataclasses.dataclass
class Env:
    paths: DataPaths
    clock: FakeClock
    logger: logging.Logger

    @property
    def configPath(self) -> Path:
        return self.paths.config

    def load(self) -> ConfigLoadResult:
        return loadConfig(self.paths, self.clock, self.logger)

    def appLog(self) -> list[dict[str, Any]]:
        for handler in logging.getLogger("pc_handoff").handlers:
            handler.flush()
        if not self.paths.appLog.exists():
            return []
        return [json.loads(line) for line in self.paths.appLog.read_text("utf-8").splitlines()]

    def warningMessages(self) -> list[str]:
        return [line["message"] for line in self.appLog() if line["level"] == "WARNING"]

    def corruptFiles(self) -> list[Path]:
        return sorted(self.paths.dataDir.glob("config.json.corrupt-*"))


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    dataDir = tmp_path / "data"
    dataDir.mkdir()
    configureAppLogging(dataDir)
    yield Env(
        DataPaths(dataDir), FakeClock(NOW), logging.getLogger("pc_handoff.persistence.config")
    )
    logger = logging.getLogger("pc_handoff")
    for handler in list(logger.handlers):
        if isinstance(handler, AppLogFileHandler):
            logger.removeHandler(handler)
            handler.close()


def fingerprint(path: Path) -> tuple[str, int, int]:
    stat = path.stat()
    return hashlib.sha256(path.read_bytes()).hexdigest(), stat.st_size, stat.st_mtime_ns


# --- AC-3 / REQ AC-15 ----------------------------------------------------------------------


class TestPerFieldFallback:
    def test_ac3_invalid_values_fall_back_and_the_file_is_untouched(self, env: Env) -> None:
        env.configPath.write_bytes(AC3_BYTES)
        before = fingerprint(env.configPath)

        result = env.load()

        assert result.config == DEFAULTS
        assert result.warnings == [
            "dashboardRefreshIntervalSeconds: 2 out of range 5-300; default 10 applied",
            "sessionPollIntervalSeconds: wrong type (expected integer); default 5 applied",
            "dashboardPort: 80 out of range 1024-65535; default 8501 applied",
            "foo: unknown setting ignored",
        ]
        assert result.notice is None
        assert result.created is False
        logged = env.warningMessages()
        for warning in result.warnings:
            assert any(warning in message for message in logged), warning
        assert fingerprint(env.configPath) == before
        assert env.corruptFiles() == []
        assert sorted(p.name for p in env.paths.dataDir.iterdir()) == ["config.json", "logs"]

    def test_ac4_bool_and_float_are_the_wrong_type(self, env: Env) -> None:
        env.configPath.write_text(
            '{"schemaVersion": 1, "dashboardRefreshIntervalSeconds": 10.0, '
            '"sessionPollIntervalSeconds": true, "dashboardPort": 8501}',
            encoding="utf-8",
        )
        result = env.load()
        assert result.config == DEFAULTS
        assert result.warnings == [
            "dashboardRefreshIntervalSeconds: wrong type (expected integer); default 10 applied",
            "sessionPollIntervalSeconds: wrong type (expected integer); default 5 applied",
        ]

    @pytest.mark.parametrize("value", [None, "5", [5], {"v": 5}])
    def test_other_wrong_types(self, env: Env, value: Any) -> None:
        env.configPath.write_text(
            json.dumps({"schemaVersion": 1, "sessionPollIntervalSeconds": value}), "utf-8"
        )
        result = env.load()
        assert result.config.sessionPollIntervalSeconds == 5
        assert "sessionPollIntervalSeconds: wrong type (expected integer); default 5 applied" in (
            result.warnings
        )

    def test_valid_values_are_used_and_produce_no_warnings(self, env: Env) -> None:
        env.configPath.write_text(
            '{"schemaVersion": 1, "dashboardRefreshIntervalSeconds": 30, '
            '"sessionPollIntervalSeconds": 2, "dashboardPort": 65535}',
            encoding="utf-8",
        )
        before = fingerprint(env.configPath)
        result = env.load()
        assert result.config == Config(30, 2, 65535)
        assert result.warnings == []
        assert result.notice is None
        assert fingerprint(env.configPath) == before
        assert env.warningMessages() == []


@pytest.mark.parametrize(("name", "minimum", "maximum", "default"), SETTINGS)
class TestRangeBoundaries:
    def test_bounds_are_inclusive(
        self, name: str, minimum: int, maximum: int, default: int
    ) -> None:
        for value in (minimum, maximum):
            config, warnings = parseConfig({"schemaVersion": 1, name: value})
            assert getattr(config, name) == value
            assert not [w for w in warnings if w.startswith(f"{name}:")]

    def test_one_past_each_bound_is_out_of_range(
        self, name: str, minimum: int, maximum: int, default: int
    ) -> None:
        for value in (minimum - 1, maximum + 1):
            config, warnings = parseConfig({"schemaVersion": 1, name: value})
            assert getattr(config, name) == default
            assert (
                f"{name}: {value} out of range {minimum}-{maximum}; default {default} applied"
                in warnings
            )


def test_settings_table_matches_requirement_section_5() -> None:
    assert SETTINGS == (
        ("dashboardRefreshIntervalSeconds", 5, 300, 10),
        ("sessionPollIntervalSeconds", 2, 60, 5),
        ("dashboardPort", 1024, 65535, 8501),
    )
    assert Config() == DEFAULTS
    assert Config().schemaVersion == 1


def test_config_is_immutable() -> None:
    """REQ AC-14: the Config held by the runtime cannot be changed after startup."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        Config().dashboardPort = 9000  # type: ignore[misc]


# --- AC-5 / REQ AC-16 ----------------------------------------------------------------------


class TestMissingAndPartial:
    def test_missing_config_is_created_with_the_documented_defaults(self, env: Env) -> None:
        result = env.load()

        assert result.created is True
        assert result.config == DEFAULTS
        assert result.warnings == []
        assert result.notice is None
        raw = env.configPath.read_bytes()
        assert raw == DEFAULT_CONFIG_BYTES
        assert not raw.startswith(b"\xef\xbb\xbf")  # UTF-8 without BOM (ADR-005)
        assert json.loads(raw) == {
            "schemaVersion": 1,
            "dashboardRefreshIntervalSeconds": 10,
            "sessionPollIntervalSeconds": 5,
            "dashboardPort": 8501,
        }
        assert raw.decode("utf-8").startswith('{\n  "schemaVersion": 1,')  # indented 2
        # The created file passes validation: it parses with no warnings at all.
        assert parseConfig(decodeJson(raw)) == (DEFAULTS, [])
        assert env.warningMessages() == []
        assert env.corruptFiles() == []

    def test_created_file_reloads_cleanly(self, env: Env) -> None:
        env.load()
        second = env.load()
        assert (second.created, second.warnings, second.notice) == (False, [], None)

    def test_missing_data_directory_is_created(self, tmp_path: Path) -> None:
        paths = DataPaths(tmp_path / "new" / "data")
        result = loadConfig(paths, FakeClock(NOW), logging.getLogger("pc_handoff.test"))
        assert result.created is True
        assert paths.config.read_bytes() == DEFAULT_CONFIG_BYTES

    def test_partial_config_is_not_corrupt(self, env: Env) -> None:
        env.configPath.write_bytes(b'{"schemaVersion": 1, "dashboardPort": 9000}')
        before = fingerprint(env.configPath)

        result = env.load()

        assert result.config == Config(10, 5, 9000)
        assert result.warnings == [
            "dashboardRefreshIntervalSeconds: missing; default 10 applied",
            "sessionPollIntervalSeconds: missing; default 5 applied",
        ]
        assert result.notice is None
        assert result.created is False
        assert fingerprint(env.configPath) == before
        assert env.corruptFiles() == []
        for warning in result.warnings:
            assert any(warning in message for message in env.warningMessages())

    def test_write_failure_for_missing_config_keeps_defaults(
        self, env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def failingWrite(path: Path, data: bytes) -> None:
            raise PersistenceError("disk full")

        monkeypatch.setattr(config_store, "atomicWriteBytes", failingWrite)
        result = env.load()
        assert result.config == DEFAULTS
        assert result.created is False
        assert not env.configPath.exists()
        assert any(
            line["level"] == "ERROR" and "config.json" in line["message"] for line in env.appLog()
        )


# --- AC-6 / REQ AC-18 ----------------------------------------------------------------------


CORRUPT_CASES = [
    pytest.param(b"{not json", id="unparseable"),
    pytest.param(b"[1, 2]", id="root-array"),
    pytest.param(b'{"dashboardPort": 9000}', id="no-schema-version"),
    pytest.param(b'{"schemaVersion": 2}', id="schema-version-2"),
    pytest.param(b'{"schemaVersion": true}', id="schema-version-bool"),
    pytest.param(b'{"schemaVersion": "1"}', id="schema-version-string"),
    pytest.param(b'{"schemaVersion": 1.0}', id="schema-version-float"),
    pytest.param(b"null", id="root-null"),
    pytest.param(b'"config"', id="root-string"),
    pytest.param(b"", id="empty-file"),
    pytest.param(b"\xff\xfe{\x00}\x00", id="utf16"),
]


class TestWholeFileCorrupt:
    @pytest.mark.parametrize("raw", CORRUPT_CASES)
    def test_backed_up_with_identical_bytes_and_replaced_with_defaults(
        self, env: Env, raw: bytes
    ) -> None:
        env.configPath.write_bytes(raw)

        result = env.load()

        backup = env.paths.dataDir / BACKUP_NAME
        assert env.corruptFiles() == [backup]
        assert backup.read_bytes() == raw
        assert env.configPath.read_bytes() == DEFAULT_CONFIG_BYTES
        assert result.config == DEFAULTS
        assert result.warnings == []
        assert result.created is False
        assert result.notice == StartupNotice(
            fileName="config.json",
            backupName=BACKUP_NAME,
            text=f"config.json was corrupt and has been reset (backup: {BACKUP_NAME})",
        )
        # Critic TASK-005 S2: the reset line itself, at WARNING (backupCorrupt's own
        # "backed up as" line must not satisfy this).
        resetLines = [
            m
            for m in env.warningMessages()
            if re.fullmatch(
                r"config\.json was corrupt \(.+\) and has been reset to defaults "
                rf"\(backup: {re.escape(BACKUP_NAME)}\)",
                m,
                flags=re.DOTALL,
            )
        ]
        assert len(resetLines) == 1, env.appLog()

    def test_second_corruption_in_the_same_second_gets_a_distinct_backup(self, env: Env) -> None:
        env.configPath.write_bytes(b"{bad")
        env.load()
        env.configPath.write_bytes(b"[]")
        result = env.load()
        assert result.notice is not None
        assert result.notice.backupName == f"{BACKUP_NAME}-2"
        assert [p.name for p in env.corruptFiles()] == [BACKUP_NAME, f"{BACKUP_NAME}-2"]

    def test_utf8_bom_with_valid_content_is_not_corrupt(self, env: Env) -> None:
        env.configPath.write_bytes(
            b'\xef\xbb\xbf{"schemaVersion": 1, "dashboardRefreshIntervalSeconds": 20, '
            b'"sessionPollIntervalSeconds": 3, "dashboardPort": 8600}'
        )
        before = fingerprint(env.configPath)
        result = env.load()
        assert result.config == Config(20, 3, 8600)
        assert (result.warnings, result.notice) == ([], None)
        assert fingerprint(env.configPath) == before
        assert env.corruptFiles() == []

    def test_backup_failure_never_overwrites_the_corrupt_file(
        self, env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Backup before replace: if the rename fails, the corrupt file is left as it is."""

        def failingBackup(path: Path, clock: Any) -> Path:
            raise PermissionError(13, "in use")

        monkeypatch.setattr(config_store, "backupCorrupt", failingBackup)
        env.configPath.write_bytes(b"{bad")
        result = env.load()
        assert env.configPath.read_bytes() == b"{bad"
        assert result.config == DEFAULTS
        assert result.notice == StartupNotice(
            fileName="config.json",
            backupName=None,
            text="config.json is corrupt and could not be backed up; defaults are in use",
        )
        assert any(line["level"] == "ERROR" for line in env.appLog())

    def test_write_failure_after_backup_still_reports_the_backup(
        self, env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def failingWrite(path: Path, data: bytes) -> None:
            raise PersistenceError("disk full")

        monkeypatch.setattr(config_store, "atomicWriteBytes", failingWrite)
        env.configPath.write_bytes(b"{bad")
        result = env.load()
        assert (env.paths.dataDir / BACKUP_NAME).read_bytes() == b"{bad"
        assert not env.configPath.exists()
        # Critic TASK-005 S6: no config.json exists, so the notice must not say "reset".
        assert result.notice == StartupNotice(
            fileName="config.json",
            backupName=BACKUP_NAME,
            text=(
                f"config.json was corrupt and has been moved to {BACKUP_NAME}; defaults are in use"
            ),
        )
        assert result.config == DEFAULTS
        assert any(line["level"] == "ERROR" for line in env.appLog())
        assert not [m for m in env.warningMessages() if "has been reset" in m]


# --- read behaviour ------------------------------------------------------------------------


class TestReading:
    def test_bytes_are_read_exactly_once(self, env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
        env.configPath.write_bytes(AC3_BYTES)
        calls: list[Path] = []
        realRead = config_store.readBytes

        def countingRead(path: Path) -> bytes | None:
            calls.append(path)
            return realRead(path)

        monkeypatch.setattr(config_store, "readBytes", countingRead)
        env.load()
        assert calls == [env.configPath]

    def test_unreadable_config_uses_defaults_without_touching_the_file(
        self, env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env.configPath.write_bytes(AC3_BYTES)
        before = fingerprint(env.configPath)

        def deniedRead(path: Path) -> bytes | None:
            raise PermissionError(13, "access denied")

        monkeypatch.setattr(config_store, "readBytes", deniedRead)
        result = env.load()
        assert result.config == DEFAULTS
        assert result.warnings == ["config.json: could not be read; defaults applied"]
        assert result.notice is None
        assert fingerprint(env.configPath) == before
        assert env.corruptFiles() == []
        assert any(line["level"] == "ERROR" for line in env.appLog())

    def test_permission_error_from_the_real_reader_never_overwrites(
        self, env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Critic TASK-005 S1: an unreadable file is not "missing"; it is never replaced."""
        env.configPath.write_bytes(AC3_BYTES)
        before = fingerprint(env.configPath)
        realReadBytes = Path.read_bytes

        def deniedForConfig(self: Path) -> bytes:
            if self.name == "config.json":
                raise PermissionError(13, "access denied")
            return realReadBytes(self)

        monkeypatch.setattr(Path, "read_bytes", deniedForConfig)
        result = env.load()
        monkeypatch.undo()
        assert result.config == DEFAULTS
        assert result.warnings == ["config.json: could not be read; defaults applied"]
        assert (result.created, result.notice) == (False, None)
        assert fingerprint(env.configPath) == before
        assert env.corruptFiles() == []
        assert any(line["level"] == "ERROR" for line in env.appLog())

    def test_config_path_that_is_a_directory_is_left_alone(self, env: Env) -> None:
        """Critic TASK-005 S1: reading a directory fails; it is neither replaced nor backed up."""
        env.configPath.mkdir()
        result = env.load()
        assert result.config == DEFAULTS
        assert result.warnings == ["config.json: could not be read; defaults applied"]
        assert env.configPath.is_dir()
        assert env.corruptFiles() == []
        assert any(line["level"] == "ERROR" for line in env.appLog())

    def test_config_path_escaping_the_data_directory_is_unreadable(
        self, env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Critic TASK-005 S5: a config.json link that resolves outside the data directory
        makes resolveInside raise ValueError; loadConfig treats it as unreadable."""
        realResolve = DataPaths.resolveInside

        def escaping(self: DataPaths, name: str) -> Path:
            if name == "config.json":
                raise ValueError("path escapes the data directory: 'config.json'")
            return realResolve(self, name)

        monkeypatch.setattr(DataPaths, "resolveInside", escaping)
        result = env.load()
        assert result.config == DEFAULTS
        assert result.warnings == ["config.json: could not be read; defaults applied"]
        assert (result.created, result.notice) == (False, None)
        assert sorted(p.name for p in env.paths.dataDir.iterdir()) == ["logs"]
        assert any(
            line["level"] == "ERROR" and "escapes" in line["message"] for line in env.appLog()
        )


def test_data_directory_that_is_a_regular_file(tmp_path: Path, capturedAppLog) -> None:
    """Critic TASK-005 S3: if the data directory cannot be created, loadConfig still returns
    the defaults and logs an ERROR instead of raising."""
    notADirectory = tmp_path / "data"
    notADirectory.write_bytes(b"I am a file")
    logger = logging.getLogger("pc_handoff.t")
    result = loadConfig(DataPaths(notADirectory), FakeClock(NOW), logger)
    assert result.config == DEFAULTS
    assert result.created is False
    assert notADirectory.read_bytes() == b"I am a file"
    assert any(line["level"] == "ERROR" for line in capturedAppLog())
