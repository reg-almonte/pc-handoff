# Contributing to PC Handoff

Welcome to the **PC Handoff** project. This guide outlines how to set up your environment, follow our development workflow, and contribute changes.

---

## 1. Quick Start Development Setup

### Prerequisites
- Windows 10/11 or Windows Server (with Remote Desktop enabled for session detection).
- Python 3.11 or later (developed and tested on 3.13 and 3.14).

### Virtual Environment Setup
```powershell
python -m venv .venv
.venv\Scripts\pip install -e .[dev]
```

---

## 2. Running Quality Checks Locally

We strictly enforce linting and test coverage gates. Run these before pushing any code:

```powershell
# Lint and format checks
.venv\Scripts\ruff check .
.venv\Scripts\ruff format --check .

# Full test suite with coverage gate (>86% line and branch)
.venv\Scripts\pytest; if ($?) { .venv\Scripts\python tools\check_coverage.py coverage.json }
```

### Helpful Development Test Commands
- **Quick dev run (skip slow tests and coverage gate)**:
  ```powershell
  .venv\Scripts\pytest -m "not slow and not perf" --no-cov
  ```
- **Run a single test file**:
  ```powershell
  .venv\Scripts\pytest tests/unit/test_clock.py --no-cov
  ```

---

## 3. Development Workflow

1. **Find or create a GitHub Issue** (`TASK-XXX` or `BUG-XXX`).
2. **Create a branch** using the naming standard in [GEMINI.md](GEMINI.md):
   - `feature/task<NNN>-<short-description>`
   - `bugfix/bug<NNN>-<short-description>`
3. **Commit your changes** following Conventional Commits (e.g. `feat(scope): description`).
4. **Ensure all tests and coverage gates pass** locally.
5. **Open a Pull Request** using the provided PR template.

---

## 4. Documentation & Architecture

- **Specifications & Plans**: See `.sdlc/plans/` and `.sdlc/specs/`.
- **Architectural Decisions**: See `.sdlc/docs/decisions.log`.
- **Public Operator Docs**: See `docs/operations.md` and `docs/data-files.md`.
