# Claude Cortex

**Persistent memory for Claude Code. Every session picks up where the last one left off.**

Claude Code forgets everything between sessions. Cortex fixes that — it gives Claude searchable memory, automatic context loading, a knowledge graph, and adaptive tool-dispatch optimization. Modeled after how the human brain actually organizes memory.

---

## What It Does

```
  ┌──────────────────────────────────────────────────────────┐
  │                   CLAUDE CODE SESSION                    │
  │                                                          │
  │  Knows: current time, active goals, recent work,         │
  │  pending items, relationships, optimized routes          │
  └────────────────────────┬─────────────────────────────────┘
                           │
              ┌────────────┴────────────┐
              ▼                         ▼
  ┌────────────────────┐   ┌─────────────────────────────┐
  │   MEMORY SYSTEM    │   │  ADAPTIVE INFERENCE         │
  │                    │   │  ROUTING (AIR)              │
  │  • Observations    │   │                             │
  │  • Search          │   │  Observes tool-call patterns│
  │  • Knowledge Graph │   │  Learns optimized shortcuts │
  │  • Working Memory  │   │  Injects routes into context│
  │  • Session History │   │  Gets faster over time      │
  └─────────┬──────────┘   └──────────────┬──────────────┘
            │                             │
            └──────────┬──────────────────┘
                       ▼
         ┌───────────────────────────┐
         │    4 LIFECYCLE HOOKS      │
         │                           │
         │  Session Start → Context  │
         │  Tool Use    → Capture    │
         │  User Prompt → Hint       │
         │  Session End → Summarize  │
         └───────────────────────────┘
```

**9 memory layers**, each inspired by a different part of human cognition:

| Layer | Inspired By | Purpose |
|-------|------------|---------|
| **Adaptive Inference Routing** | **Motor learning** | **Learns tool-call patterns, eliminates unnecessary lookups, gets faster per user over time** |
| Observation Pipeline | Procedural memory | Captures every tool use and prompt automatically |
| Session Bootstrap | Prospective memory | Loads recent context, goals, and pending work on startup |
| Auto Memory | Long-term memory | Permanent notes Claude always sees (MEMORY.md) |
| Working Memory | Mental scratchpad | Tracks goals and state; survives context compression |
| Episodic Memory | Autobiographical recall | Searchable index of all past sessions |
| Hybrid Search | Associative recall | Combines keyword, vector, and graph search |
| Knowledge Graph | Semantic memory | Entity relationships (people, projects, systems) |
| RLM-Graph | Chunking | Partitions complex queries using graph topology |

Each layer is independent — implement any one without the others.

---

## Get Started

```bash
git clone https://github.com/YoungMoneyInvestments/claude-cortex.git
cd claude-cortex
pip install -r requirements.txt
```

Then follow the [Quick Start Guide](docs/03-QUICK-START.md) to wire up hooks and start capturing memory. Takes about 15 minutes.

For AIR (Adaptive Inference Routing), set `AIR_CLASSIFIER_MODE=local` for zero-cost local classification, or `AIR_CLASSIFIER_MODE=api` with your `ANTHROPIC_API_KEY` for higher accuracy.

---

## Documentation

| Guide | What It Covers |
|-------|---------------|
| [Quick Start](docs/03-QUICK-START.md) | Get running in 15-30 minutes |
| [Architecture Overview](docs/01-ARCHITECTURE-OVERVIEW.md) | Layer design, data flow, configuration |
| [Implementation Guide](docs/02-IMPLEMENTATION-GUIDE.md) | Step-by-step code for every layer |
| [AIR Specification](docs/superpowers/specs/adaptive-inference-routing.md) | Full Adaptive Inference Routing framework |

---

## Requirements

- Claude Code CLI
- Python 3.10+
- Dependencies: `pip install -r requirements.txt`

---

## Recent Updates

- **Adaptive Inference Routing (AIR)** — Learns tool-call patterns, eliminates unnecessary lookups, gets faster per user over time
- **Improved retrieval ranking** — Checked-in ranking fixtures and benchmark runner
- **Public-safe defaults** — Generic `CORTEX_*` env vars, no machine-specific assumptions
- **Offline test suite + CI** — GitHub Actions workflow for the public `src/` surface

---

## License

MIT
