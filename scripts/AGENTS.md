# LLM Cortex Scripts

Operational and migration scripts for context loading, memory migration, and Obsidian promotion.

## Rules

- Prefer dry-run flags before writes when available.
- Do not mutate canonical memory or Obsidian notes without clear user intent.
- Keep scripts idempotent where possible; context bootstrap should fail loudly on missing prerequisites.

## Verification

Run focused script with `--dry-run` when available and report exact command/output.
