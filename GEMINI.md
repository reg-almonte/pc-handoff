# Project Governance & Development Standards: PC Handoff

This document defines governance rules, Git workflow, ticketing standards, and quality requirements for developers and AI agents working on **PC Handoff**.

---

## 1. Project Principles & Architecture Guardrails

1. **Spec-Driven**: Every substantial capability begins with an approved specification in `.sdlc/specs/`.
2. **Offline-First & Local Persistence**: The application must run without internet access, external databases, or cloud brokers. All state is held in atomic local JSON files (`config.json`, `status.json`, `activity_log.json`, `reservations.json`).
3. **Continuous Knowledge**: Architectural decisions, trade-offs, and critical assumptions are recorded in `.sdlc/docs/decisions.log`.
4. **Deterministic Quality Gates**: Automated gates must pass before any code is merged into `main`.

---

## 2. Issue & Ticket Governance (GitHub Issues)

We use **GitHub Issues** for all work items:

- **Issue Types & Prefixes**:
  - `REQ-XXX`: Requirement specifications (e.g. `REQ-002: Reservation & Ownership Handoff`).
  - `TASK-XXX`: Implementation tasks (e.g. `TASK-012: Reservation Domain Models & Schemas`).
  - `BUG-XXX`: Defect reports with clear reproduction steps (e.g. `BUG-005: Outage disconnect detection timeout`).
- **Standard Labels**:
  - `type:feature`, `type:bug`, `type:spec`, `type:task`, `area:ui`, `area:runtime`, `area:persistence`, `status:blocked`.
- **Milestones**: Group tickets by release target (e.g. `v0.1.0-mvp`, `v0.2.0-reservations`).

---

## 3. Git Repository & Branching Strategy

We follow a standardized Git Flow / GitHub Flow hybrid:

```text
main ──────────────────────────────────────────●─────── (Tagged: v0.2.0)
       \                                      /
release \───────────────────────●────────────/
         \                     /
feature   \──●───●── (PR) ────/
```

### Branch Naming Conventions
- **`main`**: Protected, always deployable, passing all tests and lint checks.
- **`feature/task<NNN>-<short-description>`**:
  - Used for new features or major enhancements.
  - *Example*: `feature/task012-reservation-models`, `feature/task014-streamlit-queue-ui`
- **`bugfix/bug<NNN>-<short-description>`**:
  - Used for non-critical bug fixes during standard development.
  - *Example*: `bugfix/bug003-status-reset-handling`
- **`release/v<X.Y.Z>`**:
  - Used for release preparation, final verification, and documentation freeze.
  - *Example*: `release/v0.2.0`
- **`hotfix/bug<NNN>-<short-description>`**:
  - Urgent fixes created directly from `main` for production defects.
  - *Example*: `hotfix/bug009-port-probe-wsaeinval`

---

## 4. Commit Message Standard (Conventional Commits)

Commit messages must follow the [Conventional Commits](https://www.conventionalcommits.org/) specification:

```text
<type>(<scope>): <short summary in imperative mood>

[optional body explaining rationale]

[optional footer referencing issue, e.g. Closes #12 or Ref TASK-012]
```

### Allowed Types:
- `feat`: A new feature or capability.
- `fix`: A bug fix.
- `docs`: Documentation only changes.
- `test`: Adding or correcting tests.
- `refactor`: Code changes that neither fix a bug nor add a feature.
- `perf`: Code changes that improve performance.
- `chore`: Build process, dependencies, tooling, or repository maintenance.

*Example*:
```text
feat(reservations): add reservation state machine and validator

Implement pure transition functions for queue advance, claim timeouts,
and correlation with active RDP sessions.

Closes #12
```

---

## 5. Automated Quality & Coverage Gates

Before submitting a PR or merging to `main`, all checks must pass locally:

```powershell
# 1. Lint & formatting checks
.venv\Scripts\ruff check .
.venv\Scripts\ruff format --check .

# 2. Automated test suite & coverage gate (each must exceed 86%)
.venv\Scripts\pytest; if ($?) { .venv\Scripts\python tools\check_coverage.py coverage.json }
```

- **Line Coverage Gate**: `>= 86.0%`
- **Branch Coverage Gate**: `>= 86.0%`
- **No Unhandled Warnings or Lint Violations**.

---

## 6. AI Agent Guidelines

1. **Check Existing Artifacts First**:
   - Check `.sdlc/plans/` for active roadmaps.
   - Check `.sdlc/logs/gap-analysis.md` for feature tracking.
   - Check `.sdlc/docs/decisions.log` before making architectural choices.
2. **Preserve Documentation Integrity**: Maintain comments, docstrings, and cross-references.
3. **Keep Plans & Logs Updated**: Record all significant architectural decisions in `.sdlc/docs/decisions.log`.
