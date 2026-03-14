# Cortex Retrieval v2 Design

## Summary

This design upgrades LLM Cortex from a mostly flat multi-source search stack into a typed, layered retrieval system optimized for retrieval accuracy first and token efficiency second.

The design borrows the most useful ideas observed in OpenViking without replacing LLM Cortex with a virtual filesystem platform. The key changes are:

- Introduce typed `MemoryObject` records as the canonical retrieval unit.
- Add layered content representations: `abstract`, `overview`, and `detail`.
- Replace global flat retrieval with staged retrieval: analyze, route, retrieve, rank, expand.
- Expand context through semantic/task/entity neighborhoods instead of naive timeline windows.
- Expose retrieval reasoning and diagnostics so tuning becomes evidence-driven.

The result should improve three current failure modes together:

- irrelevant memories ranking too highly
- relevant memories being missed entirely
- correct memories lacking enough surrounding context to be useful

## Goals

- Improve top-1 and top-3 retrieval accuracy across real LLM Cortex usage.
- Preserve LLM Cortex's role as a shared local memory layer for Claude Code, Cami/OpenClaw, and Codex.
- Keep a clean upstream path so the generic engine lives in the public `llm-cortex` repository.
- Maintain backward compatibility long enough to migrate existing MCP clients safely.

## Non-Goals

- Rebuilding LLM Cortex into a full OpenViking-style context filesystem.
- Unifying every possible resource type, capability, and tool into one virtual URI scheme.
- Optimizing for token cost before retrieval quality is measurably improved.

## Why Retrieval v2

The current Cortex stack already has strong pieces:

- observation capture
- SQLite + FTS + optional vector search
- MCP retrieval tools
- working memory
- session bootstrap
- knowledge graph augmentation

The main weakness is not lack of data. It is that retrieval still behaves too much like "search many stores, normalize scores, and drill down manually." That approach does not adequately distinguish between:

- a decision recall query
- a continuation query
- a people/relationship query
- a message recall query
- an exact fact lookup

OpenViking's strongest ideas are useful here:

- layered representations of the same memory unit
- staged retrieval instead of immediately opening full content
- deterministic narrowing before deep expansion
- explicit retrieval observability

LLM Cortex should adopt those principles while staying a pragmatic local memory system rather than a general context operating system.

## Architecture

Retrieval v2 is split into six runtime stages:

1. `QueryAnalyzer`
2. `SourceRouter`
3. `CandidateRetriever` adapters
4. `CrossSourceRanker`
5. `NeighborhoodExpander`
6. `LayeredResultAPI`

### 1. QueryAnalyzer

The analyzer classifies the query into one or more intents and extracts routing hints.

Example intents:

- `decision_recall`
- `session_continuation`
- `entity_lookup`
- `relationship_query`
- `message_recall`
- `fact_lookup`
- `broad_exploration`

It also extracts:

- entity mentions
- people names
- time hints
- recency bias signals
- exact-match phrases
- task/decision language

This stage does not retrieve content. It builds a query plan.

### 2. SourceRouter

The router uses the query plan to prioritize source families.

Primary source families:

- `observations`
- `working_memory`
- `handoffs`
- `session_summaries`
- `notes`
- `messages`
- `knowledge_graph`

Example routing behavior:

- decision recall should boost observations, handoffs, session summaries, and notes
- continuation should boost working memory, handoffs, recent observations, and recent session summaries
- entity lookup should boost knowledge graph, notes, and observations with entity overlap
- message recall should boost conversation/message stores before generic memory search

This stage reduces missed results by ensuring the right stores lead retrieval.

### 3. CandidateRetriever Adapters

Each source family gets a typed adapter that returns candidates in the same normalized shape.

Candidate fields:

- `object_id`
- `source_family`
- `object_type`
- `score_components`
- `timestamp`
- `entities`
- `task_markers`
- `decision_markers`
- `abstract`
- `overview_ref`
- `detail_ref`

Adapters are responsible for source-specific retrieval logic, including:

- lexical retrieval
- semantic retrieval
- graph traversal
- source-native heuristics

They return candidates with evidence, not just opaque scores.

### 4. CrossSourceRanker

The ranker compares candidates across families using shared features instead of simplistic score normalization alone.

Ranking features should include:

- lexical match strength
- semantic similarity
- intent fit
- entity overlap
- task overlap
- decision relevance
- recency
- source authority
- source diversity bonus
- duplicate or near-duplicate penalties

The ranker produces:

- final score
- explanation fields
- ranking trace for debugging

This stage is the main retrieval-accuracy improvement layer.

### 5. NeighborhoodExpander

After top candidates are chosen, this stage gathers the most useful surrounding context.

Expansion signals:

- same session
- same handoff
- same working-memory thread
- same entities
- same task
- same decision chain
- chronological adjacency only when stronger links are absent

This replaces blunt timeline expansion with contextual neighborhoods.

### 6. LayeredResultAPI

Results are returned in layers so clients can inspect the right level first.

- `abstract`: one-sentence identity and relevance summary
- `overview`: condensed but useful context with key entities, decisions, and source references
- `detail`: raw or full content

The retrieval interface should default to returning `abstract` plus enough explanation to decide what to open next. That improves both accuracy and eventual token efficiency.

## Canonical Data Model

Introduce a shared `MemoryObject` model as the canonical indexing and retrieval unit.

```python
@dataclass
class MemoryObject:
    object_id: str
    source_family: str
    object_type: str
    created_at: str | None
    updated_at: str | None
    session_id: str | None
    source_ref: str | None
    entities: list[str]
    task_markers: list[str]
    decision_markers: list[str]
    related_object_ids: list[str]
    abstract: str
    overview: str
    detail: str | None
    metadata: dict[str, Any]
```

### Why this model matters

Current Cortex data is spread across multiple storage forms with different semantics. Retrieval v2 needs one consistent object shape so every source can participate in shared ranking and context expansion.

### Layer semantics

`abstract`
- one line
- purpose: quick identity and relevance check
- should name the core subject, action, and why it may matter

`overview`
- short compressed context
- should include who/what/decision/task/result
- should point to important linked objects

`detail`
- full raw body or a pointer to it
- only loaded when needed

## Source Adapters

The public repo should provide generic adapters with well-defined interfaces. Local/private installations can extend them.

### Required adapters

- `ObservationsAdapter`
- `WorkingMemoryAdapter`
- `HandoffAdapter`
- `SessionSummaryAdapter`
- `NotesAdapter`
- `MessagesAdapter`
- `KnowledgeGraphAdapter`

### Adapter responsibilities

- map source data into `MemoryObject` candidates
- expose source-native retrieval methods
- provide evidence fields for ranking
- declare neighborhood links for expansion

## MCP Interface

The current MCP tools should remain available temporarily, but the new engine should define a v2 surface.

### New primary tools

`memory_query`
- high-level query endpoint
- returns layered top results plus ranking/debug fields

`memory_open`
- opens a specific result at `abstract`, `overview`, or `detail`

`memory_neighbors`
- retrieves related context for a selected memory object

`memory_feedback`
- records whether a result was useful, incorrect, or missing expected context

### Backward compatibility

Existing tools:

- `cami_memory_search`
- `cami_memory_timeline`
- `cami_memory_details`
- `cami_memory_graph_search`
- `cami_message_search`

should initially be adapters over the new retrieval engine where possible. This allows Claude Code, Cami/OpenClaw, and Codex to migrate safely.

## Storage and Indexing Strategy

Retrieval v2 should reuse the current SQLite-centric approach where practical.

Recommended storage split:

- primary object table for `MemoryObject` metadata
- FTS indexes for `abstract` and `overview`
- vector index for `overview` embeddings
- relationship table for neighborhood links
- feedback table for retrieval evaluation

This keeps the operational model simple while enabling better ranking and expansion logic.

## Migration Plan

### Phase 1: Add the new model beside current retrieval

- add `MemoryObject` schema and object builder
- backfill observations first
- add query analyzer, router, and ranker skeleton
- keep old MCP tools untouched

### Phase 2: Expand source coverage

- add handoffs, working memory, session summaries, and notes
- add neighborhood relationships
- route old search APIs through Retrieval v2 in shadow mode

### Phase 3: Introduce v2 MCP tools

- expose `memory_query`, `memory_open`, `memory_neighbors`, `memory_feedback`
- compare old and new retrieval paths on the same benchmark set
- migrate clients gradually

### Phase 4: Default to Retrieval v2

- make v2 the default engine
- keep compatibility shims only where needed
- update docs and quick-start guidance in the public repo

## Evaluation Plan

This redesign succeeds only if retrieval quality improves on real queries.

Build a benchmark from historical usage with labeled expected answers.

Query categories:

- decision recall
- session continuation
- entity lookup
- relationship query
- message recall
- exact fact lookup

Metrics:

- top-1 hit rate
- top-3 hit rate
- context usefulness rate
- false-positive rate in top results
- average tokens required to reach the correct answer
- latency by query type and source family

The benchmark should live in the public repo so improvements are testable and upstream reviewable.

## Public Repo vs Local Repo Boundary

The public `llm-cortex` repo should own:

- `MemoryObject` model
- retrieval planner/router/ranker/expander
- generic source adapters
- MCP v2 interface
- benchmark/evaluation harness
- migration docs

The local `clawd` repo should own only:

- machine-specific paths and config
- private data source connectors
- local MCP wiring
- local bootstrap or operational glue that cannot be upstreamed cleanly

This boundary keeps the design publishable and prevents the public repo from drifting into Cameron-specific local wiring.

## Risks

### Risk: over-engineering before proof

Mitigation:
- ship in phases
- benchmark every phase
- keep compatibility adapters

### Risk: summaries lose key detail

Mitigation:
- do not replace `detail`
- always keep traceable pointers from `abstract` and `overview` back to full source

### Risk: cross-source scoring becomes opaque

Mitigation:
- store score components explicitly
- expose ranking/debug output

### Risk: migration breaks existing agent workflows

Mitigation:
- maintain existing MCP tools during rollout
- test old and new interfaces against the same fixtures

## Recommended First Implementation Slice

Implement the smallest slice that proves the architecture:

- `MemoryObject` model
- observations object builder
- query analyzer
- source router
- observations adapter
- cross-source ranker with one source family
- `memory_query` MVP
- evaluation harness with a small labeled benchmark

Then expand to handoffs, working memory, and session summaries.

## Decision

Proceed with Option 2: restructure LLM Cortex around layered memory objects and staged retrieval, while preserving a clean upgrade path for current MCP clients and a clean upstream path to the public `llm-cortex` repository.
