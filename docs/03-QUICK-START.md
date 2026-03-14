# LLM Cortex: Quick Start
### Get running in 30 minutes

---

## 5-Minute Setup (Layer 1: Auto Memory)

```bash
# 1. Create memory file for your project
PROJECT_KEY="-Users-$(whoami)-$(basename $(pwd))"
mkdir -p ~/.claude/projects/${PROJECT_KEY}/memory/
echo "# Project Memory" > ~/.claude/projects/${PROJECT_KEY}/memory/MEMORY.md

# 2. Add to your CLAUDE.md (project root)
cat >> CLAUDE.md << 'EOF'

## Auto Memory
You have persistent memory at ~/.claude/projects/<project>/memory/MEMORY.md
- Always loaded into system prompt (~200 lines max)
- Save confirmed patterns, decisions, recurring solutions
- Create topic files for overflow (link from MEMORY.md)
EOF
```

Done. Claude now has permanent notes across sessions.

---

## 15-Minute Setup (Layer 2-3: Bootstrap + Working Memory)

```bash
# 1. Create directory structure
mkdir -p scripts/ .planning/{handoffs,working-memory} memory/

# 2. Copy working_memory.py and context_loader.py from the Implementation Guide
# Update the CONFIGURE paths in each file to point to your project

# 3. Add SessionStart hook to ~/.claude/settings.json
```

Add to `~/.claude/settings.json`:
```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$HOME/your-project/scripts/context_loader.py\" --hours 48"
          }
        ]
      }
    ]
  }
}
```

Test: `python3 scripts/context_loader.py --hours 48`

Done. Claude now auto-loads context and tracks session state.

---

## 30-Minute Setup (Layer 4-6: Search + Knowledge Graph)

```bash
# 1. Install dependencies
pip install networkx

# 2. Install episodic memory plugin
# Add to ~/.claude/settings.json:
# "enabledPlugins": { "episodic-memory@superpowers-marketplace": true }

# 3. Copy knowledge_graph.py and query_knowledge_graph.py from Implementation Guide
# Update CONFIGURE paths

# 4. Seed your graph
python3 scripts/seed_graph.py

# 5. Test
python3 scripts/query_knowledge_graph.py --stats
```

Done. Claude can search past conversations and query entity relationships.

---

## What Each File Does

| File | Size | Purpose |
|------|------|---------|
| `scripts/working_memory.py` | ~120 lines | Session state (goals, notes, references) |
| `scripts/context_loader.py` | ~150 lines | Auto-loads context at session start |
| `scripts/knowledge_graph.py` | ~150 lines | Entity/relationship graph (NetworkX) |
| `scripts/query_knowledge_graph.py` | ~120 lines | CLI for graph queries |
| `scripts/hybrid_search.py` | ~100 lines | Multi-source search fusion |
| `scripts/rlm_graph.py` | ~150 lines | Recursive queries for large contexts |
| `scripts/seed_graph.py` | ~20 lines | Initial graph population |

---

## Common Commands

```bash
# Query knowledge graph
python3 scripts/query_knowledge_graph.py --stats
python3 scripts/query_knowledge_graph.py --related-to "entity"
python3 scripts/query_knowledge_graph.py --find-path "entity1" "entity2"
python3 scripts/query_knowledge_graph.py --subgraph "entity" --depth 2
python3 scripts/query_knowledge_graph.py --search "keyword"

# Bootstrap context manually
python3 scripts/context_loader.py --hours 48

# RLM-Graph recursive search
python3 scripts/rlm_graph.py "How is X connected to Y?"
```

---

## Troubleshooting

**Hook not firing?**
- Check `~/.claude/settings.json` has valid JSON
- Test script directly: `python3 scripts/context_loader.py --hours 48`
- Check the script path is absolute or uses `$HOME`

**Knowledge graph empty?**
- Run `python3 scripts/seed_graph.py` first
- Check `.planning/knowledge-graph.json` exists

**Episodic memory not finding conversations?**
- Plugin needs time to index existing conversations
- Try both vector and text search modes
- Use `mode: "both"` for best coverage

**Working memory not loading?**
- Check `.planning/working-memory/` has session directories
- Most recent session directory is loaded automatically
- Sessions are named `session-{key}` - verify naming

**Import errors?**
- Make sure you're running from the `scripts/` directory
- Or add `sys.path.insert(0, str(Path(__file__).parent))` to imports
- `pip install networkx` for knowledge graph

---

## Architecture Diagram

```
SESSION START
    |
    v
[SessionStart Hook] --> context_loader.py
    |                        |
    |   Scans:               |
    |   - memory/*.md        |
    |   - .planning/handoffs |
    |   - working-memory     |
    |                        |
    v                        v
[MEMORY.md loaded]     [Context injected]
    |                        |
    +--------+---------------+
             |
             v
    Claude Code Session
    (has: time, goals, recent work, notes)
             |
             |--- User asks about past decision
             |    --> episodic-memory search
             |    --> reads past conversation
             |
             |--- User asks about relationships
             |    --> knowledge_graph.py
             |    --> query_knowledge_graph.py
             |
             |--- Complex multi-entity query
             |    --> rlm_graph.py (recursive)
             |    --> hybrid_search.py (fusion)
             |
             |--- Claude learns something important
             |    --> writes to MEMORY.md
             |    --> updates knowledge graph
             |
             v
    SESSION END
    (working memory archived or cleaned up)
```
