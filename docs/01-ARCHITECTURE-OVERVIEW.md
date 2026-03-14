# LLM Cortex: Architecture Guide
### How the 7-Layer Memory Stack Works

**Based on:** Production system running since January 2026
**Status:** 267+ knowledge graph nodes, 231+ relationships, 48h automatic context loading

---

## The Problem

Out of the box, Claude Code has **zero memory between sessions**. Every conversation starts from scratch. It doesn't know:
- What you were working on yesterday
- What decisions you made last week
- Who your team members are
- What your project architecture looks like
- What time it actually is

This system fixes all of that with 7 layers of memory that stack on top of each other.

---

## The 7-Layer Memory Stack

```
Layer 7: RLM-Graph ──────── Recursive queries for large contexts
Layer 6: Knowledge Graph ── Entities + relationships, queryable
Layer 5: Hybrid Search ──── Vector + keyword + graph fusion
Layer 4: Episodic Memory ── Search past Claude Code conversations
Layer 3: Working Memory ─── Active session state (goals, scratchpad)
Layer 2: Session Bootstrap ─ Auto-loads recent context on startup
Layer 1: Auto Memory ────── MEMORY.md (permanent notes in prompt)
```

Each layer solves a different problem:

| Layer | What It Does | Persistence | When Used |
|-------|-------------|-------------|-----------|
| **Auto Memory** | Permanent notes Claude always sees | Forever | Every session (in system prompt) |
| **Session Bootstrap** | Loads what you were working on | 48h rolling window | Session start (automatic) |
| **Working Memory** | Tracks current goals, notes, state | Per-session | During active work |
| **Episodic Memory** | Searches past conversations | Forever | When recalling past decisions |
| **Hybrid Search** | Finds anything across all sources | Forever | Complex queries |
| **Knowledge Graph** | Maps people, projects, relationships | Forever | Relationship queries |
| **RLM-Graph** | Handles queries too large for context | N/A (query layer) | Complex multi-entity queries |

---

## How They Work Together

### Automatic Session Start
```
Claude Code launches
  |
  v
SessionStart hook fires automatically
  |
  v
context_loader.py runs:
  - Scans memory files from last 48 hours
  - Loads working memory (goals, scratchpad, references)
  - Detects ongoing technical work
  - Finds incomplete items and TODOs
  - Injects verified current time
  |
  v
MEMORY.md loads into system prompt (permanent notes)
  |
  v
Claude receives ALL of this before your first message
  |
  v
Claude knows: what time it is, what you were working on,
              what's pending, and your long-term notes
```

### Recalling Past Decisions
```
You ask: "What did I decide about the auth approach last month?"
  |
  v
Claude uses episodic memory to search past conversations
  |
  v
Finds 3 matching sessions with similarity scores (78%, 65%, 52%)
  |
  v
Reads the relevant section from the highest-scoring conversation
  |
  v
"On January 15th, you chose JWT with refresh tokens because..."
```

### Querying Relationships
```
You ask: "How is Alice connected to ProjectAlpha?"
  |
  v
Knowledge Graph finds entities: [Alice, ProjectAlpha]
  |
  v
Graph traversal: Alice -> Acme Corp -> ProjectAlpha
  |
  v
Hybrid Search finds mentions in memory files + past conversations
  |
  v
If context is too large, RLM-Graph partitions by graph structure
  |
  v
"Alice is connected through Acme Corp - she's a consultant
 who works with Acme Corp, which sponsors ProjectAlpha..."
```

---

## Layer Details

### Layer 1: Auto Memory (MEMORY.md)

**What:** A markdown file always loaded into Claude's system prompt. Claude's permanent notepad.

**Location:** `~/.claude/projects/<project-path>/memory/MEMORY.md`

**How it works:**
- Claude Code automatically loads MEMORY.md at every session start
- Claude can read/write to it using standard file tools
- First ~200 lines are always in context (hard limit from Claude Code)
- Create topic files for overflow (e.g., `debugging.md`, `architecture.md`)

**Best for:** Confirmed patterns, key decisions, important paths, recurring solutions

**Not for:** Temporary state, large data, conversation history

---

### Layer 2: Session Bootstrap (context_loader.py)

**What:** A Python script that runs automatically via a `SessionStart` hook. Scans recent files and injects relevant context before your first message.

**Key capabilities:**
- **Time Verification** - Injects actual timestamp so Claude never guesses the date
- **Recent File Detection** - Finds memory files modified in last 48 hours
- **Technical Work Detection** - Regex patterns detect ongoing debug/deploy/test work
- **Incomplete Item Scanning** - Finds TODOs, action items, follow-ups
- **Handoff Loading** - Previous sessions can leave structured context for the next one
- **Working Memory Restoration** - Loads active goals and scratchpad from last session

**Output example:**
```
============================================================
TIME VERIFIED: 04:46 PM on Wednesday, 2026-02-11
============================================================

Working Memory (Active Session):
  Active Goals (1):
    [!] Debug authentication module [high]
  Scratchpad: 3 notes
    [16:22:20] [debug] Found JWT validation bug...

Recent Activity (last 48h):
  Technical work in progress:
    1. auth-module.md: debug|troubleshoot|investigate
  Incomplete items (4):
    1. [auth-module.md] TODO: Add refresh token rotation...
```

---

### Layer 3: Working Memory (working_memory.py)

**What:** Active session state tracking. Like Claude's short-term memory / mental scratchpad.

**Four components:**

| Component | Purpose | Example |
|-----------|---------|---------|
| **Goals** | What Claude is working on | "Debug auth module" (priority: high) |
| **Scratchpad** | Timestamped observations | "[16:22] Found bug in token validation" |
| **State** | Key-value session variables | `files_read: ["auth.py", "tokens.py"]` |
| **References** | Pointers to important context | `git_repo: /path/to/project` |

**Why it matters:** When Claude's context window compresses (hits token limit), it loses earlier conversation. Working memory persists to disk and reloads, so goals and discoveries survive.

**Lifecycle:** Load at session start -> Update during work -> Archive or cleanup at session end

---

### Layer 4: Episodic Memory (Plugin)

**What:** Searchable index of all past Claude Code conversations. Uses the `episodic-memory` plugin.

**Search modes:**
- **Semantic** (vector): Find by meaning ("auth design decisions")
- **Text** (keyword): Find exact matches ("JWT refresh token")
- **Multi-concept AND**: Find conversations matching ALL concepts (["auth", "JWT", "decision"])

**How it works:**
1. Plugin indexes all Claude Code conversations into SQLite + vector embeddings
2. Search returns ranked results with similarity scores
3. Read specific line ranges from matching conversations (pagination for large logs)
4. Extract the full reasoning and decision context

---

### Layer 5: Hybrid Search (hybrid_search.py)

**What:** Combines three search methods for maximum recall and precision.

| Method | What It Finds | Score Priority |
|--------|-------------|----------------|
| **Vector** (semantic) | Conceptually similar content | Highest |
| **Graph** (relationship) | Connected entities from knowledge graph | Medium |
| **Keyword** (exact match) | Literal text matches | Lowest |

Results are merged, deduplicated, and ranked. This prevents missing results that one method alone would miss.

---

### Layer 6: Knowledge Graph (knowledge_graph.py)

**What:** An entity-relationship graph stored as a NetworkX MultiDiGraph. Makes implicit connections explicit and queryable.

**What you can store:**
```
Entities (nodes):
  - People: "Alice", "Bob", "Charlie"
  - Projects: "ProjectAlpha", "ProjectBeta"
  - Companies: "Acme Corp", "StartupXYZ"
  - Systems: "PaymentEngine", "RiskManager"
  - Anything else: servers, accounts, databases

Relationships (edges):
  - Alice -> knows -> Bob (context: "co-founder")
  - Alice -> develops -> ProjectAlpha (context: "lead developer")
  - ProjectAlpha -> implements -> RiskManager (context: "core module")
  - ProjectBeta -> blocked_by -> APIProvider (context: "rate limits")
```

**What you can query:**
```bash
# Get all relationships for an entity
python3 query_knowledge_graph.py --related-to "alice"

# Find connection path between two entities
python3 query_knowledge_graph.py --find-path "alice" "projectalpha"

# Get subgraph around an entity (depth 2)
python3 query_knowledge_graph.py --subgraph "projectalpha" --depth 2

# Search by keyword
python3 query_knowledge_graph.py --search "acme"

# Graph statistics
python3 query_knowledge_graph.py --stats
```

---

### Layer 7: RLM-Graph (rlm_graph.py)

**What:** Recursive Learning Machine. Handles queries that exceed context limits by partitioning using graph structure.

**The problem:** A query like "How is everything connected to Alice?" might involve 100+ entities, thousands of relationships, and dozens of search results - way more than fits in a single LLM context window.

**Traditional approach:** Truncate randomly. Lose important context.

**RLM-Graph approach:** Use the knowledge graph's topology to create semantically meaningful partitions:

```
Query: "How is Alice connected to FundAdmin Inc?"

1. Estimate context size: ~6,200 tokens (exceeds 4,000 limit)
2. Choose strategy: PATH_DECOMPOSITION (multi-entity path query)
3. Partition:
   - SubQuery 1: Alice -> Acme Corp (ego graph)
   - SubQuery 2: Acme Corp -> FundAdmin Inc (ego graph)
4. Execute each subquery (context within limits now)
5. Aggregate: merge, deduplicate, rank by score + depth penalty
6. Return: unified result from 2 subqueries, 2,100 tokens total
```

**Four partition strategies:**

| Strategy | When Used | How It Works |
|----------|-----------|-------------|
| **Ego Graph** | Single entity deep dive | Split by entity neighborhoods |
| **Path Decomposition** | Multi-entity connection queries | Split path into segments |
| **Connected Components** | Many unrelated entities | Split by graph clusters |
| **Entity Chunks** | Fallback | Fixed-size splits |

---

## File Structure

```
your-project/
  |-- CLAUDE.md                    # Project instructions (reference memory system)
  |-- scripts/
  |   |-- context_loader.py        # Layer 2: Session bootstrap
  |   |-- working_memory.py        # Layer 3: Session state
  |   |-- knowledge_graph.py       # Layer 6: Entity/relationship graph
  |   |-- query_knowledge_graph.py # Layer 6: CLI query tool
  |   |-- hybrid_search.py         # Layer 5: Multi-source search
  |   +-- rlm_graph.py             # Layer 7: Recursive queries
  |-- .planning/
  |   |-- knowledge-graph.json     # Graph data file
  |   |-- working-memory/          # Session state directories
  |   +-- handoffs/                # Session handoff documents
  +-- memory/
      |-- YYYY-MM-DD.md            # Daily logs (optional)
      +-- contacts/                # People profiles (optional)

~/.claude/
  |-- settings.json                # SessionStart hook config
  +-- projects/<project>/memory/
      +-- MEMORY.md                # Layer 1: Permanent notes
```

---

## Implementation Phases

### Phase 1: Instant Wins (30 minutes)
1. Set up MEMORY.md (built into Claude Code, just create the file)
2. Create context_loader.py with SessionStart hook
3. Add working memory for session state tracking

**Result:** Claude remembers what you were working on and knows what time it is.

### Phase 2: Searchable History (1-2 hours)
4. Install episodic-memory plugin
5. Create knowledge graph + seed with your entities
6. Set up hybrid search

**Result:** Claude can find past decisions and query entity relationships.

### Phase 3: Advanced Intelligence (half day)
7. Implement RLM-Graph for recursive queries
8. Build out the knowledge graph with more entities/relationships
9. Add custom domain extensions (e.g., project dependencies, team structure)

**Result:** Claude can answer complex multi-entity questions that exceed context limits.

---

## Key Design Principles

1. **Layers are independent** - Implement any layer without the others
2. **Graceful degradation** - If one layer fails, the rest keep working
3. **Disk-backed** - Everything persists to files/SQLite, survives restarts
4. **Token-conscious** - MEMORY.md has ~200 line limit; RLM-Graph manages overflow
5. **Time-verified** - Never let the LLM guess the date; inject actual timestamps
6. **Zero manual effort** - Session bootstrap requires no user intervention
7. **Composable** - Each layer feeds into the others (graph -> search -> RLM)
