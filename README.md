# LLM Cortex

**Your AI coding assistant forgets everything at the end of every session. LLM Cortex fixes that.**

It gives your LLM a real memory system — modeled after how the human brain organizes memory — so it picks up exactly where you left off, every time. Works with Claude Code, Codex, and any LLM agent that supports hooks.

---

## The Habits Problem

There's a second problem nobody talks about: even *within* a session, your LLM wastes time and money on mistakes it's made hundreds of times before.

You say "commit the changes" and instead of just running `git commit`, it tries to find a skill called "commit," fails, tries another variant, fails again, then finally does the thing it should have done immediately. Every. Single. Session.

[removed]

What that means in practice:
- Your LLM stops fumbling on phrases you use constantly
- Sessions start faster because less time is wasted on retry loops
- You burn fewer tokens on errors that shouldn't happen
- The longer you use it, the better it gets — it's genuinely learning your style
- It works completely locally if you want. No API calls, no data leaving your machine, no ongoing cost

---

## Why Would You Want This?

If you use LLMs seriously — on real projects, running long autonomous sessions overnight — the waste from repeated small failures adds up fast. It's the difference between working with a tool that's gotten to know you versus one that meets you for the first time every single day.

[removed]

---

## How It Works

<p align="center">
  <img src="docs/llm_cortex_architecture.svg" alt="LLM Cortex Architecture" width="680">
</p>

**9 memory layers**, each inspired by a different part of human cognition:

| Layer | Inspired By | Purpose |
|-------|------------|---------|
[removed]
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

[removed]

---

## Documentation

| Guide | What It Covers |
|-------|---------------|
| [Quick Start](docs/03-QUICK-START.md) | Get running in 15-30 minutes |
| [Architecture Overview](docs/01-ARCHITECTURE-OVERVIEW.md) | Layer design, data flow, configuration |
| [Implementation Guide](docs/02-IMPLEMENTATION-GUIDE.md) | Step-by-step code for every layer |
[removed]

---

## Requirements

- Python 3.10+
- An LLM agent with hook support (Claude Code, Codex, or similar)
- Dependencies: `pip install -r requirements.txt`

---

## Recent Updates

[removed]
- **Model-agnostic design** — Works with Claude Code, Codex, and any hook-compatible LLM agent
- **Improved retrieval ranking** — Checked-in ranking fixtures and benchmark runner
- **Public-safe defaults** — Generic `CORTEX_*` env vars, no machine-specific assumptions

---

## License

MIT
