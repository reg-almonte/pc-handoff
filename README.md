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

# Run the application (the dashboard is at http://<host>:<dashboardPort>/)
.venv\Scripts\python -m pc_handoff --data-dir C:\pc-handoff\data
```

On other platforms use `.venv/bin/` instead of `.venv\Scripts\` and
`pytest && python tools/check_coverage.py coverage.json` for the gated test run.

Notes:

- Subset runs during development (for example `pytest tests/unit -k clock`) should add
  `--no-cov`, otherwise the coverage gate fails on partial coverage.
- The performance benchmark runs separately with `pytest -m perf --no-cov`.
- Tests marked `slow` use real time (the production 5 s poll interval, real subprocesses);
  they run by default. `pytest -m "not slow and not perf" --no-cov` is a quicker
  development run (without the coverage gate).

## Documentation

- [docs/operations.md](docs/operations.md) - requirements, install, run and stop, exit
  codes, `config.json` settings, port and network, data directory, lock, `app.log`, test
  hooks and the known limitations.
- [docs/data-files.md](docs/data-files.md) - formats of `config.json`, `status.json` and
  `activity_log.json` (fields, pairing rule, canonical layout, backups).
- [docs/manual-acceptance.md](docs/manual-acceptance.md) - the checks to run by hand on the
  shared PC (REQ AC-13, the AC-21 p95 timing, real RDP and console sessions, A3, A4).

## Layout

- `src/pc_handoff/` - the application package (src layout)
- `tests/` - pytest suite (`unit/`, `integration/`, `ui/`, `acceptance/` (one end-to-end test
  per REQ-001 AC), `windows/`, `perf/`)
- `tools/check_coverage.py` - separate line and branch coverage gates
- `docs/` - operator and data-format documentation
- `data/` - default runtime data directory (created at first run, not committed);
  application logs go to `data/logs/app.log` as JSON lines
