# LLM Cortex Navigation

Use this map before broad search. LLM Cortex is the memory system for hook-based coding agents, with capture, bootstrap, search, graph, Obsidian promotion, and migration helpers.

## Start Here

| Need | Open |
|------|------|
| Project overview | `README.md` |
| Root agent rules | `AGENTS.md` |
| Architecture | `docs/01-ARCHITECTURE-OVERVIEW.md` |
| Implementation guide | `docs/02-IMPLEMENTATION-GUIDE.md` |
| Quick start | `docs/03-QUICK-START.md` |
| Multi-agent setup | `docs/05-MULTI-LLM-SETUP.md` |
| Source guide | `src/AGENTS.md` |
| Scripts guide | `scripts/AGENTS.md` |
| Hooks guide | `hooks/AGENTS.md` |
| Tests guide | `tests/AGENTS.md` |

## Top-Level Map

| Path | Purpose |
|------|---------|
| `src/` | MCP memory server and runtime modules |
| `scripts/` | Context loader, migrations, Obsidian promotion, maintenance helpers |
| `hooks/` | Agent hook integration scripts |
| `context/` | YAML/static context inputs |
| `docs/` | Architecture and setup docs |
| `tests/` | Pytest suite and fixtures |
| `benchmarks/` | Retrieval/ranking benchmarks |
| `reports/` | Generated/audit reports |

## Commands

| Need | Command |
|------|---------|
| Tests | `python -m pytest tests/` |
| Context bootstrap | `python3 scripts/context_loader.py --hours 48` |
| Obsidian promote dry run | `python3 scripts/promote_to_obsidian.py --project-dir "$PWD" --dry-run` |

## Search Rules

- memory server behavior: `rg "<name>" src tests`
- bootstrap/context loading: `rg "<name>" scripts/context_loader.py context tests`
- hooks: `rg "<name>" hooks scripts tests`
- Obsidian bridge: `rg "<name>" scripts/promote_to_obsidian.py docs tests`

Avoid first-pass search in `.pytest_cache/`, `.playwright-cli/`, `.coverage`, logs, generated reports, and agent runtime state.
