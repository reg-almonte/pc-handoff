"""JSON to UTF-8 bytes for the data files, shared by the stores (TASK-006, Critic S2)."""

import json
from typing import Any


def encodeJson(value: Any, indent: int | None = None) -> bytes:
    """``json.dumps(value, ensure_ascii=False, indent=indent)`` encoded as UTF-8 (no BOM).

    A string holding a lone surrogate (possible after ``json.loads`` of ``"\\ud800"``) cannot
    be encoded as UTF-8; such a value is written with ASCII escapes instead, so the file stays
    valid UTF-8 and the value is unchanged. The fallback is deterministic, so a canonical
    activity log stays canonical. ``TypeError``/``ValueError`` from ``json.dumps`` propagate.
    """
    try:
        return json.dumps(value, ensure_ascii=False, indent=indent).encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(value, indent=indent).encode("ascii")
