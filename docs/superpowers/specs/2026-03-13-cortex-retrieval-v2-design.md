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

Budget rules:

- the router allocates a per-family candidate budget before retrieval begins
- higher-ranked `source_preferences` get larger budgets first
- default per-family pre-rank cap: 25 candidates
- default total pre-rank cap across all families: 100 candidates
- if total routed candidates exceed budget, trim lowest-priority families first, then lowest-scoring tail candidates within a family
- trimming must use deterministic tie-breaks: source preference order, family-local score, newer timestamp, lexical `object_id`

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

Candidate emission rules:

- no adapter may emit more than its allocated per-family budget
- adapters must return candidates already ordered by family-local relevance
- adapters must expose deterministic tie-break behavior for equal local scores

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

Source authority contract:

- source authority is a versioned global ranking config keyed by `source_family` and `object_type`
- the default precedence is:
  1. native migrated source family object
  2. observation object with direct provenance
  3. handoff or working-memory object with explicit linkage
  4. session summary or note object
  5. compatibility-generated synthetic object
- ranking uses this same ordered mapping for the source-authority feature and for deterministic final tie-breaks
- the mapping is invariant by default, not query-intent-specific

Versioned config contract:

- `RankerConfig` is the single source of truth for feature weights, source authority, diversity bonus, duplicate penalties, and final tie-break settings
- `ExpansionConfig` is the single source of truth for semantic thresholds, neighbor thresholds, chronology balancing, and expansion budgets
- every `query_execution_id` must log the `RankerConfig` and `ExpansionConfig` versions used for that run

The ranker produces:

- final score
- explanation fields
- ranking trace for debugging

Normative final-score formula:

- normalize each feature to `[0, 1]`
- apply configured feature weights from `RankerConfig`
- mask unavailable features before weighted summation
- renormalize by the sum of active feature weights
- apply duplicate penalties after weighted summation
- apply diversity bonus after duplicate penalties
- final score is clipped to `[0, 1]`

Source diversity bonus contract:

- diversity bonus is prefix-dependent and applies only to seed-ranking rows
- seed ranking uses a greedy prefix-selection algorithm rather than a single global sort
- first compute `base_score` for every eligible seed candidate using the weighted feature sum, masked-feature renormalization, and duplicate penalties, but excluding the diversity bonus
- for rank position `r`, evaluate every remaining candidate with `effective_score = clip(base_score + diversity_bonus, 0, 1)`
- `diversity_bonus(candidate, r)` equals the configured per-family bonus only if no already-selected seed candidate in positions `< r` has the same `source_family`
- otherwise `diversity_bonus(candidate, r) = 0.0`
- the candidate with the highest `effective_score` wins rank `r`; ties break with the deterministic final tie-break sequence defined below
- the diversity bonus may therefore be applied at most once per `source_family` within a ranked query result set
- duplicate collapse and duplicate penalties run before diversity-bonus eligibility is evaluated

Final ranked results must use deterministic tie-breaks after equal final score:

1. stronger intent-fit contribution
2. higher source authority
3. newer timestamp
4. lexical `object_id`

### Duplicate handling and source authority

Duplicate detection keys:

- exact `source_ref`
- exact `authoritative_content_hash`
- compatibility provenance pointing at the same underlying legacy item

Duplicate hash contract:

- `authoritative_content_hash` is the only canonical cross-object duplicate hash
- adapters must compute it from a canonical JSON envelope with stable key order
- the canonical envelope must include: canonical `object_type`, normalized primary textual content, canonical provenance namespace, and canonical immutable provenance identifier
- normalization must trim surrounding whitespace, normalize line endings to `\n`, and normalize Unicode to NFC before hashing
- the hash algorithm must be SHA-256 recorded under the active config version
- layer-level `ContentLayer.content_hash` values are not used for cross-object dedupe unless explicitly copied into `authoritative_content_hash`
- `related_object_ids` are general semantic relationships and must never be interpreted as exact-duplicate evidence
- if a future adapter wants to contribute explicit duplicate assertions, it must do so through a dedicated duplicate-provenance field rather than `related_object_ids`

Merge policy:

- exact duplicates from different retrieval paths should be merged into one returned candidate
- near-duplicates with materially different context may both remain visible but lower-ranked
- suppressed duplicates still contribute evidence to the surviving candidate and may contribute neighbor links

Source authority precedence for merged duplicates:

1. native migrated source family object
2. observation object with direct provenance
3. handoff or working-memory object with explicit linkage
4. session summary or note object
5. compatibility-generated synthetic object

When duplicates merge, the highest-authority candidate survives as the canonical returned object. Lower-authority duplicates contribute score evidence and provenance metadata but are not separately returned.

Field merge rules for exact duplicates:

- canonical `object_id`, `abstract`, `overview_layer`, `detail_layer`, and freshness state come from the surviving candidate
- `entities`, `task_markers`, `decision_markers`, and `related_object_ids` are unioned and deduped
- `timestamp` uses the surviving candidate timestamp unless it is missing, then the newest non-missing duplicate timestamp wins
- warnings and debug evidence may include contributed duplicate provenance
- lower-authority duplicates may contribute provenance and neighbor links, but may not replace the surviving candidate's layers

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
    visibility: Literal["active", "tombstoned"]
    created_at: str | None
    updated_at: str | None
    session_id: str | None
    handoff_id: str | None
    working_memory_thread_id: str | None
    link_group_id: str | None
    source_ref: str | None
    authoritative_content_hash: str | None
    entities: list[str]
    task_markers: list[str]
    decision_markers: list[str]
    related_object_ids: list[str]
    abstract: str
    overview_layer: ContentLayer
    detail_layer: ContentLayer
    abstract_version: str | None
    overview_version: str | None
    abstract_generation_method: Literal["deterministic", "model"] | None
    abstract_generation_model: str | None
    abstract_generated_at: str | None
    overview_generation_method: Literal["deterministic", "model"] | None
    overview_generation_model: str | None
    overview_generated_at: str | None
    overview_freshness: Literal["fresh", "stale", "missing"]
    detail_freshness: Literal["fresh", "stale", "missing"]
    embedding_freshness: Literal["fresh", "stale", "missing", "disabled"]
    link_freshness: Literal["fresh", "stale", "missing"]
    metadata: dict[str, Any]
```

### ID contract

`object_id` must be deterministic, namespaced, and parseable.

Canonical formats:

- observations: `obs:<observation_id>`
- working memory: `wm:<session_key>:<entry_id>`
- handoffs: `handoff:<handoff_id>`
- session summaries: `session:<session_id>`
- notes: `note:<note_id>`
- messages: `msg:<platform>:<message_id>`
- knowledge graph: `kg:<entity_id>`
- compatibility-generated synthetic objects: `compat:<source_family>:<stable_key>`

Component encoding rule:

- every `object_id` component must use lowercase URL-safe base32 without padding when the raw immutable source identifier contains characters outside the canonical grammar
- adapters and compatibility translators must use the same encoding rule
- if reversibility is required, the raw immutable identifier must also be persisted in metadata

Legacy translation rules:

- numeric observation IDs must translate directly to `obs:<id>`
- legacy message IDs must translate directly to `msg:<platform>:<id>` when platform is known
- compatibility adapters must expose deterministic translation helpers for legacy IDs they still support
- `memory_open` and `memory_neighbors` must accept either canonical `object_id` values or legacy IDs that a compatibility adapter can translate unambiguously
- both APIs must canonicalize accepted legacy IDs to canonical `object_id` before object lookup and logging

Immutable key rules:

- `entry_id`, `note_id`, and `stable_key` must come from immutable source identifiers, not user-visible slugs or titles
- user-visible slugs may appear in metadata, but must not be used as canonical `object_id` components
- if source content is renamed, the canonical `object_id` must remain unchanged
- if an immutable source identifier is unavailable, the adapter must synthesize one once and persist it for future reuse
- collisions must be resolved by appending a stable disambiguator derived from immutable provenance, not by renumbering existing IDs

Compatibility invariants:

- translating a supported legacy ID to `object_id` and back must be stable within a rollout phase
- shadow-mode comparisons must assert that legacy fetches and translated `object_id` fetches resolve to the same underlying provenance

Identity migration rule:

- if a compatibility adapter cannot emit the eventual native `object_id` from day one, the system must persist a permanent alias table from `compat:*` IDs to canonical native IDs
- query, open, neighbors, logging, feedback, and duplicate merging must canonicalize through that alias table
- once a native ID exists, the compat alias remains valid for backward compatibility

### State contract

- `visibility="active"` objects are eligible for ranking
- `visibility="tombstoned"` objects remain openable by ID but are excluded from default ranking and neighbor expansion
- `overview_freshness`, `detail_freshness`, `embedding_freshness`, and `link_freshness` are authoritative per-object state fields for the currently published artifact set only
- a newer unseen source revision does not by itself flip published `overview_freshness` or `detail_freshness` to `stale`; that condition must instead surface as a warning/debug state such as `source_newer_than_published`
- ranking must exclude stale embeddings and stale links
- ranking may continue to use the last fully fresh `abstract` and `overview_layer` until replacement text and indexes are ready, then atomically swap to the rebuilt version
- `memory_query` must surface relevant state through warnings/debug when stale data affects ranking
- `memory_open` must surface layer freshness through `status` and warnings
- `memory_neighbors` must exclude stale or tombstoned neighbors by default unless explicitly requested by a future extension

Update state model:

- each object has one published version that remains queryable
- rebuilds create pending derived artifacts for the next published version
- `memory_query` reads only the published version
- `memory_open` reads the published version unless a future admin/debug path explicitly requests pending state
- when rebuild completes, layers, embeddings, and links swap atomically from pending to published

Update state transitions:

1. source change detected
   - published object remains queryable
   - pending rebuild state is created
   - published `overview_freshness` and `detail_freshness` remain whatever they were before the change because they describe the published artifact set, not source recency
   - published `embedding_freshness` and `link_freshness` may become `stale` immediately if the source change invalidates them
   - query/open warnings must indicate that the source is newer than the published artifact set until the rebuild publishes

2. rebuild in progress
   - `memory_query` continues serving the published object
   - `memory_open` continues serving the published object
   - warnings/debug may note pending rebuild and stale derived artifacts

3. rebuild complete
   - new layers, embeddings, and links become the published version atomically
   - freshness fields reset to `fresh` for the rebuilt artifacts

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

### Layer generation contract

`abstract` and `overview_layer` must be generated explicitly and versioned.

Generation rules:

- `abstract` should be deterministic where a concise structured summary can be produced from metadata; otherwise it may use a model-based summarizer
- `overview_layer` may be deterministic for structured sources or model-based for free text
- `detail_layer` is never generated; it is copied from source content or referenced directly

Required generation metadata:

- `abstract_version`
- `overview_version`
- `abstract_generation_method`: `deterministic` or `model`
- `abstract_generation_model` if model-based
- `abstract_generated_at`
- `overview_generation_method`: `deterministic` or `model`
- `overview_generation_model` if model-based
- `overview_generated_at`

Regeneration triggers:

- source content change
- marker extraction change
- summary prompt/template change
- generation model/version change
- explicit repair or rebuild command

Failure behavior:

- if `abstract` generation fails, the object is not queryable until a fallback abstract is synthesized from source metadata
- if `overview_layer` generation fails, the object may remain queryable through `abstract`, but it is ineligible for semantic retrieval until repaired
- generation failures must emit typed warnings and queue the object for repair

### Content resolution contract

Reference-backed layers must use a concrete `content_ref` contract.

Allowed `content_ref` formats:

- `obs://<object_id>/<layer>`
- `wm://<object_id>/<layer>`
- `handoff://<object_id>/<layer>`
- `session://<object_id>/<layer>`
- `note://<object_id>/<layer>`
- `msg://<object_id>/<layer>`
- `kg://<object_id>/<layer>`
- `compat://<object_id>/<layer>`

Formal grammar:

- regex: `^(obs|wm|handoff|session|note|msg|kg|compat)://([a-z0-9._:-]+)/((overview|detail))$`
- scheme and layer tokens must be lowercase
- `object_id` may contain only `a-z`, `0-9`, `.`, `_`, `:`, and `-`
- `/` is not allowed inside `object_id`
- malformed refs must raise typed `Error(code="invalid_ref", scope="resolver")`, not `not_found`
- well-formed refs with missing targets must resolve to `not_found`

Resolver rules:

- each source family must register a resolver for its own `content_ref` namespace
- resolvers must return a `ResolvedLayerResult`
- `memory_open` must use the registered resolver rather than source-specific branching in the MCP layer

Resolver return contract:

```python
@dataclass
class ResolvedLayerResult:
    status: Literal["success", "absent", "stale", "not_found"]
    payload: ContentLayer
    resolved_text: str | None
    warnings: list["Warning"]
```

Resolver invariants:

- on `success`, `payload.mode` must be `inline`, `payload.text` must be populated, and `resolved_text` must equal `payload.text`
- on `absent`, `payload.mode` must be `absent`, `payload.text` must be `None`, and `resolved_text` must be `None`
- on `stale`, `payload.mode` may remain `ref` or be `absent`, `resolved_text` must be `None`, and `warnings` must explain staleness
- on `not_found`, `payload.mode` must be `ref` or `absent`, `resolved_text` must be `None`, and `warnings` must explain the missing target

`memory_open` invariant:

- `OpenLayerResult.payload` is always the post-resolution payload, never the original unresolved ref shell
- `OpenLayerResult.resolved_text` is populated only when the resolved payload is inline text
- ref-backed success therefore resolves to an inline payload in the response

Indexing and ranking rules for ref-backed `overview_layer`:

- every ref-backed overview must still have a materialized text form available for FTS and ranking
- materialized overview text may be cached in the primary object table or a dedicated overview cache
- if a ref-backed overview cannot be materialized, the object is not eligible for ranking until repaired
- stale refs must return warnings and be excluded from ranking until the resolver succeeds again

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
class CandidateState:
    visibility: Literal["active", "tombstoned"]
    overview_freshness: Literal["fresh", "stale", "missing"]
    detail_freshness: Literal["fresh", "stale", "missing"]
    embedding_freshness: Literal["fresh", "stale", "missing", "disabled"]
    link_freshness: Literal["fresh", "stale", "missing"]

@dataclass
class NormalizedCandidate:
    object_id: str
    source_family: str
    object_type: str
    timestamp: str | None
    source_ref: str | None
    authoritative_content_hash: str | None
    state: CandidateState
    entities: list[str]
    task_markers: list[str]
    decision_markers: list[str]
    related_object_ids: list[str]
    provenance_object_ids: list[str]
    abstract: str
    overview_layer: ContentLayer
    detail_layer: ContentLayer
    family_local_score: float
    global_score: float
    score_components: list["ScoreComponent"]
    metadata: dict[str, Any]
```

Timestamp contract:

- `timestamp` should represent source event time when the family has a meaningful event timestamp
- if event time is unavailable, use the best immutable creation timestamp for that family
- only if neither exists may the adapter fall back to update time, and that fallback must be recorded in debug metadata

Per-family timestamp source:

- observations: observation event timestamp
- working memory: entry creation timestamp
- handoffs: handoff creation timestamp
- session summaries: session end timestamp
- notes: note creation timestamp if available, otherwise first-seen timestamp
- messages: message sent/received timestamp
- knowledge graph: entity creation or first-seen timestamp; update time only as explicit fallback

Ranker and query logic must use `NormalizedCandidate.state` directly rather than hidden store lookups.

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
    source_family: str
    abstract: str
    global_score: float
    seed_link_type: str
    seed_link_strength: float
    support_count: int
```

### OpenLayerResult

```python
@dataclass
class OpenLayerResult:
    object_id: str
    layer: Literal["abstract", "overview", "detail"]
    status: Literal["success", "absent", "stale", "not_found"]
    payload: ContentLayer
    resolved_text: str | None
    byte_range: tuple[int, int] | None
    has_more: bool
    warnings: list["Warning"]

@dataclass
class QueryResult:
    object_id: str
    source_family: str
    object_type: str
    match_kind: Literal["seed", "expanded_context"]
    abstract: str
    score: float
    score_components: list["ScoreComponent"]
    overview_layer: ContentLayer | None
    neighbor_previews: list[NeighborPreview]
    warnings: list["Warning"]
    debug: dict[str, Any] | None

@dataclass
class NeighborResult:
    object_id: str
    abstract: str
    is_seed: bool
    timestamp: str | None
    global_score: float
    link_type: str
    link_strength: float
    support_count: int
    overview_layer: ContentLayer | None
    warnings: list["Warning"]
    debug: dict[str, Any] | None
```

### Warning and Error

```python
@dataclass
class Warning:
    code: str
    message: str
    scope: Literal["query", "result", "neighbor", "object", "source_family"]
    object_id: str | None
    source_family: str | None

@dataclass
class Error:
    code: str
    message: str
    scope: Literal["request", "object", "source_family", "resolver"]
    object_id: str | None
    source_family: str | None
```

`memory_open` response mapping:

- resolver `success` maps to `OpenLayerResult.status="success"`
- resolver `absent` maps to `OpenLayerResult.status="absent"`
- resolver `stale` maps to `OpenLayerResult.status="stale"`
- resolver `not_found` maps to `OpenLayerResult.status="not_found"`
- resolver `invalid_ref` must surface as typed `Error(code="invalid_ref", scope="resolver")` at the MCP boundary

Canonical warnings location:

- warnings live only in `OpenLayerResult.warnings`
- the top-level `memory_open` response must not duplicate warnings separately

`memory_open` behavior table:

- malformed request or unsupported `layer` token: typed MCP error with `Error.scope="request"`
- untranslatable legacy ID: typed MCP error with `Error.scope="object"`
- nonexistent `object_id`: successful `OpenLayerResult` with `status="not_found"`
- existing object, requested layer absent: successful `OpenLayerResult` with `status="absent"`
- existing object, requested ref stale: successful `OpenLayerResult` with `status="stale"`
- existing object, malformed stored ref: typed MCP error with `Error.code="invalid_ref"` and `Error.scope="resolver"`
- existing object, successful resolution: successful `OpenLayerResult` with `status="success"`
- `layer="abstract"`: synthesize an inline `ContentLayer` from the stored abstract string and return it as a successful `OpenLayerResult`

`memory_open(detail)` payload contract:

- native v2 callers may receive chunked detail
- default inline detail chunk limit for native v2 callers: 64 KB of UTF-8 text
- `offset` and `limit_bytes` control which detail chunk is returned
- if more detail remains after the returned chunk, emit a truncation warning and set `has_more=true`
- native callers request the next chunk by advancing `offset` to the previous `byte_range[1]`
- compatibility-backed `cami_memory_details` calls must retrieve full detail for migrated families, even if that requires internal pagination or multiple fetches behind the compatibility adapter
- the response must set `payload.token_estimate` for the returned chunk or full body

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
        "same_link_group",
        "same_source_ref",
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

Canonical semantic rule:

- if semantic retrieval is unavailable for a candidate, the semantic feature family is excluded from score normalization
- in that case, no semantic `ScoreComponent` is emitted for that candidate
- `semantic_used` must be `false`
- `semantic_coverage_state` must describe why semantic scoring was excluded

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
- `include_overview: bool` optional, default false
- `expand_neighbors: bool` optional, default true

`memory_query` response:

- `query_execution_id`
- `query_plan`
- `results: list[QueryResult]`
- `debug` optional
- `warnings: list[Warning]` optional

`source_families` semantics:

- `source_families`, when present, is a hard allowlist for seed retrieval
- the router may only allocate candidate budget to listed families
- unsupported or unavailable listed families must produce structured warnings
- omitted families must not appear as seed results
- neighbor expansion may still return linked objects from omitted families only inside `neighbor_previews`
- every `NeighborPreview` is expanded context by definition and must not be interpreted as a seed match
- omitted families may appear in `neighbor_previews` only when linked expansion selects them

Each result must include:

- `object_id`
- `source_family`
- `object_type`
- `match_kind`
- `abstract`
- `score`
- `score_components`
- `overview_layer` optional, included only when `include_overview=true`
- `neighbor_previews: list[NeighborPreview]`
- `debug` optional, included only when `include_debug=true`

`memory_query` result-shape invariant:

- initial v2 `memory_query.results` contain only seed matches, so `QueryResult.match_kind` must be `"seed"` for every returned row
- if a future compatibility mode inlines expanded rows into `results`, those rows must set `match_kind="expanded_context"` and must never consume seed-ranking budget
- `neighbor_previews` remain previews of expanded context, not additional seed rows

`include_overview=true` invariant:

- when `include_overview=true`, `overview_layer` in `QueryResult` must always be returned as a resolved inline `ContentLayer`
- if the source stored a ref-backed overview, the server must resolve it before returning the query result
- if that resolution fails, `overview_layer` must be `None` and the result warnings/debug output must record the failure
- `memory_open` remains the authoritative path for `detail` and for explicit status inspection

`memory_open` request:

- `query_execution_id: str | None` optional
- `object_id: str` required
- `layer: Literal["abstract", "overview", "detail"]` required
- `offset: int` optional, default 0, only for `layer="detail"`
- `limit_bytes: int` optional, default 65536, only for `layer="detail"`

`memory_open` response:

- `OpenLayerResult`

`memory_neighbors` request:

- `query_execution_id: str | None` optional
- `object_id: str` required
- `limit: int` optional, default 10
- `link_types: list[str]` optional
- `mode: Literal["relevance", "chronology"]` optional, default `"relevance"`

`memory_neighbors` response:

- `results: list[NeighborResult]`
- optional `debug`
- `warnings: list[Warning]` optional

`memory_neighbors` ordering and dedupe:

- dedupe by `object_id`
- order by global neighbor score descending in `mode="relevance"`
- chronology mode uses the following deterministic selection algorithm
- if the seed has a timestamp:
  1. partition eligible deduped neighbors into `before`, `after`, and `untimestamped`
  2. sort `before` by absolute time delta to the seed ascending, then stronger link strength, then newer timestamp, then lexical `object_id`
  3. sort `after` by absolute time delta to the seed ascending, then stronger link strength, then older timestamp, then lexical `object_id`
  4. fill the `limit - 1` non-seed slots by alternating between the head of `before` and the head of `after`, starting with whichever side has the smaller head delta; if one side is exhausted, continue with the other side; only after both timestamped sides are exhausted may `untimestamped` rows fill remaining slots
  5. render the selected timestamped rows in ascending timestamp order with the seed row inserted between the selected `before` and `after` rows at the seed timestamp position; append selected `untimestamped` rows after all timestamped rows
- if the seed timestamp is missing, chronology mode selects the non-seed rows using the same candidate order as `mode="relevance"`; the rendered order is then: seed row first, selected timestamped rows in ascending timestamp order, then selected untimestamped rows in lexical `object_id` order
- ties break by stronger link strength, then newer timestamp if available, then lexical object ID
- returned neighbors include `abstract` always and `overview_layer` only if a future request flag explicitly asks for it; initial v2 should default `overview_layer` to `None`
- `mode="chronology"` must include the seed object in the returned sequence
- in `mode="chronology"`, `limit` includes the seed row
- the seed row must set `is_seed=true`, `link_type="seed"`, `link_strength=0.0`, and `support_count=1`
- when timestamps are missing, place those rows after timestamped rows using lexical `object_id` tie-breaks

`memory_neighbors` behavior table:

- malformed request or unsupported `mode`: typed MCP error with `Error.scope="request"`
- nonexistent or untranslatable seed `object_id`: typed MCP error with `Error.scope="object"`
- tombstoned seed object: successful response with warnings; seed may be returned but tombstoned neighbors remain excluded by default
- unknown `link_types` filter values: structured warning and those filter values are ignored
- `link_types` filtering applies before final neighbor ranking and before displayed `link_type` collapse

`memory_feedback` request:

- `query_execution_id: str` required
- `query: str` required
- `object_id: str | None` optional
- `feedback: Literal["helpful", "irrelevant", "missing_context", "missed_expected_result"]`
- `notes: str | None`

`memory_feedback` response:

- `recorded: bool`
- `feedback_id: str`

Query execution logging:

- every `memory_query` execution must persist `query_execution_id`
- the persisted execution record must include engine version, ranker version, source coverage state, top-k returned object IDs, and whether the request ran in shadow mode
- `memory_feedback` must attach to that stored execution record for regression analysis and cutover decisions
- `memory_open` and `memory_neighbors` should attach to the originating `query_execution_id` when present so token-to-answer paths remain traceable

### Error behavior

- unsupported source family filters return a structured `Warning`, not silent omission
- malformed requests return typed `Error` objects at the MCP boundary
- unavailable vector search returns a `Warning` and activates lexical fallback
- absent layers return a successful response with `OpenLayerResult.status="absent"`
- nonexistent objects in `memory_open` return a successful response with `OpenLayerResult.status="not_found"`

### Backward compatibility

Existing tools:

- `cami_memory_search`
- `cami_memory_timeline`
- `cami_memory_details`
- `cami_memory_graph_search`
- `cami_message_search`

should initially be adapters over the new retrieval engine where possible. This allows Claude Code, Cami/OpenClaw, and Codex to migrate safely.

Compatibility matrix:

- `cami_memory_search` -> `memory_query`
  - preserve legacy-style compact summaries
  - legacy observation IDs remain valid for observation-backed objects via stable `object_id` namespacing
  - non-onboarded families may be omitted unless explicitly exposed by compatibility adapters

- `cami_memory_timeline` -> `memory_neighbors`
  - preserve chronology-oriented output for session-linked and time-adjacent neighbors by using `mode="chronology"`
  - if v2 neighborhood data is unavailable, fall back to legacy timeline behavior for that family

- `cami_memory_details` -> `memory_open`
  - map detail fetches to `layer="detail"`
  - preserve legacy ability to fetch full text for migrated families

- `cami_memory_graph_search` -> `memory_query` plus graph-biased routing
  - preserve graph-oriented recall behavior and result summaries

- `cami_message_search` -> `memory_query` or compatibility adapter over message stores
  - preserve message-centric result ordering and source labeling

During rollout, legacy responses remain user-visible stable while v2 runs in shadow mode for comparison.

Per-family onboarding table:

- observations
  - source of truth: observations database
  - canonical object type: observation
  - immutable key: numeric observation ID
  - `object_id`: `obs:<observation_id>`
  - update/delete signal: hook writes and worker updates
  - resolver path: observation content resolver
  - replayability: yes

- working_memory
  - source of truth: working-memory state files
  - canonical object type: working-memory entry
  - immutable key: persisted entry ID
  - `object_id`: `wm:<session_key>:<entry_id>`
  - update/delete signal: working-memory writes
  - resolver path: working-memory resolver
  - replayability: partial

- handoffs
  - source of truth: handoff documents
  - canonical object type: handoff
  - immutable key: persisted handoff ID
  - `object_id`: `handoff:<handoff_id>`
  - update/delete signal: handoff document writes
  - resolver path: handoff resolver
  - replayability: yes if retained

- session_summaries
  - source of truth: session summary records
  - canonical object type: session summary
  - immutable key: session ID
  - `object_id`: `session:<session_id>`
  - update/delete signal: session-end summarization
  - resolver path: session-summary resolver
  - replayability: yes if retained

- notes
  - source of truth: note files or note records
  - canonical object type: note
  - immutable key: persisted note ID
  - `object_id`: `note:<note_id>`
  - update/delete signal: note updates
  - resolver path: note resolver
  - replayability: partial

- messages
  - source of truth: message store
  - canonical object type: message
  - immutable key: platform message ID
  - `object_id`: `msg:<platform>:<message_id>`
  - update/delete signal: message ingestion/update pipeline
  - resolver path: message resolver
  - replayability: yes if retained

- knowledge_graph
  - source of truth: graph entity store
  - canonical object type: graph entity
  - immutable key: entity ID
  - `object_id`: `kg:<entity_id>`
  - update/delete signal: graph seed/update pipeline
  - resolver path: graph resolver
  - replayability: partial unless snapshots are retained

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

## Sync and Invalidation Lifecycle

Retrieval v2 must define how source changes propagate into derived state.

### Create

- on source create, build or enqueue a new `MemoryObject`
- generate or resolve `abstract` and `overview_layer`
- extract markers
- create embeddings if semantic search is enabled
- derive neighborhood links

### Update

- on source update, create or refresh a pending rebuild for the next published version
- regenerate layers and markers
- rebuild embeddings if overview text changed or embedding version changed
- recompute affected neighborhood links
- if the source change invalidates published embeddings or links before rebuild completes, mark only those derived artifacts stale on the published version and emit `source_newer_than_published` warnings until publish

Published/pending persistence contract:

- object metadata tracks one published artifact set and zero or one pending artifact set
- pending layers, embeddings, and links must be written under a new version identifier before publish
- publish is one atomic transaction that swaps the published version pointer, updates searchable indexes, and marks the previous version superseded
- query and open paths must never mix published and pending artifacts within the same response

### Delete or tombstone

- sources should prefer tombstoning over hard deletion
- tombstoned objects remain addressable for provenance but are excluded from ranking by default
- links to tombstoned objects must be suppressed from default neighbor expansion

### Repair and rebuild

- failed generation or stale refs must enqueue a repair job
- embedding-version changes must enqueue incremental reindex jobs
- link-derivation rule changes must enqueue link rebuild jobs
- the system must support source-family-scoped and full rebuild procedures

### Stale marking

- stale layers, stale embeddings, and stale links must be tracked separately
- read-time retrieval must surface stale status through warnings/debug fields
- ranking must exclude stale semantic features and stale links until rebuilt

### Partial semantic coverage rules

Real deployments may have incomplete embedding coverage. Retrieval v2 must support:

- full semantic coverage
- partial per-object coverage
- partial per-family coverage
- globally disabled vector search
- stale embeddings after version changes

Rules:

- if a candidate lacks a valid embedding, semantic scoring is omitted for that candidate rather than assigned a zero-valued semantic component
- the result debug payload must include `semantic_coverage_state`
- lexical and symbolic features must still be computed normally
- stale embeddings must be excluded from semantic ranking until reindexed
- per-family semantic disablement must produce a family-level warning in debug output
- ranking must remain deterministic under mixed semantic coverage

Required debug fields:

- `semantic_coverage_state`: `full`, `partial`, `disabled`, or `stale`
- `embedding_version`
- `semantic_used: bool`

## Marker Normalization Contract

Cross-source overlap depends on normalized markers, not raw free-text lists.

### Canonical marker shapes

- `entities`: normalized identifiers such as `person:jake-smith` or `project:brokerbridge`
- `task_markers`: normalized task phrases such as `debug-auth-module`
- `decision_markers`: normalized decision phrases such as `use-refresh-token-rotation`

### Normalization rules

- lowercase
- trim whitespace
- collapse punctuation and spacing to a canonical slug form
- attach a namespace where possible (`person:`, `project:`, `company:`, `topic:`)
- keep original extracted text in metadata for debugging

### Overlap rules

- entity overlap is exact-match on normalized identifiers
- task overlap is exact-match first, with optional heuristic alias mapping from a shared synonym table
- decision overlap is exact-match first, with optional heuristic alias mapping from a shared synonym table
- routing, ranking, and neighborhood linking must all use the same normalized marker forms

This contract is required so different adapters can interoperate predictably.

## Neighborhood Expansion Policy

Neighborhood expansion is intentionally bounded and policy-driven.

### Link generation

Deterministic links:

- shared `session_id`
- shared `handoff_id`
- shared `working_memory_thread_id`
- shared `link_group_id`
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

`neighbor_previews` in `memory_query` responses is an ordered per-seed list derived from the globally deduped expanded set. It must be ordered by `global_score` descending, capped at the per-seed expansion limit, and returned as an empty list when `expand_neighbors=false`.

Field semantics:

- `global_score` is the globally arbitrated neighbor score after dedupe and multi-seed bonuses
- `seed_link_type` is the highest-priority link type connecting the current seed to that neighbor
- `seed_link_strength` is the seed-local link strength for the displayed `seed_link_type`
- `support_count` is the number of seed results that nominated the neighbor

If multiple seed-local link types exist for the same neighbor, choose the displayed `seed_link_type` by deterministic priority:

1. deterministic non-time link types
2. `semantic_neighbor`
3. `time_adjacent`

Break ties by stronger seed-local link strength, then lexical link-type name.

This keeps previews stable across runs and avoids duplicate context.

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

Synthetic ID resolution rules:

- compatibility-generated synthetic IDs must be resolvable by `memory_open`
- compatibility-generated synthetic IDs must be expandable by `memory_neighbors`
- each compatibility adapter must therefore register open and neighbor resolvers for its synthetic IDs
- synthetic ID resolution must preserve stable namespacing and source provenance
- if a compatibility resolver cannot open or expand a synthetic ID, it must return typed warnings and the appropriate status rather than an opaque failure

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

- minimum 7 consecutive days of shadow traffic for the family
- minimum 200 shadowed queries in that window
- minimum 50 evaluable shadowed queries for lineage-based usefulness scoring in that same window
- top-3 hit rate must not regress by more than 2 percentage points versus the legacy path
- production-estimated context usefulness must be equal or better versus the legacy path
- p95 latency must not worsen by more than 20 percent
- `memory_open` object resolution must remain stable for migrated families

Shadow-read pass/fail rule:

- the family passes only if all parity metrics pass over the full 7-day window
- any failed metric resets the 7-day parity window after the fix is deployed
- shadow-read context usefulness is computed on the evaluable shadow-query set only
- an evaluable shadow query is one that, during the parity window, has either explicit `memory_feedback` attached to its `query_execution_id` or a lineage trace showing at least one accepted object interaction on the user-visible legacy path
- lineage-derived accepted seed objects are the legacy result objects opened by the user for that `query_execution_id`; lineage-derived accepted neighbors are the neighbor objects subsequently opened or selected from the same seed context within the configured evaluation window
- queries with neither explicit feedback nor accepted lineage are excluded from the usefulness denominator and reported as `unevaluable_shadow_queries`
- `production_estimated_context_usefulness = useful_shadow_queries / evaluable_shadow_queries`
- a shadow query counts as `useful_shadow_queries` only if the shadow result set contains at least one accepted seed object and, when accepted neighbors exist, the shadow result set includes that accepted neighbor set in `neighbor_previews` or `memory_neighbors`
- explicit negative feedback (`irrelevant`, `missing_context`, `missed_expected_result`) marks the shadow query not useful unless a later feedback event on the same `query_execution_id` supersedes it
- the parity gate cannot pass until `evaluable_shadow_queries >= 50`

### Cutover rule

A replayable source family may move from shadow mode to default only when:

- benchmark thresholds are met on the held-out validation window for pre-cutover qualification
- shadow-read parity is maintained for 7 consecutive days
- no severity-1 retrieval regressions are observed for that family in production feedback
- final cutover approval then passes once on the held-out test window

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
- replay each query against an as-of-time corpus snapshot containing only source data and derived artifacts available at the original query timestamp
- generated layers, embeddings, and neighborhood links used in evaluation must be constrained to that same snapshot

As-of-time replay contract:

- each source family must define its historical source of truth for replay
- the evaluation harness must reconstruct or filter source data to the query timestamp before building derived artifacts
- derived artifacts for replay must be rebuilt from that as-of-time source state rather than reused from current live indexes
- if a source family lacks replayable history, it must be excluded from public cutover metrics until a replayable retention path exists

Non-replayable family cutover rule:

- replayable as-of-time history is the default prerequisite for public benchmark-based cutover
- families without full replayability may still qualify for local/private cutover only through this alternate cutover path, which replaces the held-out validation and held-out test benchmark gates for that family
- the alternate local/private path requires 14 consecutive days of shadow-read parity, at least 50 evaluable local shadow queries, stable `memory_open` resolution, no severity-1 regressions, and the same top-1/top-3/false-positive/context-usefulness/p95 acceptance thresholds otherwise required for replayable families
- those local/private parity and acceptance gates are the only cutover authority for non-replayable families until replayable retention exists
- those families must not be counted toward public benchmark claims until replayable retention exists

### Public benchmark sanitization

- raw historical queries, messages, and private object content stay in private local evaluation inputs
- the public repo must contain only sanitized fixtures
- sanitization must replace personal names, handles, phone numbers, emails, account numbers, and other private identifiers with stable placeholders
- sanitized fixtures must preserve query category, structural difficulty, and acceptable-answer relationships
- the private-to-public fixture generation step should live as a dedicated script in the public repo, with local private inputs excluded from version control

### Data split rules

- use time-based splits to reduce leakage
- train/tune on older queries
- validate on a held-out later window for tuning and pre-cutover qualification
- reserve a final test window for one-time family cutover approval

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
- percentage of queries where the returned result set includes an acceptable seed object plus the labeled acceptable neighbor set, without requiring unrelated objects

`average tokens required`
- estimated tokens consumed to reach an acceptable answer path, including the initial query response and any required `memory_open` or `memory_neighbors` calls

Benchmark annotations required for context usefulness:

- acceptable seed object IDs
- acceptable neighbor object IDs, if context is required
- acceptable seed-plus-neighbor sets when multiple combinations are valid

Harness rule:

- a query counts as context-useful only if at least one acceptable seed object is returned and one labeled acceptable context set is fully satisfied by the returned neighbors

### Acceptance criteria

Before default cutover on a source family:

- top-1 hit rate must improve, or stay within 1 percentage point of baseline while top-3 improves by at least 3 percentage points
- false-positive rate in top-3 must not worsen by more than 2 percentage points
- context usefulness rate must improve by at least 5 percentage points
- p95 latency must not worsen by more than 20 percent

### Severity taxonomy

- severity-1: top production query class returns materially wrong or missing results for a migrated family in a way that blocks expected task continuation or decision recall
- severity-2: noticeable quality regression with an available workaround
- severity-3: cosmetic or low-impact mismatch

Severity-1 gate:

- one confirmed severity-1 regression in the 7-day parity window blocks cutover for that family
- confirmation requires either repeated production feedback on the same failure mode or reproducibility on the held-out validation/test benchmark
- the block is cleared only after the regression is fixed and the family re-passes the 7-day parity window

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
