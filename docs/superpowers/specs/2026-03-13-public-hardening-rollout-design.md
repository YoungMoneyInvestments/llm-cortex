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

## Canonical Public Surface

For this rollout, the canonical public MCP entrypoint is `src/mcp_memory_server.py`.

The canonical public tools are:
- `cami_memory_search`
- `cami_memory_timeline`
- `cami_memory_details`
- `cami_memory_save`
- `cami_memory_graph_search`

Older `scripts/` mirrors and stale docs are treated as publication drift. Aligning them to the canonical `src/` public surface is part of the hardening work.

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
- target the canonical `src/` public surface rather than legacy `scripts/` mirrors

Success criteria:
- a contributor can run the test suite in a clean environment
- CI runs on pull requests and `main`

## PR 2: `schema-safety`

**Purpose:** address schema brittleness and public-repo leakage risk in one branch.

Deliverables:
- add explicit request/config/data validation where the public surface is currently ad hoc
- replace or remove local-specific defaults and documentation assumptions
- ensure the public repo documents generic environment-based configuration rather than Cameron-local wiring
- align docs/examples to the canonical `src/` MCP entrypoint and `cami_*` tool names

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

Primary ownership:
- `src/memory_retriever.py`
- retrieval tests/fixtures/benchmarks

Shared-boundary rule:
- if this PR needs MCP-layer edits, they must be limited to retrieval-result formatting/debug output against the canonical `src/mcp_memory_server.py` surface after rebasing on `schema-safety`

Success criteria:
- ranking behavior is more deterministic and explainable
- benchmark scaffolding exists in the public repo for iterative improvement

## Merge Order

Recommended merge order:

1. `schema-safety`
2. `tests-ci`
3. `retrieval-ranking`

Rationale:
- schema/safety first removes public leakage risk and establishes the canonical public boundary
- CI then locks in sanitized public behavior
- retrieval/ranking merges last onto a safer and better-tested base

## Worktree Strategy

Use three worktrees from the clean public clone:

- `tests-ci`
- `schema-safety`
- `retrieval-ranking`

Each branch should:
- own a disjoint primary file set where possible
- keep any worktree-local execution notes in an untracked `RESULTS.md`
- avoid reverting unrelated changes from sibling branches

Boundary ownership:
- `schema-safety` owns canonical public-surface alignment plus config validation at the MCP/config boundary
- `retrieval-ranking` owns retrieval internals and benchmark scaffolding
- `tests-ci` owns CI/test harness and may add tests for the other branches, but should not become the branch that defines runtime semantics

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
