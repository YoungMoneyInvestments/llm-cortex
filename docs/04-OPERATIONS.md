# LLM Cortex: Operations Guide
### What runs, where, and how to operate it safely in a public setup

---

## Runtime Components

### 1. Memory Worker (`src/memory_worker.py`)
- **What:** FastAPI service that captures, compresses, and indexes Claude Code activity
- **Port:** `127.0.0.1:${CORTEX_WORKER_PORT:-37778}`
- **Lifecycle:** run it directly or under your own process manager (`launchd`, `systemd`, Docker, etc.)
- **Logs:** `${CORTEX_LOG_DIR:-$HOME/.cortex/logs}/memory-worker.log`
- **Auth:** all POST endpoints require `CORTEX_WORKER_API_KEY`

### 2. Hook Scripts (`hooks/`)
- `session_start.sh`
- `user_prompt_submit.sh`
- `post_tool_use.sh`
- `session_end.sh`

These fire on Claude Code lifecycle events, POST to the worker, and exit immediately.

### 3. MCP Memory Server (`src/mcp_memory_server.py`)
- **What:** stdio MCP server exposing the canonical public `cami_*` tools
- **Runs:** on demand when your MCP client invokes it
- **Tools:** `cami_memory_search`, `cami_memory_timeline`, `cami_memory_details`, `cami_memory_save`, `cami_memory_graph_search`

---

## Data Locations

The public repo defaults to a generic local runtime under `~/.cortex`. Override with env vars if you want a different layout.

| Store | Default location | Notes |
|------|------------------|-------|
| Observations DB | `${CORTEX_DATA_DIR:-$HOME/.cortex/data}/cortex-observations.db` | observations, sessions, summaries |
| Vector DB | `${CORTEX_DATA_DIR:-$HOME/.cortex/data}/cortex-vectors.db` | FTS + optional embeddings |
| Knowledge Graph DB | `${CORTEX_DATA_DIR:-$HOME/.cortex/data}/cortex-knowledge-graph.db` | entities and relationships |
| Worker log | `${CORTEX_LOG_DIR:-$HOME/.cortex/logs}/memory-worker.log` | worker application log |
| PID file | `${CORTEX_PID_FILE:-$HOME/.cortex/worker.pid}` | optional worker PID location |

---

## Required Configuration

| Variable | Required | Purpose |
|----------|----------|---------|
| `CORTEX_WORKER_API_KEY` | no (auto-managed) | shared bearer token between the worker and hook environment |
| `CORTEX_WORKER_PORT` | no | worker port, defaults to `37778` |
| `CORTEX_DATA_DIR` | no | runtime data directory |
| `CORTEX_LOG_DIR` | no | runtime log directory |
| `CORTEX_PID_FILE` | no | PID file path |

### API Key Resolution (DEF-6)

The worker and hook scripts resolve `CORTEX_WORKER_API_KEY` in this order:

1. `CORTEX_WORKER_API_KEY` environment variable (if set and non-empty)
2. Key file at `~/.cortex/data/.worker_api_key` (auto-read by hooks)
3. Auto-generate a new 32-char hex key, write it to the key file, and use it (worker startup only)

**You do not need to set `CORTEX_WORKER_API_KEY` in your shell profile, launchd plist, or supervisor config.** The worker generates and persists the key on first start. Hook scripts read the same file. As long as both the worker and the hooks share the same `CORTEX_DATA_DIR`, the key stays in sync automatically.

Only set `CORTEX_WORKER_API_KEY` explicitly when you need a specific value (e.g., shared with a remote client over a trusted tunnel).

Optional AI compression settings:

| Variable | Required | Purpose |
|----------|----------|---------|
| `CAMIROUTER_URL` | optional | OpenAI-compatible router endpoint |
| `CAMIROUTER_API_KEY` | required if `CAMIROUTER_URL` is set | router auth |
| `CORTEX_AUTH_PROFILES_PATH` | optional | OAuth fallback auth-profiles.json path |
| `OPENAI_API_KEY` | optional | OpenAI embeddings |
| `CORTEX_ENV_FILE` | optional | env file containing `OPENAI_API_KEY=...` |

To override with a specific key (optional), generate one with:

```bash
python3 -c "import secrets; print(secrets.token_hex(16))"
```

Then set `CORTEX_WORKER_API_KEY=<value>` in your environment before starting the worker.

---

## Common Commands

```bash
# Start the worker
python3 src/memory_worker.py

# Health
curl -s http://127.0.0.1:${CORTEX_WORKER_PORT:-37778}/api/health | python3 -m json.tool

# Compression status
curl -s http://127.0.0.1:${CORTEX_WORKER_PORT:-37778}/api/compression/status | python3 -m json.tool

# NER stats
curl -s http://127.0.0.1:${CORTEX_WORKER_PORT:-37778}/api/ner/stats | python3 -m json.tool

# Retention stats
curl -s http://127.0.0.1:${CORTEX_WORKER_PORT:-37778}/api/retention/stats | python3 -m json.tool

# Manual retention run
curl -s -X POST \
  -H "Authorization: Bearer $CORTEX_WORKER_API_KEY" \
  "http://127.0.0.1:${CORTEX_WORKER_PORT:-37778}/api/retention/run?dry_run=true" \
  | python3 -m json.tool

# Tail logs
tail -50 "${CORTEX_LOG_DIR:-$HOME/.cortex/logs}/memory-worker.log"
```

---

## Re-embedding

```bash
python3 src/unified_vector_store.py backfill
```

If you switch embedding providers or dimensions, re-embed after resetting the vector store.

---

## Process Management

This repo does not require a specific supervisor. Use whatever is standard for your environment:
- local shell session
- `launchd`
- `systemd`
- Docker / Compose
- CI job / devcontainer entrypoint

If you use a supervisor, make sure it exports the same `CORTEX_WORKER_API_KEY`, `CORTEX_DATA_DIR`, and `CORTEX_LOG_DIR` that your hooks expect.

---

*Last updated: 2026-03-13*
