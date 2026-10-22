"""Cross-file pairing check over ``activity_log.json`` and its backups (TASK-008, TASK-011).

The requirement's pairing rule holds across the log and all of its ``.corrupt-*`` backups:
each ``usagePeriodId`` has at most one ``session_start`` and at most one ``session_end``, and
an end never precedes its start. Backups that cannot be parsed (or have no ``events`` array)
are skipped: their events cannot be read by anyone.
"""

import json
from pathlib import Path
from typing import Any

from pc_handoff.clock import parseIso

LOG_NAME = "activity_log.json"


def logFiles(dataDir: Path) -> list[Path]:
    """The log (if present) followed by its backups, in name order."""
    directory = Path(dataDir)
    files = [directory / LOG_NAME] if (directory / LOG_NAME).exists() else []
    return files + sorted(directory.glob(f"{LOG_NAME}.corrupt-*"))


def readableEvents(path: Path) -> list[dict[str, Any]] | None:
    try:
        doc = json.loads(path.read_bytes().decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        return None
    events = doc.get("events") if isinstance(doc, dict) else None
    if not isinstance(events, list):
        return None
    return [e for e in events if isinstance(e, dict)]


def pairingProblemsAcross(dataDir: Path) -> list[str]:
    """Every pairing violation across the log and its parseable backups."""
    starts: dict[str, tuple[str, str]] = {}
    ends: dict[str, tuple[str, str]] = {}
    problems: list[str] = []
    for path in logFiles(dataDir):
        events = readableEvents(path)
        if events is None:
            continue
        for event in events:
            periodId = event.get("usagePeriodId")
            kind = event.get("eventType")
            seen = starts if kind == "session_start" else ends if kind == "session_end" else None
            if seen is None or not isinstance(periodId, str):
                continue
            if periodId in seen:
                problems.append(f"second {kind} for {periodId} in {path.name}")
                continue
            seen[periodId] = (str(event.get("timestamp")), path.name)
    for periodId, (endAt, endFile) in ends.items():
        if periodId in starts:
            startAt, startFile = starts[periodId]
            if parseIso(endAt) < parseIso(startAt):
                problems.append(
                    f"session_end for {periodId} ({endFile}) precedes its start ({startFile})"
                )
    return problems


def assertPairingAcross(dataDir: Path) -> None:
    problems = pairingProblemsAcross(dataDir)
    assert problems == [], problems
