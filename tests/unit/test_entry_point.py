"""Package metadata and the ``python -m pc_handoff`` entry point.

TASK-001 asserted the stub launcher; TASK-009 replaced it (TASK-001 S6), so the entry point
is now checked through the real ``--check-lock-only`` path, which exits without serving.
"""

import runpy
import sys
from pathlib import Path

import pytest

import pc_handoff
from pc_handoff import launcher


def test_version() -> None:
    assert pc_handoff.__version__ == "0.1.0"


def test_launcher_is_no_longer_a_stub() -> None:
    assert not hasattr(launcher, "NOT_IMPLEMENTED_MESSAGE")


def test_module_entry_point_exits_with_the_launchers_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        sys, "argv", ["pc_handoff", "--data-dir", str(tmp_path), "--check-lock-only"]
    )
    with pytest.raises(SystemExit) as exitInfo:
        runpy.run_module("pc_handoff", run_name="__main__")
    assert exitInfo.value.code == 0
    assert (tmp_path / "pc-handoff.lock").exists()
    assert capsys.readouterr().out == ""


def test_module_entry_point_passes_a_failure_code_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launcher, "pythonVersion", lambda: (3, 10, 0))
    monkeypatch.setattr(sys, "argv", ["pc_handoff", "--data-dir", str(tmp_path)])
    with pytest.raises(SystemExit) as exitInfo:
        runpy.run_module("pc_handoff", run_name="__main__")
    assert exitInfo.value.code == 5
