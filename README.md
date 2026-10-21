# pc-handoff

Shared PC status monitoring. It detects Windows user sessions, derives the PC's status
(Available / In Use / Unknown), persists it as local JSON files and shows it on a Streamlit
dashboard on the LAN.

This directory holds all application code, tests, configuration and documentation. Refer to
[CLAUDE.md](../CLAUDE.md) for the workspace isolation policy.

## Requirements

- Python 3.11 or later (developed and tested on 3.13 and 3.14)

## Quick start (Windows, from this `external/` directory)

```powershell
python -m venv .venv
.venv\Scripts\pip install -e .[dev]

# Full test run with the line and branch coverage gates (each must exceed 85%)
.venv\Scripts\pytest; if ($?) { .venv\Scripts\python tools\check_coverage.py coverage.json }

# Lint and format checks
.venv\Scripts\ruff check .
.venv\Scripts\ruff format --check .

# Run the application (the launcher is a stub until TASK-009)
.venv\Scripts\python -m pc_handoff
```

On other platforms use `.venv/bin/` instead of `.venv\Scripts\` and
`pytest && python tools/check_coverage.py coverage.json` for the gated test run.

Notes:

- Subset runs during development (for example `pytest tests/unit -k clock`) should add
  `--no-cov`, otherwise the coverage gate fails on partial coverage.
- The performance benchmark runs separately with `pytest -m perf --no-cov`.

## Layout

- `src/pc_handoff/` - the application package (src layout)
- `tests/` - pytest suite (`unit/`, later `integration/`, `ui/`, `acceptance/`, `windows/`, `perf/`)
- `tools/check_coverage.py` - separate line and branch coverage gates
- `data/` - default runtime data directory (created at first run, not committed);
  application logs go to `data/logs/app.log` as JSON lines
