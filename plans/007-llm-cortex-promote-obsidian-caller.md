# Plan 007 — Verify whether `promote_to_obsidian.py` has a live caller (llm-cortex)

- **Repo:** `/Users/cameronbennion/Projects/llm-cortex`
- **Branch / commit:** `main` (current SHA — stamp with `git -C ~/Projects/llm-cortex rev-parse --short HEAD` at execution)
- **Leverage:** LOW · **Risk:** LOW · **Effort:** S · **Gate:** none
- **Confidence:** PARTIAL — this is **INVESTIGATE-FIRST**, not a confirmed defect. May resolve to "no action needed."

## Why this is investigate-first
On `main`, `scripts/promote_to_obsidian.py` (582 LOC per the triage) and `tests/test_promote_to_obsidian.py` exist, but nothing **imports or calls** it in-process (`git grep "import promote_to_obsidian\|promote_to_obsidian("` → empty besides its own def/test). The triage agent flagged it as possibly orphaned because the session-end hook that drives it lives only on a branch (`fix/health-report-exit-code`, plan-worthy separately). **BUT** it's a `scripts/` CLI — it may be invoked externally via `python scripts/promote_to_obsidian.py` from cron, launchd, a git hook, or another repo. Absence of an in-process caller ≠ dead code. **Verify before doing anything.**

## Steps
1. **Search every external invocation surface** for the script name:
   ```
   # cron / launchd / shell scripts / hooks anywhere on the machine
   grep -rl "promote_to_obsidian" ~/Library/LaunchAgents/ 2>/dev/null
   crontab -l 2>/dev/null | grep -i promote_to_obsidian
   grep -rl "promote_to_obsidian" ~/Projects ~/clawd ~/.claude 2>/dev/null | grep -v "/llm-cortex/scripts/\|/llm-cortex/tests/"
   # is it referenced by the obsidian session-end hook the triage mentioned?
   git -C ~/Projects/llm-cortex grep -n "promote_to_obsidian" main
   ```
2. **Classify the outcome:**
   - **(i) It IS invoked externally** (cron/launchd/hook/other repo) → NOT orphaned. No code change. Document the caller in a one-line comment at the top of `promote_to_obsidian.py` (`# Invoked by: <path>`) so the next reader doesn't re-flag it. Done.
   - **(ii) Its intended driver is the session-end hook that only exists on `fix/health-report-exit-code`** → the script is stranded waiting for an un-merged caller. The real action is to triage that branch (see the separate plan / triage doc entry for `llm-cortex/fix/health-report-exit-code` MERGE). Record the dependency; do NOT wire a new caller here.
   - **(iii) No caller anywhere, and no pending branch provides one** → genuinely orphaned. Decide with Cameron: wire it into the documented memory→obsidian flow, or remove it + its test. Default recommendation: keep + document as a manual CLI (it's a 582-LOC tool that clearly does real work) rather than delete.
3. Do not modify `promote_to_obsidian.py`'s logic in this plan — this is a wiring/clarity investigation, not a refactor.

## Done criteria
- A written determination (i/ii/iii) recorded in the plan's status and, for (i), a `# Invoked by:` comment added to the script.
- If (iii) and Cameron opts to keep: a documented entry point (README line or Makefile target). If remove: script + test deleted in one commit with rationale.

## Escape hatch
If this turns out to be entirely dependent on the `fix/health-report-exit-code` branch (outcome ii), STOP here and fold it into that branch's merge decision — don't build a parallel caller that the branch would conflict with.

## Maintenance note
`scripts/`-style CLIs with no in-repo caller are easy to mis-flag as dead. A `# Invoked by:` header convention across `llm-cortex/scripts/` would prevent repeat false-positives in future audits.
