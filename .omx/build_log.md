# BUILD LOG — llm-cortex

## Session: 2026-04-18 14:47 CDT
**Status**: PLANNING
**Mode**: selective Cortex-to-Obsidian bridge
**RALPLAN**: v1 — promote curated Cortex summaries into Obsidian without mirroring the full memory store

### What happened this session
- Confirmed current architecture already separates machine memory (`~/.cortex` SQLite/vector/graph stores) from curated Obsidian context.
- Verified Obsidian integration exists today for read-path bootstrap via `context_loader.py` and project-note generation via `map_projects.py`.
- Verified `LLMCortex` Obsidian folder exists and `sessions/` is currently empty.
- Chosen scope: add a safe promotion command that exports project-matched session summaries and checked-in markdown reports into the project vault folder.
- Next action: implement shared Obsidian bridge helpers, promotion CLI, tests, execute promotion for `llm-cortex`, then adversarially review.

## Session: 2026-04-18 16:10 CDT
**Status**: COMPLETE
**Mode**: selective Cortex-to-Obsidian bridge
**RALPLAN**: v1 — promote curated Cortex summaries into Obsidian without mirroring the full memory store

### What happened this session
- Added shared vault resolution helpers in `scripts/obsidian_bridge.py`.
- Refactored `scripts/context_loader.py` to reuse the shared vault mapping instead of carrying a duplicate copy.
- Added `scripts/promote_to_obsidian.py` to export project-focused session summaries into `vault/sessions/` and markdown reports into `vault/research/`.
- Added shared timeout-guarded Obsidian reads, atomic note writes, managed-note pruning, repo-root canonicalization, and deterministic session ordering/tie-breaks.
- Expanded regression coverage to 37 unit tests covering vault routing, project matching, bootstrap selection, stale-note pruning, git-root resolution, and fail-closed edge cases.
- Executed the bridge for `llm-cortex`; `LLMCortex/sessions/` now contains one promoted session note and `LLMCortex/research/` contains two promoted markdown reports.
- Verified live no-op behavior with `python3 scripts/promote_to_obsidian.py --project-dir /Users/cameronbennion/Projects/llm-cortex --dry-run` returning `changed=0` for all promoted items.
- Passed repeated adversarial review loops; final fresh reviewer reported `no findings`.
- Next action: commit and push the branch-local change set.

## Session: 2026-04-19 21:41 CDT
**Status**: COMPLETE
**Mode**: targeted hardening
**RALPLAN**: v2 — fix session_bootstrap context_loader path resolution without changing bridge behavior

### What happened this session
- Verified a concrete live defect in `src/mcp_memory_server.py`: `session_bootstrap` looked for `src/context_loader.py`, but the repo layout stores it at `scripts/context_loader.py`.
- Implemented `_resolve_context_loader_script()` to support both layouts safely: installed flat `scripts/` deployments and source-repo `src/ + scripts/` checkouts.
- Added regression tests covering both layouts and verifying `session_bootstrap` invokes the resolved script path.
- Verified targeted suites: `tests/test_mcp_server_coverage.py` (`22 passed`) and `tests/test_context_loader.py` (`2 passed`).
- Verified direct functional call now succeeds: `handle_tool_call('session_bootstrap', {'hours': 1})` returned real bootstrap output instead of a missing-file stderr.
- Next action: commit and push only the targeted hardening change; do not touch the dirty `~/clawd` checkout without a separate deploy decision.
