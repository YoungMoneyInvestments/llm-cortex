# LLM Cortex

**Your AI coding assistant forgets everything at the end of every session. LLM Cortex fixes that.**

It gives your LLM a real memory system — modeled after how the human brain organizes memory — so it picks up exactly where you left off, every time. Works with Claude Code, Codex, and any LLM agent that supports hooks.

---

## Why Would You Want This?

If you use LLMs seriously — on real projects, running long autonomous sessions overnight — the amnesia problem costs you every single day. You re-explain context, repeat preferences, and watch your assistant make the same mistakes it made yesterday.

LLM Cortex turns your LLM from a powerful-but-amnesiac assistant into something that actually feels like it's been on your team for months.

---

## How It Works

**8 memory layers**, each inspired by a different part of human cognition:

| Layer | Inspired By | Purpose |
|-------|------------|---------|
| Observation Pipeline | Procedural memory | Captures every tool use and prompt automatically |
| Session Bootstrap | Prospective memory | Loads recent context, goals, and pending work on startup |
| Auto Memory | Long-term memory | Permanent notes your LLM always sees (MEMORY.md) |
| Working Memory | Mental scratchpad | Tracks goals and state; survives context compression |
| Episodic Memory | Autobiographical recall | Searchable index of all past sessions |
| Hybrid Search | Associative recall | Combines keyword, vector, and graph search |
| Knowledge Graph | Semantic memory | Entity relationships (people, projects, systems) |
| RLM-Graph | Chunking | Partitions complex queries using graph topology |

Each layer is independent — implement any one without the others.

---

## Get Started

```bash
git clone https://github.com/YoungMoneyInvestments/llm-cortex.git
cd llm-cortex
pip install -r requirements.txt
```

Then follow the [Quick Start Guide](docs/03-QUICK-START.md) to wire up hooks and start capturing memory. Takes about 15 minutes.

---

## Multi-LLM Support

LLM Cortex isn't locked to one agent. Run Claude Code, Codex, Cursor, and Gemini against the same memory database — each one tags its writes so you can filter by who wrote what.

```bash
# Set per-agent identity via environment variable
CORTEX_AGENT_NAME=claude-code  # in Claude Code hooks
CORTEX_AGENT_NAME=codex        # in Codex MCP config
CORTEX_AGENT_NAME=cursor       # in Cursor MCP config
```

See [Multi-LLM Setup](docs/05-MULTI-LLM-SETUP.md) for full configuration examples.

---

## Documentation

| Guide | What It Covers |
|-------|---------------|
| [Quick Start](docs/03-QUICK-START.md) | Get running in 15-30 minutes |
| [Architecture Overview](docs/01-ARCHITECTURE-OVERVIEW.md) | Layer design, data flow, configuration |
| [Implementation Guide](docs/02-IMPLEMENTATION-GUIDE.md) | Step-by-step code for every layer |
| [Multi-LLM Setup](docs/05-MULTI-LLM-SETUP.md) | Claude Code, Codex, Cursor, Gemini — one shared brain |

---

## Requirements

- Python 3.10+
- An LLM agent with hook support (Claude Code, Codex, or similar)
- Dependencies: `pip install -r requirements.txt`

---

## How This Compares

There are other memory tools out there. Here's what makes LLM Cortex different:

| Project | What It Does | What's Missing |
|---------|-------------|----------------|
| [Mem0](https://mem0.ai/) | Generic memory layer for AI apps ($24M funded) | Flat memory store — no layered architecture, no brain-inspired design |
| [Claude-Mem](https://github.com/thedotmack/claude-mem) | Claude Code session capture plugin | Single-layer capture, no knowledge graph, no hybrid search |
| [Memory-MCP](https://github.com/yuvalsuede/memory-mcp) | Two-tier MCP memory server | No observation pipeline, no working memory |
| [Hindsight](https://hindsight.vectorize.io/) | MCP retain/recall/reflect | No session bootstrap, no knowledge graph |

LLM Cortex is the only project that combines brain-inspired layered memory in one system. Each piece works independently, but together they compound.

---

## Contributing

Contributions are welcome. If you're interested in improving memory retrieval, adding support for new LLM agents, or anything else — open an issue or submit a PR.

See the [Architecture Overview](docs/01-ARCHITECTURE-OVERVIEW.md) to understand how the layers connect before diving in.

---

## Recent Updates

- **Model-agnostic design** — Works with Claude Code, Codex, and any hook-compatible LLM agent
- **Improved retrieval ranking** — Checked-in ranking fixtures and benchmark runner
- **Public-safe defaults** — Generic `CORTEX_*` env vars, no machine-specific assumptions

---

## License

MIT License. See [LICENSE](LICENSE) for details.
