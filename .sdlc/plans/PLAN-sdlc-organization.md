# Plan: AI-Driven SDLC Structure, Governance & Lifecycle Organization

This plan establishes a structured, intuitive organization for AI-driven development lifecycle (SDLC) artifacts, governance documentation, repository workflows, and tracking files for the `pc-handoff` project.

---

## 1. Context & Objectives

1. **Relocate Root AI Files**:
   Move temporary/LLM-focused root files (`LOG.md`, `PLAN.md`) into a dedicated directory structure to keep the root directory clean and professional.
2. **Establish Repository Governance (`GEMINI.md`)**:
   Implement `GEMINI.md` at the workspace root (which is automatically loaded by Antigravity as a behavioral customization rule) establishing issue tracking, branching, and commit conventions.
3. **Structured SDLC Artifacts**:
   Introduce standard governance and artifact structures (`.sdlc/`, `.github/`, ADRs, and templates) aligned with the existing references in `docs/project-overview.md` and `docs/operations.md`.

---

## 2. Directory Structure Recommendation: `.sdlc/` vs Alternatives

### Analysis of Candidates:
- **`.sdlc/` (Recommended)**:
  - Already cited throughout the existing documentation (e.g. `docs/project-overview.md:43` references `.sdlc/docs/`, and `docs/operations.md:14` references `.sdlc/specs/REQ-001-...` and `.sdlc/docs/decisions.log`).
  - Standard industry nomenclature for Software Development Life Cycle.
  - Houses specifications, plans, gap analyses, decision logs, and ADRs.
- **`.agents/`**:
  - Reserved for Antigravity-specific configuration, prompt extensions, and skill definitions.
- **Decision**:
  - Adopt **`.sdlc/`** for lifecycle artifacts (`plans/`, `logs/`, `specs/`, `adrs/`).
  - Use **`GEMINI.md`** at the workspace root for agent instructions and project governance.

---

## 3. Target Directory & File Layout

```text
pc-handoff/
├── .github/                              # GitHub integration & automation
│   ├── ISSUE_TEMPLATE/
│   │   ├── feature_request.yml
│   │   └── bug_report.yml
│   ├── PULL_REQUEST_TEMPLATE.md
│   └── workflows/
│       └── ci.yml                        # Automated test, coverage & lint gates
├── .sdlc/                                # AI & Human SDLC Workspace
│   ├── plans/
│   │   ├── PLAN-001-reservation-and-handoff.md  # (Migrated from PLAN.md)
│   │   └── PLAN-sdlc-organization.md            # (Migrated from PLAN-sldc.md)
│   ├── logs/
│   │   ├── gap-analysis.md                      # (Migrated from LOG.md)
│   │   └── decisions.log                        # Chronological decision ledger
│   └── specs/
│       ├── REQ-001-pc-status-monitoring/        # Historical specs & ADRs
│       └── REQ-002-reservation-and-handoff/     # SD001_System_Design_v0.1.md
├── docs/                                 # Public / operator-facing documentation
│   ├── data-files.md
│   ├── manual-acceptance.md
│   ├── operations.md
│   └── project-overview.md
├── GEMINI.md                             # Antigravity Governance & Agent Rulebook
├── CONTRIBUTING.md                       # Developer contribution guidelines
├── README.md
└── pyproject.toml
```

---

## 4. Proposed Governance in `GEMINI.md`

`GEMINI.md` will define concrete, enforce-ready standards:

### A. Issue & Ticket Management
- **Platform**: GitHub Issues.
- **Ticket Format**:
  - `TASK-XXX`: Specific implementation tasks.
  - `BUG-XXX`: Defect reports with reproduction steps.
  - `REQ-XXX`: Requirement specifications.
- **Labels**: `spec`, `feature`, `bug`, `documentation`, `testing`, `blocked`.

### B. Git Branching Model
A predictable GitHub Flow / GitFlow hybrid:
- **`main`**: Protected, production-ready, passing all CI gates (>86% coverage, zero ruff warnings).
- **`feature/task<NNN>-<short-description>`**: For new features (e.g. `feature/task012-reservation-model`).
- **`bugfix/bug<NNN>-<short-description>`**: For bug fixes (e.g. `bugfix/bug004-disconnect-timing`).
- **`release/v<X.Y.Z>`**: Release preparation, version bumps, and documentation freeze.
- **`hotfix/bug<NNN>-<short-description>`**: Urgent fixes branched directly off `main`.

### C. Commit Message Standard (Conventional Commits)
Format: `<type>(<scope>): <short description>`
- Types: `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `chore`.
- Example: `feat(reservations): add reservation state machine and validator`
- Footer: Closes task/issue reference (e.g., `Closes #12`).

### D. Automated Quality Gates
Every PR and branch merge must satisfy:
1. `ruff check .` (0 errors)
2. `ruff format --check .` (0 warnings)
3. Line coverage `>= 86%` and branch coverage `>= 86%` (`pytest` + `check_coverage.py`).

---

## 5. Suggested Supporting Files for Governance & Data

| File Path | Purpose |
| :--- | :--- |
| **`GEMINI.md`** | AI agent guidelines, coding standards, branch rules, and workflow protocol. |
| **`CONTRIBUTING.md`** | Onboarding guide for human developers (setup, test commands, PR checklist). |
| **`.sdlc/docs/decisions.log`** | Append-only ledger of architectural decisions and rationale. |
| **`.github/pull_request_template.md`** | Checklist enforcing coverage, linting, and documentation updates. |
| **`.github/workflows/ci.yml`** | GitHub Actions workflow automating the Windows test & coverage gates. |

---

## 6. Execution Steps

1. **Step 1: Create Directory Structure**:
   - Create `.sdlc/plans/`, `.sdlc/logs/`, `.sdlc/specs/`, `.sdlc/docs/`.
2. **Step 2: Relocate AI Files**:
   - Move `PLAN.md` &rarr; `.sdlc/plans/PLAN-001-reservation-and-handoff.md`.
   - Move `LOG.md` &rarr; `.sdlc/logs/gap-analysis.md`.
   - Update `.gitignore` to reflect the new structure.
3. **Step 3: Author `GEMINI.md`**:
   - Write comprehensive governance rules for ticket management, branching, commit formats, and quality gates.
4. **Step 4: Author Supporting Files**:
   - Create `.sdlc/docs/decisions.log`.
   - Create `.github/pull_request_template.md` and basic issue templates.
5. **Step 5: Move this plan into `.sdlc/plans/`**:
   - Relocate `PLAN-sldc.md` &rarr; `.sdlc/plans/PLAN-sdlc-organization.md`.
