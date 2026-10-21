"""The test-side activity document checker falls back only when TASK-005's module is missing
(Critic TASK-004 S1)."""

import importlib

import pytest

from tests.fakes import activity_checks
from tests.fakes.activity_checks import SCHEMAS_MODULE, realValidatorProblems

DOCUMENT = {"schemaVersion": 1, "events": []}


def failingImport(error):
    def importModule(name):
        assert name == SCHEMAS_MODULE
        raise error

    return importModule


def test_missing_schemas_module_falls_back(monkeypatch):
    error = ModuleNotFoundError(f"No module named '{SCHEMAS_MODULE}'", name=SCHEMAS_MODULE)
    monkeypatch.setattr(activity_checks.importlib, "import_module", failingImport(error))
    assert realValidatorProblems(DOCUMENT) == []


@pytest.mark.parametrize(
    "error",
    [
        ModuleNotFoundError("No module named 'typo'", name="typo"),
        ImportError("cannot import name 'x' from 'pc_handoff.clock'"),
        SyntaxError("broken schemas.py"),
    ],
    ids=["missing-dependency", "bad-name", "syntax-error"],
)
def test_broken_schemas_module_propagates(monkeypatch, error):
    monkeypatch.setattr(activity_checks.importlib, "import_module", failingImport(error))
    with pytest.raises(type(error)):
        realValidatorProblems(DOCUMENT)


def test_present_schemas_module_is_called(monkeypatch):
    calls = []

    class FakeSchemas:
        @staticmethod
        def validateActivityDocument(document):
            calls.append(document)
            return ["issue"]

    monkeypatch.setattr(activity_checks.importlib, "import_module", lambda name: FakeSchemas)
    assert realValidatorProblems(DOCUMENT) == ["issue"]
    assert calls == [DOCUMENT]


def test_module_name_is_the_task_005_schemas_module():
    assert SCHEMAS_MODULE == "pc_handoff.persistence.schemas"
    assert activity_checks.importlib is importlib
