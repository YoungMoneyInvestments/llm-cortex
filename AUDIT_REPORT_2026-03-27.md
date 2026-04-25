# llm-cortex Audit Report — 2026-03-27

## 1. Test Results

**12 passed, 6 failed** (18 total tests, 5 subtests within those)

### Passing Tests (12)
- test_knowledge_graph.py (1)
- test_mcp_memory_server.py (1)
- test_memory_retriever.py (1)
- test_memory_retriever_ranking.py (4)
- test_public_src_surface.py (1)
- test_retrieval_benchmark_runner.py (1)
- test_schema_safety.py (3 of 9)
- test_unified_vector_store.py (1)

### Failing Tests (6) — all in test_schema_safety.py

| # | Test | Root Cause | Fix Location |
|---|------|-----------|--------------|
| 1 | `test_memory_worker_defaults_are_generic` | Test expects `~/.cortex/data`, `~/.cortex/logs`, `~/.cortex/worker.pid` but code hardcodes `~/clawd/data`, `~/.openclaw/worker.pid`, `~/.openclaw/logs`. No env var override (`CORTEX_DATA_DIR`, etc.) exists. | **memory_worker.py lines 50-54**: Either add env var overrides or update test expectations to match actual paths. |
| 2 | `test_require_auth_fails_clearly_without_configured_api_key` | Test expects HTTP 503 + detail mentioning `CORTEX_WORKER_API_KEY` when no key is configured, but `require_auth()` returns 401 with generic message. The default key `"cortex-local-2026"` (line 72) means "no key configured" is never actually detected. | **memory_worker.py lines 198-206**: Need a "not configured" sentinel or env check. |
| 3 | `test_observation_request_rejects_invalid_source` | `ObservationRequest.source` is `str` with no validator — accepts any value including `"bogus"`. | **memory_worker.py line 249**: Add a `Literal` type or Pydantic `@field_validator`. |
| 4 | `test_observation_request_requires_tool_name_for_post_tool_use` | No cross-field validation requiring `tool_name` when `source="post_tool_use"`. | **memory_worker.py class ObservationRequest**: Add `@model_validator`. |
| 5 | `test_mcp_tool_call_rejects_invalid_graph_depth` | `handle_tool_call` catches all exceptions generically. The `FakeRetriever` lacks `search_with_context`, so it errors with `AttributeError` instead of a validation message about `graph_depth`. | **mcp_memory_server.py ~line 330**: Add input validation BEFORE calling retriever methods. |
| 6 | `test_mcp_tool_call_rejects_empty_observation_ids` | Same pattern — `FakeRetriever` lacks `get_details`, so `AttributeError` instead of validation. | **mcp_memory_server.py ~line 302**: Add input validation BEFORE calling retriever methods. |

**Summary:** Tests 1-4 are code/test expectation mismatches (code drifted from the generic-path design). Tests 5-6 show missing input validation in the MCP handler.

---

## 2. Bare `except...pass` Audit (10 instances)

### SAFE (6) — Appropriate silent handling

| File | Line | Context | Verdict |
|------|------|---------|---------|
| `memory_retriever.py` | 421 | FTS5 search fails, falls back to LIKE. | **Safe.** Graceful degradation pattern. Could add `logger.debug` for observability. |
| `memory_retriever.py` | 849 | Loading KG aliases table fails (table may not exist). | **Safe.** KG is optional. Logging would help debugging. |
| `memory_worker.py` | 1895 | `except asyncio.CancelledError: pass` during shutdown task cleanup. | **Safe.** Standard asyncio shutdown pattern. Correct and idiomatic. |
| `knowledge_graph.py` | 478 | `except nx.NetworkXNoPath: pass` — no path found between entities. | **Safe.** Expected condition, returns `None` correctly. Not even a bare `except` — catches specific exception. |
| `unified_vector_store.py` | 936 | Callback failure during backfill progress reporting. | **Safe.** Don't crash a long backfill because a progress callback failed. |
| `unified_vector_store.py` | 1548 | `close()` swallows exception on connection close. | **Safe.** Standard cleanup-in-destructor pattern. Acceptable. |

### MODERATE RISK (3) — Should log, not silently swallow

| File | Line | Context | Risk |
|------|------|---------|------|
| `unified_vector_store.py` | 1005 | Failed to load `last_backfill` stats from JSON. | **Moderate.** Corrupted JSON in `backfill_meta` table would be silently ignored. Should `logger.debug` at minimum. |
| `unified_vector_store.py` | 1399 | `except OSError: pass` on `stat()` for DB size. | **Low-Moderate.** File may not exist yet. An `OSError` here is expected in some paths but logging helps. |
| `unified_vector_store.py` | 1412 | Failed to count duplicate hash groups. | **Moderate.** Schema migration issue (missing `text_hash` column) would be silently hidden. Should `logger.debug`. |

### BUG RISK (1) — Silent failure masks real issues

| File | Line | Context | Risk |
|------|------|---------|------|
| `memory_worker.py` | 833 | `except Exception as e: logger.warning(...)` on vector sync. | **Actually logged** — this is NOT a bare `except...pass`. It logs a warning. The `except ImportError: pass` on line 831-833 IS bare but is safe (vector store optional). **No bug here.** |

**Revised count:** The task description mentioned line 833 of memory_worker.py, but that line is actually `pass` after `except ImportError` (line 831-833), which is safe — vector store is an optional dependency.

**Overall verdict:** No bare `except...pass` instances are actively causing bugs today. Three in `unified_vector_store.py` should get `logger.debug` calls to aid debugging of edge cases.

---

## 3. Asyncio Deadlock Assessment

### db_lock Usage Pattern
- `db_lock` is an `asyncio.Lock()` (NOT reentrant — `asyncio.Lock` does NOT support recursive acquisition)
- Created once at startup (line 1851)
- Used as `async with db_lock:` across ~15 locations

### process_pending_observations() Analysis (line 318)
The function acquires `db_lock` in two separate `async with` blocks:
1. **Line 332**: Acquires lock to SELECT pending rows, then RELEASES it
2. **Line 386**: Re-acquires lock (per row) to UPDATE status, then RELEASES it
3. **Lines 424**: Re-acquires lock (in error handler) to mark as failed

**This is SAFE.** The lock is never held recursively — it's acquired, released, then re-acquired. The `await ai_compressor.compress(...)` call (line 367) happens OUTSIDE the lock, which is correct (don't hold DB lock during network I/O).

### Other Callers
All route handlers (`/observe`, `/search`, etc.) use `async with db_lock:` in non-nested fashion. No function acquires the lock and then calls another function that also acquires it.

### Verdict: NO DEADLOCK RISK
The code correctly uses acquire-release-reacquire, never recursive acquisition. The pattern of releasing the lock during AI compression is also good practice.

---

## 4. LanceDB Assessment

- **Zero imports of `lancedb`** anywhere in the codebase
- The vector store uses **SQLite + OpenAI embeddings** (`unified_vector_store.py`), not LanceDB
- **LanceDB is NOT required.** The worker runs fine without it.
- No action needed.

---

## 5. Other Code Quality Findings

### P1 — Missing Input Validation in MCP Handler
`mcp_memory_server.py handle_tool_call()` (line 262) dispatches directly to retriever methods without validating inputs:
- No `graph_depth` range check before calling `search_with_context` (the retriever clamps internally, but the MCP layer should reject invalid values with a clear error)
- No `observation_ids` emptiness check before calling `get_details` (the retriever returns `[]` for empty, but the test expects an error)
- **Fix:** Add validation block at the top of each tool branch.

### P2 — Hardcoded Cameron-Specific Paths
- `memory_worker.py` line 50: `DATA_DIR = Path.home() / "clawd" / "data"` — hardcoded to Cameron's deployment
- `memory_worker.py` line 52-53: PID/log paths use `.openclaw` — not generic
- `memory_worker.py` line 87: `AUTH_PROFILES_PATH` hardcoded to `.openclaw` path
- `mcp_memory_server.py` line 80: `_load_env()` loads from `~/clawd/.env.local`
- **Fix:** Add `CORTEX_DATA_DIR`, `CORTEX_LOG_DIR`, `CORTEX_PID_FILE` env var overrides with generic defaults.

### P2 — Auth Model Has No "Not Configured" State
`require_auth()` (line 198) always has a key (defaults to `"cortex-local-2026"`). There's no way to detect "key not configured" vs "wrong key" — both return 401. If this is open-source software, the default key is a security risk (anyone who reads the source knows the default).

### P3 — ObservationRequest Lacks Validation
- `source` field accepts any string — no enum/literal constraint
- No cross-field validation for `post_tool_use` requiring `tool_name`
- **Fix:** Add `Literal["post_tool_use", "user_prompt", "session_end"]` and a `@model_validator`

### P3 — Missing `logger.debug` on 3 Silent Excepts
See section 2 above. Lines 1005, 1399, 1412 of `unified_vector_store.py`.

### P4 — Global Mutable State Pattern
`memory_worker.py` uses module-level globals (`db`, `db_lock`, `processor_task`, etc.) managed via `lifespan`. This works for a single-process FastAPI app but makes testing harder and prevents running multiple worker instances in the same process. Not a bug, but worth noting for future refactoring.

---

## 6. Prioritized Fix Recommendations

| Priority | Issue | Effort | Impact |
|----------|-------|--------|--------|
| **P1** | Add input validation in `mcp_memory_server.py handle_tool_call()` | Small | Fixes 2 test failures, improves error messages |
| **P2** | Add env var overrides for DATA_DIR/LOG_DIR/PID_FILE in `memory_worker.py` | Small | Fixes 1 test failure, makes project generic/portable |
| **P2** | Add "not configured" detection in `require_auth()` | Small | Fixes 1 test failure, improves security posture |
| **P3** | Add source validation + cross-field validator to `ObservationRequest` | Small | Fixes 2 test failures, prevents bad data |
| **P3** | Add `logger.debug` to 3 silent excepts in `unified_vector_store.py` | Trivial | Improves debuggability |
| **P4** | Consider dependency injection for global state | Large | Better testability long-term |

**Total estimated effort to fix all 6 failures:** ~1 hour of focused work.
