# Public Hardening Rollout Design

**Goal:** harden the public `claude-cortex` repo against the still-valid critiques without leaking local/private assumptions, while keeping the rollout small enough to ship as three focused PRs.

## Scope

This rollout is intentionally limited to three parallel tracks:

1. `tests-ci`
Add an initial automated verification surface for the current public implementation.

2. `schema-safety`
Add validation and remove local-specific assumptions from the public repo.

3. `retrieval-ranking`
Improve current retrieval calibration and add benchmark scaffolding for future Retrieval v2 work.

The rollout does **not** implement full Retrieval v2. That remains separate architecture work already captured in the Retrieval v2 spec.

## Constraints

- No private machine paths, tokens, phone numbers, or local service assumptions may be introduced into the public repo.
- Existing public MCP tools remain stable.
- Keep markdown sprawl minimal: this file is the canonical rollout spec.
- Prefer incremental hardening over broad refactors.

## PR 1: `tests-ci`

**Purpose:** address the strongest public-repo weakness: no automated trust surface.

Deliverables:
- add a minimal Python test suite covering current public modules that are practical to exercise offline
- add GitHub Actions CI for tests and basic lint/format checks
- avoid tests that depend on private local services, local launchd setup, or live external credentials

Success criteria:
- a contributor can run the test suite in a clean environment
- CI runs on pull requests and `main`

## PR 2: `schema-safety`

**Purpose:** address schema brittleness and public-repo leakage risk in one branch.

Deliverables:
- add explicit request/config/data validation where the public surface is currently ad hoc
- replace or remove local-specific defaults and documentation assumptions
- ensure the public repo documents generic environment-based configuration rather than Cameron-local wiring

Examples of issues to remove or neutralize:
- `~/.openclaw` log paths in public docs
- `~/clawd/.env.local` assumptions in public code/docs
- default bearer token strings that look production-like
- hard-coded local router assumptions as public defaults

Success criteria:
- public docs are generic and safe
- code paths fail clearly when config is missing instead of silently assuming Cameron-local layout

## PR 3: `retrieval-ranking`

**Purpose:** improve the current retrieval path without pretending Retrieval v2 already exists.

Deliverables:
- tighten ranking normalization and score fusion in the current retriever/vector path
- make source contributions more inspectable
- add benchmark scaffolding and fixtures for future retrieval comparisons

This branch should stay within the current public tool surface:
- `cami_memory_search`
- `cami_memory_timeline`
- `cami_memory_details`
- `cami_memory_save`
- `cami_memory_graph_search`

Success criteria:
- ranking behavior is more deterministic and explainable
- benchmark scaffolding exists in the public repo for iterative improvement

## Merge Order

Recommended merge order:

1. `tests-ci`
2. `schema-safety`
3. `retrieval-ranking`

Rationale:
- CI first gives immediate protection for later PRs
- schema/safety then removes public leakage risk
- retrieval/ranking merges last onto a safer and better-tested base

## Worktree Strategy

Use three worktrees from the clean public clone:

- `tests-ci`
- `schema-safety`
- `retrieval-ranking`

Each branch should:
- own a disjoint primary file set where possible
- include a concise `RESULTS.md`
- avoid reverting unrelated changes from sibling branches

## Review Standard

For each PR:
- verify no local/private references are introduced
- run local verification commands in the worktree
- summarize residual risks explicitly

## Non-Goals

- no full rewrite into Retrieval v2 yet
- no migration of local private datasets into the public repo
- no broad redesign of every module in one pass

## Decision

Proceed with the three-PR rollout above, with sanitization folded into `schema-safety` rather than split into a separate prep PR.
