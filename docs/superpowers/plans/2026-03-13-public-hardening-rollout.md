# Public Hardening Rollout Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ship three upstream-safe PRs that improve public trust, safety, and retrieval quality in `claude-cortex` without leaking private local assumptions.

**Architecture:** Use three isolated worktrees rooted at the clean public clone. Keep changes scoped by branch: `tests-ci`, `schema-safety`, and `retrieval-ranking`. Preserve the current public MCP tool surface while adding validation, verification, and benchmark scaffolding.

**Tech Stack:** Python, pytest, GitHub Actions, SQLite/FTS5, Pydantic, git worktrees

## Chunk 1: Prepare Shared Execution Context

### Task 1: Verify clean public clone state

**Files:**
- Inspect: `/Users/cameronbennion/tmp-claude-cortex`

- [ ] **Step 1: Confirm git status is clean enough for worktree creation**

Run: `git -C /Users/cameronbennion/tmp-claude-cortex status --short`
Expected: no unexpected tracked changes beyond the new spec/plan docs

- [ ] **Step 2: Confirm worktree directory strategy**

Run: `ls -d /Users/cameronbennion/tmp-claude-cortex/.worktrees /Users/cameronbennion/tmp-claude-cortex/worktrees 2>/dev/null || true`
Expected: determine whether an existing worktree directory is present

- [ ] **Step 3: Create or reuse a project-local worktree directory**

If missing, create `.worktrees/` and ensure it is ignored before adding worktrees.

- [ ] **Step 4: Commit spec/plan docs if needed**

Run:
```bash
git -C /Users/cameronbennion/tmp-claude-cortex add docs/superpowers/specs/2026-03-13-public-hardening-rollout-design.md docs/superpowers/plans/2026-03-13-public-hardening-rollout.md
git -C /Users/cameronbennion/tmp-claude-cortex commit -m "docs: define public hardening rollout"
```

## Chunk 2: Create Parallel Worktrees

### Task 2: Create three isolated worktrees

**Files:**
- Create directories under: `/Users/cameronbennion/tmp-claude-cortex/.worktrees/`

- [ ] **Step 1: Create `tests-ci` worktree**

Run: `git -C /Users/cameronbennion/tmp-claude-cortex worktree add -b tests-ci /Users/cameronbennion/tmp-claude-cortex/.worktrees/tests-ci`

- [ ] **Step 2: Create `schema-safety` worktree**

Run: `git -C /Users/cameronbennion/tmp-claude-cortex worktree add -b schema-safety /Users/cameronbennion/tmp-claude-cortex/.worktrees/schema-safety`

- [ ] **Step 3: Create `retrieval-ranking` worktree**

Run: `git -C /Users/cameronbennion/tmp-claude-cortex worktree add -b retrieval-ranking /Users/cameronbennion/tmp-claude-cortex/.worktrees/retrieval-ranking`

- [ ] **Step 4: Validate each worktree**

Run:
```bash
git -C /Users/cameronbennion/tmp-claude-cortex/.worktrees/tests-ci ls-files >/dev/null
git -C /Users/cameronbennion/tmp-claude-cortex/.worktrees/schema-safety ls-files >/dev/null
git -C /Users/cameronbennion/tmp-claude-cortex/.worktrees/retrieval-ranking ls-files >/dev/null
git -C /Users/cameronbennion/tmp-claude-cortex worktree list
```

### Task 3: Dispatch branch ownership

**Files:**
- Optional untracked notes:
  - `/Users/cameronbennion/tmp-claude-cortex/.worktrees/tests-ci/RESULTS.md`
  - `/Users/cameronbennion/tmp-claude-cortex/.worktrees/schema-safety/RESULTS.md`
  - `/Users/cameronbennion/tmp-claude-cortex/.worktrees/retrieval-ranking/RESULTS.md`

- [ ] **Step 1: Assign `tests-ci` branch scope**

Ownership:
- tests package and fixtures
- `.github/workflows/*`
- lightweight contributor docs for test commands
- tests should target the canonical `src/` public surface

- [ ] **Step 2: Assign `schema-safety` branch scope**

Ownership:
- config/env validation
- public-safe defaults
- docs that currently mention Cameron-local paths/services
- canonical public-surface alignment for `src/mcp_memory_server.py` and the five `cami_*` tools

- [ ] **Step 3: Assign `retrieval-ranking` branch scope**

Ownership:
- `src/memory_retriever.py`
- related benchmark scaffolding
- retrieval-facing tests and docs
- only minimal MCP-layer formatting/debug additions after rebasing on `schema-safety`

## Chunk 3: Branch Execution Requirements

### Task 4: `tests-ci` branch

**Files:**
- Create: `tests/...`
- Create: `.github/workflows/python-tests.yml`
- Modify: public docs as needed

- [ ] **Step 1: Write failing tests for current offline-safe behavior**
- [ ] **Step 2: Run them and verify they fail for the right reasons**
- [ ] **Step 3: Add minimal test harness and CI workflow**
- [ ] **Step 4: Run the test suite locally**
- [ ] **Step 5: Write `RESULTS.md` summarizing coverage and gaps**
- If used, `RESULTS.md` must remain untracked.
- [ ] **Step 6: Commit with a focused message**

### Task 5: `schema-safety` branch

**Files:**
- Modify: `src/memory_worker.py`
- Modify: `src/unified_vector_store.py`
- Modify: `hooks/*.sh`
- Modify: `README.md`
- Modify: `docs/04-OPERATIONS.md`
- Create tests as needed

- [ ] **Step 1: Write failing tests for config validation/public-safe defaults where practical**
- [ ] **Step 2: Remove Cameron-local assumptions and production-looking default secrets**
- [ ] **Step 3: Add schema/config validation with clear error behavior**
- [ ] **Step 4: Rewrite docs to use generic public-safe environment guidance**
- [ ] **Step 5: Run targeted tests and text scans for private references**
- [ ] **Step 6: Write `RESULTS.md` and commit**
- If used, `RESULTS.md` must remain untracked.

### Task 6: `retrieval-ranking` branch

**Files:**
- Modify: `src/memory_retriever.py`
- Possibly modify: `src/unified_vector_store.py`
- Create: benchmark scaffolding under a new public-safe test/fixtures path
- Modify docs as needed

- [ ] **Step 1: Write failing tests for ranking/dedup behavior**
- [ ] **Step 2: Tighten score calibration and source fusion**
- [ ] **Step 3: Add benchmark scaffolding and fixture format for future retrieval evaluation**
- [ ] **Step 4: Run targeted retrieval tests locally**
- [ ] **Step 5: Write `RESULTS.md` and commit**
- If used, `RESULTS.md` must remain untracked.

## Chunk 4: Integration and Safety Verification

### Task 7: Review all branches before PR creation

**Files:**
- Inspect all three worktrees

- [ ] **Step 1: Check each branch diff for scope discipline**

Run:
```bash
git -C /Users/cameronbennion/tmp-claude-cortex/.worktrees/tests-ci status --short
git -C /Users/cameronbennion/tmp-claude-cortex/.worktrees/schema-safety status --short
git -C /Users/cameronbennion/tmp-claude-cortex/.worktrees/retrieval-ranking status --short
```

- [ ] **Step 2: Scan each branch for private references**

Run a shared `rg` scan for:
- `/Users/cameronbennion`
- `~/.openclaw`
- `~/clawd`
- literal API keys / bearer tokens / phone numbers

- [ ] **Step 3: Run branch-local verification commands**

Expected:
- `tests-ci`: CI-targeted test command passes
- `schema-safety`: targeted tests and safety scans pass
- `retrieval-ranking`: targeted retrieval tests pass

### Task 8: Prepare PR-ready state

**Files:**
- none required beyond branch commits

- [ ] **Step 1: Summarize each PR**
- [ ] **Step 2: Confirm merge order**
- Merge order:
  1. `schema-safety`
  2. `tests-ci`
  3. `retrieval-ranking`
- [ ] **Step 3: Push branches and open PRs only after private-reference scan is clean**

Plan complete and saved to `docs/superpowers/plans/2026-03-13-public-hardening-rollout.md`. Ready to execute?
