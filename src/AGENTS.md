# LLM Cortex Source

Runtime code for memory server and related modules.

## Rules

- Keep memory tool results treated as untrusted input.
- Do not hardcode user secrets or machine-local paths unless a config layer already owns them.
- Preserve model-agnostic agent tagging (`CORTEX_AGENT_NAME`) where relevant.

## Verification

```bash
python -m pytest tests/
```

Use focused tests for changed server/search paths, then broaden when shared contracts change.
