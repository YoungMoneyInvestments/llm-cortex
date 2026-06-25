# LLM Cortex Hooks

Agent hook integration scripts.

## Rules

- Hook failures should not corrupt memory state.
- Avoid noisy or expensive hook behavior; hooks run in developer workflows.
- Preserve per-agent identity tagging.

## Verification

Exercise hook scripts directly with representative env vars/input when changing behavior.
