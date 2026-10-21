"""Tests for the package metadata and the ``python -m pc_handoff`` stub (TASK-001)."""

import runpy

import pytest

import pc_handoff
from pc_handoff import launcher


def test_version() -> None:
    assert pc_handoff.__version__ == "0.1.0"


def test_launcher_stub_prints_and_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert launcher.main([]) == 0
    assert "not yet implemented" in capsys.readouterr().out


def test_module_entry_point_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exitInfo:
        runpy.run_module("pc_handoff", run_name="__main__")
    assert exitInfo.value.code == 0
    assert "not yet implemented" in capsys.readouterr().out
