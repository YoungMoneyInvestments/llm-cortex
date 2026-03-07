# Claude Cortex: Operations Guide
### What runs, where, and why

---

## Background Processes

### 1. Memory Worker (`memory_worker.py`)
- **What:** FastAPI service that captures, compresses, and indexes all Claude Code activity
- **Port:** 37778 (localhost only)
- **Managed by:** launchd (`com.cortex.memory-worker`)
- **Auto-starts:** Yes, on login. Auto-restarts on crash.
- **RAM:** ~100MB idle
- **Logs:** `~/.openclaw/logs/memory-worker.log`

**Sub-systems running inside the worker:**

| Sub-system | What it does | Runs every |
|------------|-------------|------------|
| **Observation Processor** | Picks up pending observations, generates AI or rule-based summaries, syncs to vector store | 5 seconds |
| **AI Compressor** | Sends high-signal observations to CamiRouter/Anthropic for semantic compression | Per observation (with rate control) |
| **NER Extractor** | Extracts people, projects, tools, files, companies from observations via regex patterns | Per high-signal observation |
| **Session Summarizer** | Generates session summaries when sessions end (key decisions, entities, files changed) | On session end + 5-min catch-up loop |
| **Retention Manager** | Deletes old low-signal data (7d+), trims raw fields on old high-signal data (30d+) | Every hour |
| **Watchdog** | Monitors all sub-systems, restarts any that crash | Every 30 seconds |

### 2. Hook Scripts (in `hooks/`)
- **What:** Shell scripts that fire on Claude Code lifecycle events
- **Triggered by:** Claude Code hooks system (configured in `.claude/settings.json`)
- **They do NOT run continuously** — they fire, POST to the worker, and exit

| Hook | Trigger | What it sends |
|------|---------|--------------|
| `post-tool-use.sh` | After every tool call | Tool name, input, output |
| `user-prompt.sh` | When you type a prompt | Your prompt text, registers session |
| `session-end.sh` | When a session ends | Session ID (triggers summarization) |
| `start-worker.sh` | On session start | Ensures worker is running (redundant with launchd) |

### 3. MCP Memory Server (`mcp_memory_server.py`)
- **What:** Exposes memory search as MCP tools for Claude/Cami
- **Runs:** On-demand (spawned by MCP client when tools are called)
- **Not a background process** — starts and stops with each MCP session
- **Tools provided:** `cami_memory_search`, `cami_memory_timeline`, `cami_memory_details`, `cami_memory_save`, `cami_memory_graph_search`

---

## Data Stores

| Database | Location | Size (typical) | What's in it |
|----------|----------|---------------|-------------|
| **Observations DB** | `~/clawd/data/cortex-observations.db` | 50-200MB | All observations, sessions, session summaries |
| **Vector DB** | `~/clawd/data/cortex-vectors.db` | 50-150MB | FTS5 index + 384-dim embeddings for semantic search |
| **Knowledge Graph DB** | `~/clawd/data/cortex-knowledge-graph.db` | 1-5MB | Entities, relationships, aliases |

**Estimated growth:** ~10-20MB/month with retention cleanup active. Retention auto-prunes so databases plateau rather than grow unbounded.

---

## Automatic Cleanup (Retention)

The retention manager runs hourly inside the worker:

| Data age | What happens |
|----------|-------------|
| 0-7 days | Everything kept |
| 7-30 days | Low-signal observations deleted (Read, Glob, Grep, Bash, TaskOutput) + their vector embeddings |
| 30+ days | Raw input/output nulled on remaining observations. Summaries preserved forever. |

---

## Managing the Worker

```bash
# Check if running
launchctl list | grep cortex

# Check health
TIRITH=0 curl -s http://127.0.0.1:37778/api/health | python3 -m json.tool

# Restart
launchctl kickstart -k gui/$(id -u)/com.cortex.memory-worker

# Stop temporarily
launchctl kill SIGTERM gui/$(id -u)/com.cortex.memory-worker

# View logs
tail -50 ~/.openclaw/logs/memory-worker.log

# Check AI compression status
TIRITH=0 curl -s http://127.0.0.1:37778/api/compression/status | python3 -m json.tool

# Check NER stats
TIRITH=0 curl -s http://127.0.0.1:37778/api/ner/stats | python3 -m json.tool

# Check retention stats
TIRITH=0 curl -s http://127.0.0.1:37778/api/retention/stats | python3 -m json.tool

# Manual retention run (dry run first)
TIRITH=0 curl -s -X POST "http://127.0.0.1:37778/api/retention/run?dry_run=true" | python3 -m json.tool
```

## Re-embedding (after DB reset or model change)

```bash
cd ~/clawd && source venv/bin/activate
python scripts/unified_vector_store.py backfill
```

---

## Configuration (Environment Variables)

| Variable | Default | What it controls |
|----------|---------|-----------------|
| `CORTEX_NER_ENABLED` | `true` | Enable/disable automatic entity extraction |
| `CORTEX_WORKER_API_KEY` | `cortex-local-2026` | Bearer token for POST endpoints |
| `CORTEX_EMBEDDING_PROVIDER` | `local` | `local` (free, sentence-transformers) or `openai` (paid) |

---

## launchd Plist

Location: `~/Library/LaunchAgents/com.cortex.memory-worker.plist`

- `RunAtLoad: true` — starts on login
- `KeepAlive.SuccessfulExit: false` — restarts on crash (non-zero exit)
- `ThrottleInterval: 10` — waits 10s between restart attempts
- Logs to `~/.openclaw/logs/memory-worker-stdout.log` and `memory-worker-stderr.log`

---

*Last updated: 2026-03-07*
