# Unified Search Endpoint — v1 Design Spec
**Pass FF | 2026-04-16 | Status: PROPOSED**

---

## 1. Problem Statement

Cameron's memory infrastructure currently spans two MCP servers and six distinct data sources. To
answer the question "what do I know about X?" a caller must issue six separate queries, collect
six response sets, and manually merge them:

| MCP | Tool | Data |
|-----|------|------|
| `cortex-memory` | `cami_memory_search` | Observations (what Claude has done) |
| `cortex-memory` | `cami_memory_graph_search` | Knowledge graph entity expansion |
| `cortex-memory` | `cami_memory_search` (source filter) | Strategy-KB + knowledge-base docs (Passes 8-9) |
| `conversation-memory` | `search_all_conversations` | iMessage (12,801 chunks), Discord (3,691), Facebook (10,726), Instagram (17,196) |

The fragmentation has two consequences:

1. **Cognitive load.** Every query session requires the caller to know which MCP owns which
   source and to mentally rank results across three or four separate responses.
2. **Missed relevance.** A strategy insight buried in a Facebook conversation and a matching
   observation from cortex will never be ranked against each other. The user sees them in
   separate response blocks or misses one entirely.

A single `search_all` tool that fans out to every source and returns one ranked list eliminates
both problems. The caller asks once and sees the best N results drawn from all knowledge
regardless of where they live.

This spec covers v1 only: a read-only aggregator that calls existing MCPs as black boxes. No
backend is rewritten. No new storage is introduced.

---

## 2. Architecture

```
Caller (Claude Code / Cami / OpenClaw)
           │
           │  MCP tool call: search_all(query, ...)
           ▼
┌─────────────────────────────────────────────────────────┐
│            cortex-memory MCP  (unified search host)     │
│                                                          │
│  [search_all handler]                                    │
│      │                                                   │
│      ├── 1. call MemoryRetriever.search()                │
│      │       └── observations + knowledge (FTS + vec)   │
│      │           strategy-kb, knowledge-base.db docs     │
│      │                                                   │
│      ├── 2. call MemoryRetriever.search_with_context()   │
│      │       └── knowledge-graph entity expansion        │
│      │           (capped at 20 entities per Pass A5-04)  │
│      │                                                   │
│      └── 3. call conversation-memory server              │
│              └── search_all_conversations()              │
│                  iMessage / Discord / Facebook / Instagram│
│                                                          │
│  [result merger]                                         │
│      │                                                   │
│      ├── normalize ranks per source                      │
│      ├── Reciprocal Rank Fusion                          │
│      ├── deduplicate by (source, source_id) + text hash  │
│      └── return top N unified results                    │
└─────────────────────────────────────────────────────────┘
```

### Host selection rationale

The aggregator lives inside `cortex-memory` (not `conversation-memory` and not a new third MCP)
for three reasons:

1. `cortex-memory` already owns `MemoryRetriever`, which touches observations, knowledge, and
   vector store. All cortex sub-calls are in-process.
2. The only cross-process call is to `conversation-memory`. One remote call is simpler than two.
3. Adding a new MCP would require Cameron to configure and maintain another process. Extending
   `cortex-memory` adds one tool with no new processes.

### Source-of-truth for platform messages

Both MCPs currently cover iMessage/Discord/Facebook/Instagram. In v1, `conversation-memory`
is the exclusive source for all platform messages. `cami_message_search` (cortex-memory) is
**not called** by `search_all`. Reasons:

- `cami_message_search` shares the same SQLite embedding DBs as `conversation-memory`. Calling
  both would produce duplicate chunks.
- `conversation-memory` was the primary focus of Passes 1-4 and is the more complete
  implementation (full identity resolution, four platforms, FTS + vec).

---

## 3. API Surface

### Tool name

```
search_all
```

Exposed as a new tool in `cortex-memory` MCP alongside the existing 8 tools.

### Input schema

```
{
  "query":          string   (required)  natural language search query
  "limit":          integer  (optional)  max total results; default 20, max 50
  "sources":        [string] (optional)  filter to subset of sources;
                             values: "observations", "knowledge", "graph", "conversations"
                             default: all four
  "per_source_cap": integer  (optional)  max results fetched per sub-source before merge;
                             default 10, max 25
  "recency_days":   integer  (optional)  restrict all sub-searches to last N days;
                             0 = no filter (default)
  "include_scores": boolean  (optional)  include rrf_score + per-source rank in output;
                             default false
}
```

### Output schema

Top-level response object:

```
{
  "query":    string           the original query
  "count":    integer          number of results returned
  "results":  [UnifiedResult]  ranked result list
  "sources_searched":  [string]  which sources contributed
  "source_errors":     [string]  sources that failed (empty array = no errors)
  "latency_ms":        integer   wall-clock ms for the full fan-out
}
```

`UnifiedResult` object:

```
{
  "source":      string   one of: "observations", "knowledge", "graph", "conversations"
  "platform":    string   sub-source: "cortex", "imessage", "discord", "facebook",
                          "instagram", "knowledge-base", "strategy-kb"
  "text":        string   the matched text chunk (observations or conversation snippet)
  "score":       float    RRF score (higher = more relevant); always present
  "rank":        integer  rank within the unified result set (1 = best)
  "timestamp":   string   ISO-8601 if available, else null
  "source_id":   string   stable ID for dedup: "<source>:<platform>:<chunk_id_or_obs_id>"
  "metadata":    object   platform-specific fields (see below)
}
```

`metadata` field by platform:

| platform | fields present |
|----------|---------------|
| cortex (observation) | `observation_id`, `agent`, `session_id` |
| cortex (knowledge) | `doc_id`, `tags`, `file_path` |
| graph | `entity_names`, `relationship_types` |
| imessage | `contact_name`, `contact_identifier`, `message_count` |
| discord | `channel_name`, `author_name`, `message_count` |
| facebook | `conversation_name`, `participants`, `message_count` |
| instagram | `conversation_name`, `participants`, `message_count` |

### Error modes

| Condition | Behavior |
|-----------|----------|
| All sub-sources fail | Return `isError: true` with message listing failures |
| One sub-source times out or errors | Return partial results; record source in `source_errors` |
| `query` missing or empty | Return `isError: true` immediately (no sub-calls) |
| `limit` > 50 | Clamp to 50 (consistent with B2-02 caps) |
| `per_source_cap` > 25 | Clamp to 25 |
| `sources` contains unknown value | Return `isError: true` listing valid values |
| conversation-memory MCP unreachable | Degrade to cortex-only; record "conversations" in `source_errors` |

---

## 4. Ranking Strategy

### Per-source output

Each sub-source returns results ordered by its native metric:

- **Observations / knowledge**: FTS5 rank (lower = better) then vector cosine similarity
  (higher = better) via `MemoryRetriever.search()`. Returned as a list ordered by
  `MemoryRetriever`'s existing combined score.
- **Graph**: `MemoryRetriever.search_with_context()` returns base observations augmented with
  entity-expanded results. Treated as a single ranked list.
- **Conversations**: `search_all_conversations()` returns results ordered by cosine distance
  (lower = more similar), already merged across iMessage/Discord/Facebook/Instagram by
  `merge_results()` in `search/base.py`.

Scores across sources are **not comparable**: FTS5 BM25 rank, cosine similarity, cosine
distance are different scales. Attempting min-max normalization is brittle when one source
returns 1 result and another returns 10.

### Reciprocal Rank Fusion (RRF)

RRF uses only ordinal position, not score values:

```
rrf_score(d) = Σ_s  weight_s / (k + rank_s(d))
```

where `k = 60` (standard), `rank_s(d)` is 1-based rank of document `d` in source `s`,
and `weight_s` is the source weight (see below). Documents not present in a source are
excluded from that source's sum.

Results are sorted descending by `rrf_score`. Top `limit` results are returned.

### Default weights

```python
SOURCE_WEIGHTS = {
    "observations": 1.0,   # What Cameron has directly done / decided
    "knowledge":    1.0,   # Strategy docs, KB content
    "graph":        0.7,   # Entity-expanded, potentially noisier
    "conversations": 0.8,  # Real conversations, high signal but large volume
}
```

Weights are editable via a `UNIFIED_SEARCH_WEIGHTS` env var (JSON dict) loaded at module
start. No restart required if the MCP server reloads from env on each call (current pattern
in `mcp_memory_server.py`). Documented in a comment in the handler.

### Recency bias (opt-in)

If `recency_days > 0`, a recency multiplier is applied to `rrf_score` before final sort:

```
recency_multiplier = 1.0 + (0.5 * recency_decay(timestamp, recency_days))
```

where `recency_decay` linearly scales from 1.0 (today) to 0.0 (at `recency_days` boundary)
for results that have a timestamp, and 0.0 for results with no timestamp. The multiplier
is capped at 1.5 to prevent recency from overriding strong relevance.

Recency bias is off by default (`recency_days=0`) because most queries are topic-based, not
time-bound.

### Deduplication

Before final ranking, results with the same `(platform, source_id_stem)` pair are collapsed
to the highest-scoring copy. For results where `source_id` is not unique across sources (e.g.
a strategy-kb chunk appears as both a knowledge doc and an observation summary), a SHA-256
truncated hash of the first 200 characters of `text` is used as a secondary dedup key.

---

## 5. Scope: v1 vs v2

### v1 (this spec)

- Read-only fan-out to existing MCPs; no new backend work
- Aggregator implemented as a single new tool in `cortex-memory`'s `mcp_memory_server.py`
- conversation-memory called via in-process Python import (add `clawd/mcp-servers/conversation-memory/`
  to sys.path) — NOT via subprocess or network call
- RRF ranking, dedup by source_id + text hash
- Graceful degradation: partial results if one source fails
- latency budget: 2s hard timeout per sub-call; proceed with available results after timeout
- Scope limited to the six local data sources listed in the architecture diagram

### v2 (deferred — not designed here)

- **VPS trading data**: congress trades, FRED, options flow from `tradingcore` PostgreSQL
  (100.67.112.3). Requires a new search adapter with a VPS SQL client. Major scope increase.
- **Inline LLM reranker**: pass candidate results through a small prompt to rerank by
  inferred intent. Adds latency and API cost; defer until v1 quality is measured.
- **Streaming results**: return results as they arrive per source rather than waiting for
  all sources. Requires SSE or chunked JSON; MCP stdio transport complicates this.
- **Saved searches / watchlists**: run `search_all` on a set of recurring topics and push
  to cortex memory automatically. Scheduler territory; out of scope for this tool.

---

## 6. Non-Goals

- **This tool does NOT write**. No new observations, no memory saves. Read-only.
- **This tool does NOT rewrite any existing MCPs**. It is an aggregator only.
- **This tool does NOT query VPS trading databases** (congress trades, FRED, options flow).
  Those sources are in v2.
- **This tool does NOT replace per-source tools**. `cami_memory_search`,
  `search_all_conversations`, etc. remain available for targeted queries.
- **This tool does NOT expose a GraphQL or HTTP API**. MCP tool calls only.
- **This tool does NOT do identity resolution** for conversation filtering. Person-scoped
  search remains in `search_person_conversations`.

---

## 7. Implementation Plan

Steps are ordered. Each step is a separate pass.

**Step 1 — PYTHONPATH bridge**
Verify that `clawd/mcp-servers/conversation-memory/` can be imported from within
`llm-cortex/src/mcp_memory_server.py`. Add a `sys.path` insert conditional on the path
existing. Smoke test: `from search.base import SearchResult, merge_results` in the cortex
venv. If import fails (missing sentence-transformers or sqlite_vec in the cortex venv),
resolve dependency gap before Step 2.

**Step 2 — Sub-source adapter functions**
In `mcp_memory_server.py`, add three private helpers:
`_search_cortex(query, limit)`, `_search_graph(query, limit)`,
`_search_conversations(query, limit, days_back)`. Each calls the existing library, catches
all exceptions, and returns `(list[dict], error_or_None)`. No RRF yet — just confirmed
fan-out that returns raw results from all three paths.

**Step 3 — RRF merger**
Add `_rrf_merge(source_lists, weights, k=60, global_limit=20)` in `mcp_memory_server.py`.
Input: list of `(source_name, results)` tuples. Output: deduplicated, ranked
`list[UnifiedResult]`. Unit-testable in isolation with mock inputs.

**Step 4 — Tool handler + schema**
Wire the `search_all` TOOLS entry (input schema from Section 3) and add the handler to
`handle_tool_call`. Apply `_int_arg` clamping (from BUG-B2-02 fix) to `limit` and
`per_source_cap`. Implement the `recency_days` pass-through to sub-adapters. Implement
the 2s per-sub-source timeout using `concurrent.futures.ThreadPoolExecutor` (since
MemoryRetriever is sync).

**Step 5 — Tests**
Add `tests/test_search_all.py`:
- Mock all three sub-adapters. Test RRF ordering: two sources agree on top result, it
  scores highest. One source fails: result still returned, `source_errors` populated.
- Test dedup: same text hash from two sources collapses to one result.
- Test clamping: `limit=200` → response `count <= 50`.
- Integration smoke test (live worker optional, controlled by env var).

---

## 8. Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| PYTHONPATH import fails in cortex venv (missing sentence-transformers, sqlite_vec) | Medium | Blocks conversations source | Step 1 resolves; fall back to subprocess call of conversation-memory as last resort |
| conversation-memory returns no results (not yet restarted after Pass 3 fix) | Low post-restart | Partial results only | Log warning; MCP process must be restarted after embedding fix per Pass 3 note |
| RRF scores are homogeneous (all sources return ~10 results, all rank similarly) | Low | Ranking is arbitrary for tied docs | Tie-break by recency then source priority |
| Duplicate results: strategy-kb doc is in cortex knowledge AND referenced in an observation summary | Medium | Two nearly-identical chunks in result list | Text-hash dedup in Step 3 handles this |
| Latency: three sub-calls fan out, slowest is ~1-2s (MemoryRetriever cold-start) | Low (warm worker) | Caller blocks for 2-4s | 2s per-source timeout + parallel ThreadPoolExecutor in Step 4 |
| Token volume: `per_source_cap=25` * 4 sources = 100 chunks, each up to ~500 tokens | Low (default cap=10) | Context window flood | Global `limit=50` hard cap and default `limit=20` provide ceiling |
| `cami_message_search` + `search_all_conversations` both called by mistake | Medium (future caller error) | Duplicate platform results | Non-goal to call both — documented in Section 2 and Section 6 |

---

## 9. Test Plan

### Unit tests (no live services)

1. **RRF correctness**: Given two sources each returning 5 results with known ranks, verify
   top unified result is the one that ranked 1st in both sources.
2. **Single-source partial failure**: Sub-source raises exception. Verify:
   - Non-empty results from remaining sources returned
   - `source_errors` contains the failed source name
   - `isError` is NOT set (partial success is success)
3. **Total failure**: All sub-sources raise. Verify `isError: true`.
4. **Dedup**: Two results with identical `text[:200]` hash from different sources collapse
   to one. Result count is 1, not 2.
5. **Input validation**: missing `query`, `limit=0`, `limit=999`, unknown source name —
   each returns `isError: true` with clear message.
6. **Recency multiplier**: result with timestamp=today scores higher than identical RRF
   result with no timestamp when `recency_days=7`.

### Integration smoke test (live worker)

Run against live cortex worker + conversation-memory process:

```
search_all(query="BrokerBridge architecture", limit=10)
```

Expected: observations from cortex AND at least one conversation chunk (Discord or iMessage
where BrokerBridge was discussed). `source_errors` should be empty. Latency < 5s.

```
search_all(query="VIX 50 buy rule", limit=5)
```

Expected: `strategy-kb` knowledge doc `bad_news_vs_uncertainty.md` (ingested Pass 8) in
top 3 results. The pre-existing TRADING RULE cortex observation should also appear.

### Regression check

Run `pytest tests/` after Step 5. All 96 currently-passing tests must still pass (post-C1
baseline). No existing tool behavior should change.

---

## 10. Open Questions for Cameron

1. **Import strategy for conversation-memory**: Is it acceptable to add
   `clawd/mcp-servers/conversation-memory/` to `sys.path` inside `mcp_memory_server.py`?
   The alternative is to call the conversation-memory server as a subprocess via JSON-RPC,
   which is cleaner architecturally but slower and harder to test. Which do you prefer?

2. **Default source weights**: The proposed defaults are observations=1.0, knowledge=1.0,
   graph=0.7, conversations=0.8. These reflect a prior that directly-remembered work and
   explicit knowledge docs are more authoritative than conversation fragments. If you
   regularly find conversational context more useful than observations, flip conversations
   and knowledge to 1.0 and drop observations to 0.9.

3. **Graph results in v1**: Including `search_with_context` (graph-expanded search) means
   up to 20 entity expansions each firing a sub-search (Pass A5-04 cap). This is safe but
   adds latency. Should graph results be in v1, or deferred to v2 where they can be
   behind a feature flag?

4. **VPS trading data priority**: The design defers congress trades / FRED / options flow
   to v2 on the grounds that it requires a new SQL adapter. If trading-data recall is a
   higher priority than conversation search, those sources could swap into v1 and
   conversations could move to v2. What is the priority ordering?

5. **Max context per result**: Results returned to Claude's context window are raw text
   chunks of variable length (iMessage chunks can be long). Should `search_all` truncate
   each result's `text` to a max character count (e.g., 400 chars) with a `truncated: true`
   flag, or return full text and let the caller manage context?

---

*Design doc by Pass FF — 2026-04-16. Approved by Cameron before any implementation begins.*
