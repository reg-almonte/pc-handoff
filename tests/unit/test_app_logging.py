"""Tests for pc_handoff.app_logging (TASK-001 AC-2, AC-3)."""

import json
import logging
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from pc_handoff.app_logging import (
    LOGGER_NAME,
    AppLogFileHandler,
    JsonLineFormatter,
    configureAppLogging,
    newTraceId,
    traceIdVar,
    traceScope,
)
from pc_handoff.clock import parseIso


@pytest.fixture(autouse=True)
def isolatedAppLogger() -> Iterator[None]:
    """Close app-log handlers after each test so tests stay isolated and tmp files deletable."""
    logger = logging.getLogger(LOGGER_NAME)
    originalLevel = logger.level
    yield
    for handler in list(logger.handlers):
        if isinstance(handler, AppLogFileHandler):
            logger.removeHandler(handler)
            handler.close()
    logger.setLevel(originalLevel)


def appLogPath(dataDir: Path) -> Path:
    return dataDir / "logs" / "app.log"


def readLines(dataDir: Path) -> list[dict]:
    text = appLogPath(dataDir).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def makeRecord(msg: object = "hello", args: tuple = (), **attrs: object) -> logging.LogRecord:
    record = logging.LogRecord(LOGGER_NAME, logging.INFO, __file__, 1, msg, args, None)
    for key, value in attrs.items():
        setattr(record, key, value)
    return record


class TestTraceId:
    def test_new_trace_id_is_uuid4_hex(self) -> None:
        first, second = newTraceId(), newTraceId()
        assert len(first) == 32 and int(first, 16) >= 0
        assert first != second

    def test_scope_sets_and_restores(self) -> None:
        assert traceIdVar.get() is None
        with traceScope("outer") as value:
            assert value == "outer"
            with traceScope("inner"):
                assert traceIdVar.get() == "inner"
            assert traceIdVar.get() == "outer"
        assert traceIdVar.get() is None

    def test_scope_restores_after_exception(self) -> None:
        with pytest.raises(RuntimeError), traceScope("boom"):
            raise RuntimeError("x")
        assert traceIdVar.get() is None


class TestConfigureAppLogging:
    def test_ac02_warning_line_has_required_fields(self, tmpDataDir: Path) -> None:
        logger = configureAppLogging(tmpDataDir)
        assert logger.name == LOGGER_NAME
        with traceScope("abc123"):
            logging.getLogger(LOGGER_NAME).warning(
                "session enumeration failed", extra={"detail": "access denied"}
            )

        lines = readLines(tmpDataDir)
        assert len(lines) == 1
        line = lines[0]
        assert set(line) == {"timestamp", "level", "traceId", "message", "detail"}
        assert line["level"] == "WARNING"
        assert line["traceId"] == "abc123"
        assert line["message"] == "session enumeration failed"
        assert line["detail"] == "access denied"
        parsed = parseIso(line["timestamp"])  # raises if there is no offset
        assert parsed.utcoffset() is not None
        assert "." not in line["timestamp"]

    def test_child_logger_lines_reach_app_log(self, tmpDataDir: Path) -> None:
        configureAppLogging(tmpDataDir)
        logging.getLogger(f"{LOGGER_NAME}.runtime.poller").info("poll completed")
        (line,) = readLines(tmpDataDir)
        assert line == {
            "timestamp": line["timestamp"],
            "level": "INFO",
            "traceId": None,
            "message": "poll completed",
        }

    def test_creates_logs_dir_and_appends(self, tmpDataDir: Path) -> None:
        appLogPath(tmpDataDir).parent.mkdir(parents=True)
        appLogPath(tmpDataDir).write_text('{"existing": true}\n', encoding="utf-8")
        configureAppLogging(tmpDataDir).error("second")
        lines = readLines(tmpDataDir)
        assert lines[0] == {"existing": True}
        assert lines[1]["message"] == "second"

    def test_idempotent_for_same_dir(self, tmpDataDir: Path) -> None:
        logger = configureAppLogging(tmpDataDir)
        again = configureAppLogging(tmpDataDir)
        assert again is logger
        assert sum(isinstance(h, AppLogFileHandler) for h in logger.handlers) == 1
        logger.warning("once")
        assert len(readLines(tmpDataDir)) == 1

    def test_reconfigure_with_other_dir_replaces_handler(self, tmp_path: Path) -> None:
        firstDir, secondDir = tmp_path / "a", tmp_path / "b"
        configureAppLogging(firstDir)
        logger = configureAppLogging(secondDir)
        assert sum(isinstance(h, AppLogFileHandler) for h in logger.handlers) == 1
        logger.warning("to second")
        assert appLogPath(firstDir).read_text(encoding="utf-8") == ""
        assert readLines(secondDir)[0]["message"] == "to second"

    def test_keeps_explicit_logger_level(self, tmpDataDir: Path) -> None:
        logging.getLogger(LOGGER_NAME).setLevel(logging.WARNING)
        configureAppLogging(tmpDataDir).info("dropped")
        assert appLogPath(tmpDataDir).read_text(encoding="utf-8") == ""

    def test_default_level_is_info(self, tmpDataDir: Path) -> None:
        logging.getLogger(LOGGER_NAME).setLevel(logging.NOTSET)
        configureAppLogging(tmpDataDir).info("kept")
        assert readLines(tmpDataDir)[0]["message"] == "kept"

    def test_ignores_foreign_handlers(self, tmpDataDir: Path) -> None:
        logger = logging.getLogger(LOGGER_NAME)
        foreign = logging.NullHandler()
        logger.addHandler(foreign)
        try:
            configureAppLogging(tmpDataDir)
            assert foreign in logger.handlers
        finally:
            logger.removeHandler(foreign)

    def test_non_ascii_user_name_written_as_utf8(self, tmpDataDir: Path) -> None:
        configureAppLogging(tmpDataDir).info("session start for %s", "山田太郎")
        raw = appLogPath(tmpDataDir).read_bytes()
        assert "山田太郎".encode() in raw  # not \\u-escaped
        assert readLines(tmpDataDir)[0]["message"] == "session start for 山田太郎"


class TestTraceIdIsolation:
    def test_ac03_threads_keep_their_own_trace_ids(self, tmpDataDir: Path) -> None:
        logger = configureAppLogging(tmpDataDir)
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def worker(traceId: str) -> None:
            try:
                with traceScope(traceId):
                    for index in range(50):
                        if index == 0:
                            barrier.wait(timeout=5)
                        logger.info("from %s", traceId)
            except BaseException as error:  # pragma: no cover - surfaced by the assert below
                errors.append(error)

        threads = [threading.Thread(target=worker, args=(tid,)) for tid in ("trace-a", "trace-b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        logger.info("outside any scope")

        assert errors == []
        lines = readLines(tmpDataDir)
        assert len(lines) == 101
        for line in lines[:-1]:
            assert line["message"] == f"from {line['traceId']}"
        assert sum(line["traceId"] == "trace-a" for line in lines) == 50
        assert sum(line["traceId"] == "trace-b" for line in lines) == 50
        assert lines[-1]["traceId"] is None
        assert lines[-1]["message"] == "outside any scope"


class TestJsonLineFormatter:
    def test_exactly_one_line_even_with_newlines(self) -> None:
        output = JsonLineFormatter().format(makeRecord("line one\nline two"))
        assert "\n" not in output
        assert json.loads(output)["message"] == "line one\nline two"

    def test_exception_becomes_detail_string(self) -> None:
        formatter = JsonLineFormatter()
        try:
            raise OSError("enumeration failed")
        except OSError:
            record = makeRecord("poll failed")
            record.exc_info = sys.exc_info()
        payload = json.loads(formatter.format(record))
        assert isinstance(payload["detail"], str)
        assert "OSError: enumeration failed" in payload["detail"]

    def test_exception_combined_with_detail(self, tmpDataDir: Path) -> None:
        logger = configureAppLogging(tmpDataDir)
        try:
            raise ValueError("bad")
        except ValueError:
            logger.exception("failed", extra={"detail": "while writing status.json"})
        (line,) = readLines(tmpDataDir)
        assert line["level"] == "ERROR"
        assert line["detail"].startswith("while writing status.json\n")
        assert "ValueError: bad" in line["detail"]

    def test_bad_format_args_fall_back_to_repr(self) -> None:
        output = JsonLineFormatter().format(makeRecord("value %d", ("not a number",)))
        assert json.loads(output)["message"] == repr("value %d")

    def test_non_serializable_detail_uses_str(self) -> None:
        output = JsonLineFormatter().format(makeRecord(detail={"path": Path("x")}))
        assert json.loads(output)["detail"] == {"path": str(Path("x"))}

    def test_unserializable_detail_falls_back_to_repr(self) -> None:
        circular: list = []
        circular.append(circular)
        output = JsonLineFormatter().format(makeRecord(detail=circular))
        assert json.loads(output)["detail"] == "[[...]]"

    def test_tuple_keys_fall_back_to_repr(self) -> None:
        output = JsonLineFormatter().format(makeRecord(detail={(1, 2): "x"}))
        assert json.loads(output)["detail"] == repr({(1, 2): "x"})

    def test_record_trace_id_attribute_wins(self) -> None:
        with traceScope("from-context"):
            output = JsonLineFormatter().format(makeRecord(traceId="explicit"))
        assert json.loads(output)["traceId"] == "explicit"

    def test_trace_id_from_context(self) -> None:
        with traceScope("ctx"):
            output = JsonLineFormatter().format(makeRecord())
        assert json.loads(output)["traceId"] == "ctx"
