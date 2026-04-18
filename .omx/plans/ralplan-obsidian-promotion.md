## RALPLAN: Selective Obsidian Promotion
Version: v1 | Date: 2026-04-18 | Status: APPROVED

### What We're Building
Add a narrow Cortex-to-Obsidian promotion path that exports only curated, project-relevant session summaries and markdown reports into each project's Obsidian vault folder.

This keeps `llm-cortex` as the system of record while making high-signal artifacts visible to humans and automatically reusable by the existing Obsidian bootstrap path.

### Architecture Decision
Do not mirror the whole Cortex database into Obsidian.

Instead, build a selective promotion bridge:
- raw observations, vectors, and graph stay in Cortex
- promoted summaries/reports go to Obsidian
- existing bootstrap continues to read Obsidian back into agent context

### Module Breakdown
- [ ] Obsidian bridge helper: centralize vault path resolution and project markers
- [ ] Promotion CLI: export project-matched session summaries to `vault/sessions/`
- [ ] Report sync: copy repo markdown reports into `vault/research/`
- [ ] Bootstrap refactor: make `context_loader.py` reuse the shared bridge helper
- [ ] Verification: add tests and run real promotion against `llm-cortex`

### Execution Mode
$ralph — sequential, single-owner change across one repo with shared helper + CLI + docs/tests

### Integration Points
- `scripts/context_loader.py`
- new shared helper under `src/` or `scripts/`
- new promotion CLI under `scripts/`
- Obsidian vault path under `~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Cameron/LLMCortex`
- local Cortex DB at `~/.cortex/data/cortex-observations.db`

### Done Looks Like
There is a documented CLI that can:
- resolve the correct project vault folder
- export recent project-relevant session summaries into `sessions/`
- sync checked-in markdown reports into `research/`
- run successfully for `llm-cortex`

And tests pass for the filtering/rendering logic.

### Risks & Mitigations
- Risk: wrong sessions promoted into the wrong project vault -> Mitigation: conservative project-marker matching, dry-run support, project-specific execution
- Risk: duplicate or noisy exports -> Mitigation: deterministic filenames, content hashing/overwrite only when changed, minimum observation threshold
- Risk: drift between bootstrap mapping and promotion mapping -> Mitigation: shared helper module for vault resolution

### Open Questions
None blocking. User explicitly approved planning and execution.
