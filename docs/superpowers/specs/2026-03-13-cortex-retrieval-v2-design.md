# Cortex Retrieval v2 Design

## Summary

This design upgrades Claude Cortex from a mostly flat multi-source search stack into a typed, layered retrieval system optimized for retrieval accuracy first and token efficiency second.

The design borrows the most useful ideas observed in OpenViking without replacing Claude Cortex with a virtual filesystem platform. The key changes are:

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

- Improve top-1 and top-3 retrieval accuracy across real Claude Cortex usage.
- Preserve Claude Cortex's role as a shared local memory layer for Claude Code, Cami/OpenClaw, and Codex.
- Keep a clean upstream path so the generic engine lives in the public `claude-cortex` repository.
- Maintain backward compatibility long enough to migrate existing MCP clients safely.

## Non-Goals

- Rebuilding Claude Cortex into a full OpenViking-style context filesystem.
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

Claude Cortex should adopt those principles while staying a pragmatic local memory system rather than a general context operating system.

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
- `overview_layer`
- `detail_layer`

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

The contract for layered content is authoritative and must be used consistently by:

- stored `MemoryObject` rows
- adapter outputs
- ranking inputs
- MCP tool responses
- compatibility wrappers

`abstract` is always inline text. `overview` and `detail` are always represented as `ContentLayer` objects, whether the bytes are inline or referenced externally. The system must not mix plain inline strings in one layer and ad hoc `*_ref` fields elsewhere.

```python
@dataclass
class ContentLayer:
    mode: Literal["inline", "ref", "absent"]
    text: str | None
    content_ref: str | None
    token_estimate: int | None
    content_hash: str | None

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
    overview_layer: ContentLayer
    detail_layer: ContentLayer
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
- represented as `overview_layer`

`detail`
- full raw body or a pointer to it
- only loaded when needed
- represented as `detail_layer`

### Layer materialization rules

- `abstract` must always be present inline and indexed in FTS.
- `overview_layer` may be inline or reference-backed.
- `detail_layer` may be inline, reference-backed, or absent.
- `memory_open` resolves layer payloads through the same `ContentLayer` contract regardless of storage mode.
- ranking uses `abstract` and `overview_layer`; it must never require `detail_layer` to rank.

## Core Interface Contracts

Retrieval v2 must define explicit interfaces before implementation work begins.

### QueryPlan

```python
@dataclass
class QueryPlan:
    raw_query: str
    normalized_query: str
    intents: list[str]
    entities: list[str]
    people: list[str]
    task_terms: list[str]
    decision_terms: list[str]
    exact_phrases: list[str]
    time_hints: list[str]
    recency_hint: Literal["none", "recent", "historical"]
    source_preferences: list[str]
    debug: dict[str, Any]
```

Requirements:

- `intents` must be non-empty.
- `source_preferences` is ordered from most to least preferred.
- `debug` may include parser confidence and heuristic triggers.

### NormalizedCandidate

```python
@dataclass
class NormalizedCandidate:
    object_id: str
    source_family: str
    object_type: str
    timestamp: str | None
    entities: list[str]
    task_markers: list[str]
    decision_markers: list[str]
    abstract: str
    overview_layer: ContentLayer
    detail_layer: ContentLayer
    score: float
    score_components: list["ScoreComponent"]
    metadata: dict[str, Any]
```

### ScoreComponent

```python
@dataclass
class ScoreComponent:
    name: str
    value: float
    weight: float
    contribution: float
    explanation: str
```

Requirements:

- every ranked result must expose score components
- `contribution` is the post-weight contribution to final score
- debug output must not require reading source code

### NeighborPreview

```python
@dataclass
class NeighborPreview:
    object_id: str
    abstract: str
    link_type: str
    strength: float
```

### OpenLayerResult

```python
@dataclass
class OpenLayerResult:
    object_id: str
    layer: Literal["abstract", "overview", "detail"]
    payload: ContentLayer
    resolved_text: str | None
    warnings: list[str]
```

### NeighborhoodLink

```python
@dataclass
class NeighborhoodLink:
    from_object_id: str
    to_object_id: str
    link_type: Literal[
        "same_session",
        "same_handoff",
        "same_working_memory_thread",
        "shared_entity",
        "shared_task",
        "shared_decision",
        "semantic_neighbor",
        "time_adjacent",
    ]
    strength: float
    inferred: bool
    metadata: dict[str, Any]
```

Requirements:

- deterministic links must set `inferred=False`
- heuristic links must set `inferred=True`
- expander policy must be able to filter by `link_type` and `inferred`

### Ranker missing-feature policy

The ranker must not interpret missing semantic data as negative evidence.

Rules:

- if a feature family is unavailable for a candidate, its weight is masked rather than converted into a zero-penalty feature
- final candidate score is computed from normalized contributions over available feature families only
- if semantic features are masked, debug output must include `semantic_masked: true`
- cross-source comparison must use the same masking logic for every family

This prevents partially embedded families from being unfairly demoted during migration.

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

### MCP v2 request/response contracts

`memory_query` request:

- `query: str` required
- `limit: int` optional, default 10, max 50
- `source_families: list[str]` optional
- `include_debug: bool` optional, default false
- `expand_neighbors: bool` optional, default true

`memory_query` response:

- `query_plan`
- `results`
- `debug` optional
- `warnings` optional

Each result must include:

- `object_id`
- `source_family`
- `object_type`
- `abstract`
- `overview_layer`
- `score`
- `score_components`
- `neighbor_preview`

`memory_open` request:

- `object_id: str` required
- `layer: Literal["abstract", "overview", "detail"]` required

`memory_open` response:

- resolved layer payload
- `content_ref` if applicable
- `warnings` if the layer is absent or stale

`memory_neighbors` request:

- `object_id: str` required
- `limit: int` optional, default 10
- `link_types: list[str]` optional

`memory_neighbors` response:

- ordered list of linked objects
- link metadata for each neighbor

`memory_feedback` request:

- `query: str` required
- `object_id: str | None` optional
- `feedback: Literal["helpful", "irrelevant", "missing_context", "missed_expected_result"]`
- `notes: str | None`

`memory_feedback` response:

- `recorded: bool`
- `feedback_id: str`

### Error behavior

- unsupported source family filters return a structured warning, not silent omission
- missing objects return a typed not-found error
- unavailable vector search returns a warning and activates lexical fallback
- absent layers return a successful response with `warnings`, not a transport failure

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

### Semantic retrieval contract

- semantic search is optional at runtime but first-class in the design
- embeddings attach to `overview_layer`, never `detail_layer`
- every embedding row must record provider, model, dimension, and embedding version
- ranking must degrade gracefully when vector search is disabled, stale, or unavailable
- reindexing must be triggerable by embedding version changes
- lexical-only fallback must remain a supported operating mode in the public repo

If vectors are unavailable, ranking order becomes:

1. lexical retrieval
2. source-native heuristics
3. entity overlap
4. recency and authority features

No MCP client should fail because embeddings are unavailable.

### Partial semantic coverage rules

Real deployments may have incomplete embedding coverage. Retrieval v2 must support:

- full semantic coverage
- partial per-object coverage
- partial per-family coverage
- globally disabled vector search
- stale embeddings after version changes

Rules:

- if a candidate lacks a valid embedding, its semantic score component is set to `0.0`
- the result debug payload must include `semantic_coverage_state`
- lexical and symbolic features must still be computed normally
- stale embeddings must be excluded from semantic ranking until reindexed
- per-family semantic disablement must produce a family-level warning in debug output
- ranking must remain deterministic under mixed semantic coverage

Required debug fields:

- `semantic_coverage_state`: `full`, `partial`, `disabled`, or `stale`
- `embedding_version`
- `semantic_used: bool`

## Neighborhood Expansion Policy

Neighborhood expansion is intentionally bounded and policy-driven.

### Link generation

Deterministic links:

- shared `session_id`
- shared handoff identifier
- explicit working-memory thread identifier
- explicit source references

Heuristic links:

- shared normalized entities above a confidence threshold
- shared task markers
- shared decision markers
- `semantic_neighbor` links from overview-embedding similarity above a configured threshold
- chronological adjacency within a bounded window

`semantic_neighbor` strength is computed from normalized embedding similarity and is only emitted when:

- both objects have valid current-version embeddings
- the similarity score exceeds the configured minimum threshold
- the resulting link is not dominated by a stronger deterministic relationship

If embeddings are stale, missing, or disabled, semantic links must not be generated and debug output must record that omission.

### Expansion order

The expander should prioritize:

1. deterministic non-time links
2. high-confidence heuristic semantic links
3. time-adjacent links only as a final fallback

### Expansion limits

- default maximum neighbors per seed result: 8
- maximum expansion depth: 1 for initial rollout
- global maximum expanded objects per query: 20
- stop expansion when the next candidate falls below a minimum link strength threshold
- stop expansion when the estimated token budget for neighbor previews is exhausted

These limits are required to keep latency and fan-out predictable.

### Multi-seed arbitration

When multiple seed results nominate overlapping neighbors:

- dedupe globally by `object_id`
- compute one global neighbor score from max link strength plus a small bonus for multi-seed support
- tie-break by deterministic order: stronger link, more seed support, newer timestamp, lexical object ID
- attach the neighbor to every nominating seed in metadata, but only materialize it once in the global expanded set
- semantic neighbors participate in the same arbitration rules as other inferred links and are excluded entirely when semantic coverage is unavailable

`neighbor_preview` in `memory_query` responses is per-seed but derived from the globally deduped expanded set. This keeps previews stable across runs and avoids duplicate context.

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

## Mixed-Mode Rollout Rules

Mixed mode is expected during migration and must be explicitly supported.

### Source family onboarding rules

- only onboarded source families may emit canonical `MemoryObject` results
- non-onboarded families continue to answer through legacy retrieval paths
- the router must know which families are v2-capable at runtime

### V2 mixed-query behavior

`memory_query` must support partial rollout through temporary compatibility adapters.

Rules:

- if a family is v2-capable, query it through native Retrieval v2 adapters
- if a family is legacy-only but still required by the route plan, query it through a temporary compatibility adapter that emits synthetic `MemoryObject`-shaped candidates
- synthetic candidates must set metadata flags identifying them as compatibility-generated
- if a family is unavailable to both v2 and compatibility adapters, `memory_query` must return a coverage warning naming the missing family

This prevents v2 queries from becoming incomplete during migration.

### Stable ID rules

- `object_id` must be deterministic and stable across backfills
- if derived from legacy sources, it must include the source family namespace
- `source_ref` must continue to point to the legacy/raw provenance location

### Legacy compatibility behavior

- if a legacy MCP tool queries a source family not yet onboarded to v2, it must use the old retrieval path for that family
- if both old and new paths are available in shadow mode, old tool responses stay user-facing while v2 results are logged for comparison
- cutover is allowed only after parity criteria are met on benchmark and shadow-read results

### Shadow-read parity criteria

- no severe correctness regression on top-3 hit rate
- improved or equal context usefulness
- bounded latency increase
- stable object resolution for `memory_open` on migrated families

### Cutover rule

A source family may move from shadow mode to default only when:

- benchmark thresholds are met on the held-out validation window
- shadow-read parity is maintained for 7 consecutive days
- no severity-1 retrieval regressions are observed for that family in production feedback

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

### Benchmark construction

- sample historical queries from real usage across all supported query categories
- annotate each query with one or more acceptable target objects
- annotate whether success requires neighbor context, not just the seed object
- store annotations in versioned fixtures

### Data split rules

- use time-based splits to reduce leakage
- train/tune on older queries
- validate on a held-out later window
- reserve a final test window for cutover decisions

### Labeling protocol

Each benchmark item must include:

- query text
- query category
- acceptable object IDs
- minimum acceptable layer (`abstract`, `overview`, or `detail`)
- whether neighborhood context is required
- optional notes on failure modes to avoid

### Metric definitions

`context usefulness rate`
- percentage of queries where the returned result set includes enough seed plus neighbor context to answer without opening unrelated items

`average tokens required`
- estimated tokens consumed to reach an acceptable answer path, including the initial query response and any required `memory_open` or `memory_neighbors` calls

### Acceptance criteria

Before default cutover on a source family:

- top-1 hit rate must improve, or stay within 1 percentage point of baseline while top-3 improves by at least 3 percentage points
- false-positive rate in top-3 must not worsen by more than 2 percentage points
- context usefulness rate must improve by at least 5 percentage points
- p95 latency must not worsen by more than 20 percent

### Confidence reporting

- report aggregate metrics
- report category-level metrics
- report confidence intervals or bootstrap estimates where feasible

### Minimum benchmark size

- at least 200 labeled queries before first family cutover
- at least 25 labeled queries per major query category
- final cutover decisions use the held-out test window only once per family

## Public Repo vs Local Repo Boundary

The public `claude-cortex` repo should own:

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

Proceed with Option 2: restructure Claude Cortex around layered memory objects and staged retrieval, while preserving a clean upgrade path for current MCP clients and a clean upstream path to the public `claude-cortex` repository.
