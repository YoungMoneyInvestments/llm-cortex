# LLM Cortex: Implementation Guide
### Step-by-step setup for any project

---

## Prerequisites

- Claude Code CLI installed and working
- Python 3.10+
- `pip install networkx` (for Knowledge Graph)
- A project directory to work from

---

## Phase 1: Foundation (30 minutes)

### Step 1.1: Enable Auto Memory (MEMORY.md)

This is built into Claude Code. You just need to create the file and tell Claude about it.

```bash
# Figure out your project memory path
# Claude Code encodes the full path with dashes
# Example: /Users/yourname/myproject → -Users-yourname-myproject
PROJECT_KEY="-Users-$(whoami)-$(basename $(pwd))"

# Create the memory directory
mkdir -p ~/.claude/projects/${PROJECT_KEY}/memory/

# Create the memory file
touch ~/.claude/projects/${PROJECT_KEY}/memory/MEMORY.md
```

Add this to your project's `CLAUDE.md` (create at project root):

```markdown
## Auto Memory

You have a persistent auto memory directory. Its contents persist across conversations.

As you work, consult your memory files to build on previous experience. When you encounter
something that could be useful later, record what you learned.

Guidelines:
- MEMORY.md is always loaded into your system prompt (~200 lines max)
- Create separate topic files for detailed notes (e.g., debugging.md, patterns.md)
- Link to topic files from MEMORY.md
- Update or remove memories that turn out to be wrong

What to save:
- Stable patterns confirmed across multiple interactions
- Key architectural decisions and important file paths
- User preferences for workflow and tools
- Solutions to recurring problems

What NOT to save:
- Session-specific context or in-progress work
- Speculative conclusions from reading a single file
- Anything that duplicates CLAUDE.md instructions
```

**That's it.** Claude will now read MEMORY.md at every session start and can write to it.

---

### Step 1.2: Create the Scripts Directory

```bash
# Create the directory structure
mkdir -p ~/your-project/scripts/
mkdir -p ~/your-project/.planning/handoffs/
mkdir -p ~/your-project/.planning/working-memory/
mkdir -p ~/your-project/memory/
```

---

### Step 1.3: Create Working Memory

Create `~/your-project/scripts/working_memory.py`:

```python
#!/usr/bin/env python3
"""
Working Memory - Active Session State Tracking

The "mental scratchpad" for AI sessions. Persists to disk, survives
context window compression and restarts.

Usage:
    from working_memory import WorkingMemory
    wm = WorkingMemory("session-main-20260211")
    wm.add_goal("Fix auth bug", priority="high")
    wm.add_scratchpad_note("Found issue in token validation")
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# ============================================
# CONFIGURE: Change this to your project path
# ============================================
PLANNING_DIR = Path(os.environ.get(
    "MEMORY_PLANNING_DIR",
    str(Path.home() / "your-project" / ".planning")
))
WORKING_MEMORY_DIR = PLANNING_DIR / "working-memory"


class WorkingMemory:
    """Active session state management."""

    def __init__(self, session_key: str):
        self.session_key = session_key
        self.session_dir = WORKING_MEMORY_DIR / f"session-{session_key}"
        self.session_dir.mkdir(parents=True, exist_ok=True)

        self.goals_file = self.session_dir / "active-goals.json"
        self.scratchpad_file = self.session_dir / "scratchpad.md"
        self.state_file = self.session_dir / "state.json"
        self.references_file = self.session_dir / "references.json"

        self.goals = self._load_json(self.goals_file, default=[])
        self.state = self._load_json(self.state_file, default={})
        self.references = self._load_json(self.references_file, default={})

        if "session_created_at" not in self.state:
            self.update_state("session_created_at", datetime.now().isoformat())
        self.verify_time()

    def _load_json(self, filepath: Path, default: Any) -> Any:
        if filepath.exists():
            try:
                with open(filepath) as f:
                    return json.load(f)
            except json.JSONDecodeError:
                return default
        return default

    def _save_json(self, filepath: Path, data: Any):
        with open(filepath, 'w') as f:
            json.dump(data, f, indent=2)

    # === GOALS ===

    def add_goal(self, goal: str, priority: str = "normal",
                 context: Optional[str] = None):
        """Add active goal. Priority: low, normal, high, urgent."""
        goal_obj = {
            "id": len(self.goals) + 1,
            "goal": goal,
            "priority": priority,
            "status": "active",
            "created_at": datetime.now().isoformat(),
            "context": context,
            "subgoals": []
        }
        self.goals.append(goal_obj)
        self._save_json(self.goals_file, self.goals)
        return goal_obj["id"]

    def add_subgoal(self, parent_goal_id: int, subgoal: str):
        for goal in self.goals:
            if goal["id"] == parent_goal_id:
                goal["subgoals"].append({
                    "subgoal": subgoal,
                    "completed": False,
                    "added_at": datetime.now().isoformat()
                })
                self._save_json(self.goals_file, self.goals)
                return True
        return False

    def complete_goal(self, goal_id: int):
        for goal in self.goals:
            if goal["id"] == goal_id:
                goal["status"] = "completed"
                goal["completed_at"] = datetime.now().isoformat()
                self._save_json(self.goals_file, self.goals)
                return True
        return False

    def get_active_goals(self) -> List[Dict]:
        return [g for g in self.goals if g["status"] == "active"]

    # === SCRATCHPAD (Mental Notes) ===

    def add_scratchpad_note(self, note: str, category: str = "general"):
        """Timestamped note to the scratchpad."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        with open(self.scratchpad_file, 'a') as f:
            f.write(f"[{timestamp}] [{category}] {note}\n")

    def read_scratchpad(self) -> str:
        if self.scratchpad_file.exists():
            return self.scratchpad_file.read_text()
        return ""

    def clear_scratchpad(self):
        if self.scratchpad_file.exists():
            self.scratchpad_file.unlink()

    # === STATE (Key-Value Store) ===

    def update_state(self, key: str, value: Any):
        self.state[key] = {
            "value": value,
            "updated_at": datetime.now().isoformat()
        }
        self._save_json(self.state_file, self.state)

    def get_state(self, key: str, default: Any = None) -> Any:
        if key in self.state:
            return self.state[key]["value"]
        return default

    def increment_counter(self, key: str, amount: int = 1) -> int:
        current = self.get_state(key, 0)
        new_value = current + amount
        self.update_state(key, new_value)
        return new_value

    # === TIME VERIFICATION ===

    def verify_time(self) -> Dict:
        """Record verified current time. Prevents LLM date guessing."""
        now = datetime.now()
        time_info = {
            "timestamp": now.isoformat(),
            "time_str": now.strftime("%I:%M %p"),
            "date_str": now.strftime("%Y-%m-%d"),
            "day_of_week": now.strftime("%A"),
        }
        self.update_state("last_time_verified", time_info)
        return time_info

    # === REFERENCES (Context Pointers) ===

    def add_reference(self, name: str, value: str, ref_type: str = "path"):
        """Add reference. ref_type: path, url, entity, node_id"""
        self.references[name] = {
            "value": value,
            "type": ref_type,
            "added_at": datetime.now().isoformat()
        }
        self._save_json(self.references_file, self.references)

    def get_reference(self, name: str) -> Optional[str]:
        if name in self.references:
            return self.references[name]["value"]
        return None

    def list_references(self) -> Dict:
        return self.references

    # === LIFECYCLE ===

    def summarize(self) -> Dict:
        return {
            "session_key": self.session_key,
            "active_goals": len(self.get_active_goals()),
            "total_goals": len(self.goals),
            "state_variables": len(self.state),
            "references": len(self.references),
        }

    def archive(self):
        archive_dir = WORKING_MEMORY_DIR / "archive" / "completed"
        archive_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        archive_path = archive_dir / f"{self.session_key}-{timestamp}"
        import shutil
        if self.session_dir.exists():
            shutil.move(str(self.session_dir), str(archive_path))
        return archive_path

    def cleanup(self, archive: bool = True):
        if archive:
            return self.archive()
        import shutil
        if self.session_dir.exists():
            shutil.rmtree(self.session_dir)
```

---

### Step 1.4: Create the Session Bootstrap

Create `~/your-project/scripts/context_loader.py`:

```python
#!/usr/bin/env python3
"""
Session Memory Bootstrap - Runs at session start via hook.
Scans recent memory files and injects context automatically.

Usage:
    python3 context_loader.py [--hours N]
"""

import os
import sys
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple

from working_memory import WorkingMemory

# ============================================
# CONFIGURE: Change these to your project paths
# ============================================
WORKSPACE = Path(os.environ.get(
    "MEMORY_WORKSPACE",
    str(Path.home() / "your-project")
))
MEMORY_DIR = WORKSPACE / "memory"
PLANNING_DIR = WORKSPACE / ".planning"
HANDOFF_DIR = PLANNING_DIR / "handoffs"
WORKING_MEMORY_DIR = PLANNING_DIR / "working-memory"

# Patterns that indicate ongoing technical work
TECHNICAL_PATTERNS = [
    r'config.*fix|fix.*config',
    r'debug|troubleshoot|investigate',
    r'implement|deploy|launch',
    r'error|fail|broken|issue',
    r'test.*run|run.*test',
    r'git.*commit|commit.*push',
]

# Patterns indicating incomplete work
INCOMPLETE_PATTERNS = [
    r'\btodo\b|action item|follow.?up',
    r'\bincomplete\b|\bunfinished\b|\bpending\b',
    r'next step|next:',
]


class MemoryBootstrap:
    def __init__(self, hours: int = 48):
        self.hours = hours
        self.cutoff = datetime.now() - timedelta(hours=hours)
        self.recent_files: List[Path] = []
        self.technical_work: List[Dict] = []
        self.incomplete_items: List[Dict] = []
        self.working_memory: Optional[WorkingMemory] = None
        self._load_working_memory()

    def _load_working_memory(self):
        """Load the most recent working memory session."""
        if not WORKING_MEMORY_DIR.exists():
            return
        session_dirs = [
            d for d in WORKING_MEMORY_DIR.iterdir()
            if d.is_dir() and d.name.startswith("session-")
        ]
        if not session_dirs:
            return
        most_recent = max(session_dirs, key=lambda d: d.stat().st_mtime)
        session_key = most_recent.name.replace("session-", "")
        try:
            self.working_memory = WorkingMemory(session_key)
        except Exception:
            self.working_memory = None

    def find_recent_memory_files(self) -> List[Path]:
        """Find memory files modified within the time window."""
        files = []
        if MEMORY_DIR.exists():
            for f in MEMORY_DIR.rglob("*.md"):
                try:
                    if 'archive' in f.parts:
                        continue
                    mtime = datetime.fromtimestamp(f.stat().st_mtime)
                    if mtime > self.cutoff:
                        files.append(f)
                except Exception:
                    continue
        return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)

    def find_recent_handoffs(self) -> List[Dict]:
        """Find session handoff documents."""
        if not HANDOFF_DIR.exists():
            return []
        handoffs = []
        for f in HANDOFF_DIR.glob("*.md"):
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime)
                if mtime > self.cutoff:
                    content = f.read_text(encoding='utf-8')
                    handoffs.append({
                        "name": f.name,
                        "mtime": mtime.isoformat(),
                        "preview": content[:1000],
                    })
            except Exception:
                continue
        return sorted(handoffs, key=lambda h: h["mtime"], reverse=True)

    def generate_summary(self) -> str:
        """Generate context summary injected at session start."""
        self.recent_files = self.find_recent_memory_files()
        handoffs = self.find_recent_handoffs()

        # === TIME VERIFICATION (Critical!) ===
        now = datetime.now()
        lines = []
        lines.append("=" * 60)
        lines.append(
            f"TIME VERIFIED: {now.strftime('%I:%M %p')} "
            f"on {now.strftime('%A, %Y-%m-%d')}"
        )
        lines.append("=" * 60)
        lines.append("")

        # === WORKING MEMORY ===
        if self.working_memory:
            active_goals = self.working_memory.get_active_goals()
            if active_goals:
                lines.append("Working Memory (Active Session):")
                lines.append(f"  Active Goals ({len(active_goals)}):")
                for goal in active_goals[:5]:
                    marker = "[!]" if goal['priority'] in ['high', 'urgent'] else "[ ]"
                    lines.append(f"    {marker} {goal['goal']} [{goal['priority']}]")
                lines.append("")

            scratchpad = self.working_memory.read_scratchpad()
            if scratchpad:
                notes = scratchpad.strip().split('\n')
                lines.append(f"  Scratchpad ({len(notes)} notes):")
                for note in notes[-3:]:  # Last 3 notes
                    lines.append(f"    {note[:80]}")
                lines.append("")

        # === RECENT ACTIVITY ===
        lines.append(f"Recent Activity (last {self.hours}h):")

        # Handoffs (highest priority - read these first!)
        if handoffs:
            lines.append(f"\n  Session handoffs ({len(handoffs)}):")
            for h in handoffs[:3]:
                lines.append(f"    - {h['name']}")
            lines.append("    -> Read these first for full context")

        # Detect technical work in progress
        for filepath in self.recent_files:
            try:
                content = filepath.read_text()
                content_lower = content.lower()
                for pattern in TECHNICAL_PATTERNS:
                    if re.search(pattern, content_lower):
                        self.technical_work.append({
                            "file": filepath.name,
                            "pattern": pattern
                        })
                        break
            except Exception:
                continue

        if self.technical_work:
            lines.append(f"\n  Technical work in progress:")
            for i, work in enumerate(self.technical_work[:5], 1):
                lines.append(f"    {i}. {work['file']}: {work['pattern']}")

        # Find incomplete items
        for filepath in self.recent_files:
            try:
                content = filepath.read_text()
                for line_text in content.split('\n'):
                    stripped = line_text.strip()
                    if not stripped or stripped.startswith('#'):
                        continue
                    if '[x]' in stripped.lower():
                        continue
                    for pattern in INCOMPLETE_PATTERNS:
                        if re.search(pattern, line_text.lower()):
                            self.incomplete_items.append({
                                "line": stripped[:80],
                                "file": filepath.name,
                            })
                            break
            except Exception:
                continue

        if self.incomplete_items:
            lines.append(f"\n  Incomplete items ({len(self.incomplete_items)}):")
            for i, item in enumerate(self.incomplete_items[:5], 1):
                lines.append(f"    {i}. [{item['file']}] {item['line']}...")

        if not self.recent_files and not handoffs:
            lines.append("  No recent activity found. Fresh start.")

        return '\n'.join(lines)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--hours', type=int, default=48)
    args = parser.parse_args()
    print(MemoryBootstrap(hours=args.hours).generate_summary())


if __name__ == "__main__":
    main()
```

---

### Step 1.5: Wire Up the SessionStart Hook

Edit `~/.claude/settings.json`:

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

**Test it:**
```bash
cd ~/your-project/scripts && python3 context_loader.py --hours 48
```

Expected output:
```
============================================================
TIME VERIFIED: 04:46 PM on Wednesday, 2026-02-11
============================================================

Recent Activity (last 48h):
  No recent activity found. Fresh start.
```

**Phase 1 is done.** Claude now loads context automatically and tracks session state.

---

## Phase 2: Searchable History & Knowledge Graph

### Step 2.1: Install Episodic Memory Plugin

In Claude Code, the `episodic-memory` plugin from the Superpowers marketplace indexes and searches past conversations.

Add to `~/.claude/settings.json`:
```json
{
  "enabledPlugins": {
    "episodic-memory@superpowers-marketplace": true
  }
}
```

This provides two MCP tools Claude can use:
- `episodic-memory__search` - Search past conversations (vector, text, or both)
- `episodic-memory__read` - Read full conversation content with line pagination

Add to your `CLAUDE.md`:
```markdown
## Episodic Memory

You have memory across sessions via the episodic-memory plugin.
Search your history before starting any task to recover past decisions.

Usage:
- Single string for semantic search: "auth approach decision"
- Array of 2-5 concepts for AND matching: ["auth", "JWT", "decision"]
- Use read with startLine/endLine pagination for large conversations
```

---

### Step 2.2: Create the Knowledge Graph

Create `~/your-project/scripts/knowledge_graph.py`:

```python
#!/usr/bin/env python3
"""
Knowledge Graph - Explicit entity/relationship tracking.

Converts implicit relationships into a queryable graph.
Stores as JSON-serialized NetworkX MultiDiGraph.

Dependencies: pip install networkx

Usage:
    from knowledge_graph import KnowledgeGraph
    kg = KnowledgeGraph()
    kg.add_entity("alice", "person", role="founder")
    kg.add_relationship("alice", "develops", "project-alpha",
                       context="lead developer")
"""

import json
import os
import networkx as nx
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

# ============================================
# CONFIGURE: Change this to your project path
# ============================================
GRAPH_FILE = Path(os.environ.get(
    "KNOWLEDGE_GRAPH_FILE",
    str(Path.home() / "your-project" / ".planning" / "knowledge-graph.json")
))


class KnowledgeGraph:
    """Entity-relationship graph for memory connections."""

    def __init__(self):
        self.graph = nx.MultiDiGraph()  # Multiple edges between nodes OK
        self.load()

    def load(self):
        """Load graph from JSON file."""
        if GRAPH_FILE.exists():
            try:
                with open(GRAPH_FILE) as f:
                    data = json.load(f)
                for node in data.get("nodes", []):
                    self.graph.add_node(node["id"], **node.get("attributes", {}))
                for edge in data.get("edges", []):
                    self.graph.add_edge(
                        edge["source"], edge["target"],
                        key=edge.get("key"),
                        rel_type=edge.get("rel_type", edge.get("relation", "related")),
                        **edge.get("attributes", {})
                    )
            except Exception as e:
                print(f"Error loading knowledge graph: {e}")

    def save(self):
        """Save graph to JSON file."""
        GRAPH_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "nodes": [
                {"id": node, "attributes": dict(attrs)}
                for node, attrs in self.graph.nodes(data=True)
            ],
            "edges": [
                {
                    "source": u, "target": v, "key": k,
                    "rel_type": d.get("rel_type"),
                    "attributes": {
                        key: val for key, val in d.items()
                        if key != "rel_type"
                    }
                }
                for u, v, k, d in self.graph.edges(keys=True, data=True)
            ],
            "updated_at": datetime.now().isoformat()
        }
        with open(GRAPH_FILE, 'w') as f:
            json.dump(data, f, indent=2)

    # === ENTITIES ===

    def add_entity(self, entity_id: str, entity_type: str, **attributes):
        """Add entity node. Type examples: person, project, company, system."""
        self.graph.add_node(entity_id, type=entity_type, **attributes)
        self.save()

    def get_entity(self, entity_id: str) -> Optional[Dict]:
        if entity_id in self.graph:
            return dict(self.graph.nodes[entity_id])
        return None

    def entity_exists(self, entity_id: str) -> bool:
        return entity_id in self.graph

    # === RELATIONSHIPS ===

    def add_relationship(self, source: str, rel_type: str, target: str,
                        context: Optional[str] = None,
                        strength: float = 1.0, **metadata):
        """Add directed relationship. Auto-creates entities if missing."""
        if not self.entity_exists(source):
            self.add_entity(source, "unknown")
        if not self.entity_exists(target):
            self.add_entity(target, "unknown")
        self.graph.add_edge(
            source, target,
            rel_type=rel_type,
            context=context,
            strength=strength,
            created_at=datetime.now().isoformat(),
            **metadata
        )
        self.save()

    def get_relationships(self, entity: str,
                         rel_type: Optional[str] = None,
                         direction: str = "out") -> List[Dict]:
        """Get relationships. direction: 'out', 'in', or 'both'."""
        relationships = []
        if direction in ("out", "both"):
            for _, target, data in self.graph.edges(entity, data=True):
                if rel_type is None or data.get("rel_type") == rel_type:
                    relationships.append({
                        "source": entity, "target": target,
                        "type": data.get("rel_type"),
                        "direction": "outgoing",
                        **data
                    })
        if direction in ("in", "both"):
            for source, _, data in self.graph.in_edges(entity, data=True):
                if rel_type is None or data.get("rel_type") == rel_type:
                    relationships.append({
                        "source": source, "target": entity,
                        "type": data.get("rel_type"),
                        "direction": "incoming",
                        **data
                    })
        return relationships

    def find_path(self, source: str, target: str,
                  max_hops: int = 3) -> Optional[List]:
        """Find shortest path between entities."""
        try:
            path = nx.shortest_path(self.graph, source, target)
            if len(path) <= max_hops + 1:
                return path
        except nx.NetworkXNoPath:
            pass
        return None

    def get_neighbors(self, entity: str, hops: int = 1) -> List[str]:
        """Get entities within N hops."""
        if entity not in self.graph:
            return []
        ego = nx.ego_graph(self.graph, entity, radius=hops)
        return [n for n in ego.nodes() if n != entity]

    def get_subgraph(self, entities: List[str],
                     hops: int = 1) -> 'KnowledgeGraph':
        """Get subgraph containing entities + neighbors."""
        nodes = set(entities)
        for entity in entities:
            if entity in self.graph:
                nodes.update(self.get_neighbors(entity, hops=hops))
        kg = KnowledgeGraph.__new__(KnowledgeGraph)
        kg.graph = self.graph.subgraph(nodes).copy()
        return kg

    def stats(self) -> Dict:
        """Graph statistics."""
        from collections import Counter
        node_types = Counter(
            d.get('type', 'unknown')
            for _, d in self.graph.nodes(data=True)
        )
        return {
            "total_nodes": self.graph.number_of_nodes(),
            "total_edges": self.graph.number_of_edges(),
            "node_types": dict(node_types),
        }
```

---

### Step 2.3: Create the Knowledge Graph CLI

Create `~/your-project/scripts/query_knowledge_graph.py`:

```python
#!/usr/bin/env python3
"""
Knowledge Graph Query Tool

Usage:
  python3 query_knowledge_graph.py --stats
  python3 query_knowledge_graph.py --related-to "entity_name"
  python3 query_knowledge_graph.py --find-path "entity1" "entity2"
  python3 query_knowledge_graph.py --subgraph "entity" --depth 2
  python3 query_knowledge_graph.py --search "keyword"
"""

import json
import os
import argparse
from pathlib import Path
from collections import defaultdict, deque
from typing import List, Dict

GRAPH_PATH = Path(os.environ.get(
    "KNOWLEDGE_GRAPH_FILE",
    str(Path.home() / "your-project" / ".planning" / "knowledge-graph.json")
))


class KnowledgeGraphQuery:
    def __init__(self, graph_path: Path = GRAPH_PATH):
        with open(graph_path) as f:
            data = json.load(f)
        self.nodes = {n['id']: n for n in data['nodes']}
        self.edges = data['edges']
        self.outgoing = defaultdict(list)
        self.incoming = defaultdict(list)
        for edge in self.edges:
            rel = edge.get('rel_type', edge.get('relation', 'related'))
            self.outgoing[edge['source']].append((rel, edge['target']))
            self.incoming[edge['target']].append((rel, edge['source']))

    def search_nodes(self, query: str) -> List[Dict]:
        q = query.lower()
        return [n for n in self.nodes.values() if q in n['id'].lower()]

    def get_relationships(self, node_id: str) -> Dict:
        return {
            'outgoing': [
                {'relation': r, 'target': t}
                for r, t in self.outgoing[node_id]
            ],
            'incoming': [
                {'relation': r, 'source': s}
                for r, s in self.incoming[node_id]
            ],
        }

    def find_path(self, start: str, end: str, max_depth: int = 5) -> List:
        if start not in self.nodes or end not in self.nodes:
            return []
        queue = deque([(start, [])])
        visited = set()
        paths = []
        while queue and len(paths) < 5:
            current, path = queue.popleft()
            if len(path) > max_depth:
                continue
            if current == end:
                paths.append(path)
                continue
            if current in visited:
                continue
            visited.add(current)
            for rel, target in self.outgoing[current]:
                queue.append((target, path + [(current, rel, target)]))
        return paths

    def get_subgraph(self, node_id: str, depth: int = 2) -> Dict:
        nodes_set = {node_id}
        edges_list = []
        queue = deque([(node_id, 0)])
        visited = set()
        while queue:
            current, d = queue.popleft()
            if current in visited or d > depth:
                continue
            visited.add(current)
            nodes_set.add(current)
            for rel, target in self.outgoing[current]:
                edges_list.append({
                    'source': current, 'relation': rel, 'target': target
                })
                if d < depth:
                    queue.append((target, d + 1))
            for rel, source in self.incoming[current]:
                edges_list.append({
                    'source': source, 'relation': rel, 'target': current
                })
                if d < depth:
                    queue.append((source, d + 1))
        return {
            'nodes': [self.nodes[n] for n in nodes_set if n in self.nodes],
            'edges': edges_list
        }

    def stats(self) -> Dict:
        node_types = defaultdict(int)
        for node in self.nodes.values():
            node_types[node.get('attributes', {}).get('type', 'unknown')] += 1
        return {
            'total_nodes': len(self.nodes),
            'total_edges': len(self.edges),
            'node_types': dict(node_types),
        }


def main():
    parser = argparse.ArgumentParser(description='Query knowledge graph')
    parser.add_argument('--related-to', help='Show relationships for entity')
    parser.add_argument('--find-path', nargs=2, metavar=('START', 'END'))
    parser.add_argument('--subgraph', help='Get subgraph around entity')
    parser.add_argument('--depth', type=int, default=2)
    parser.add_argument('--stats', action='store_true')
    parser.add_argument('--search', help='Search nodes by keyword')
    args = parser.parse_args()

    kg = KnowledgeGraphQuery()

    if args.stats:
        s = kg.stats()
        print(f"\nKnowledge Graph Statistics")
        print(f"  Total nodes: {s['total_nodes']}")
        print(f"  Total edges: {s['total_edges']}")
        for t, c in sorted(s['node_types'].items(), key=lambda x: -x[1]):
            print(f"    {t}: {c}")

    elif args.related_to:
        results = kg.search_nodes(args.related_to)
        if not results:
            print(f"Not found: {args.related_to}")
        else:
            node = results[0]
            rels = kg.get_relationships(node['id'])
            print(f"\n{node['id']}")
            for r in rels['outgoing']:
                print(f"  -{r['relation']}-> {r['target']}")
            for r in rels['incoming']:
                print(f"  {r['source']} -{r['relation']}->")

    elif args.find_path:
        s = kg.search_nodes(args.find_path[0])
        e = kg.search_nodes(args.find_path[1])
        if s and e:
            paths = kg.find_path(s[0]['id'], e[0]['id'])
            for i, path in enumerate(paths, 1):
                print(f"  Path {i}:")
                for src, rel, tgt in path:
                    print(f"    {src} -[{rel}]-> {tgt}")
            if not paths:
                print("  No path found")
        else:
            print("  Entity not found")

    elif args.subgraph:
        results = kg.search_nodes(args.subgraph)
        if results:
            sg = kg.get_subgraph(results[0]['id'], depth=args.depth)
            print(f"  Nodes: {len(sg['nodes'])}, Edges: {len(sg['edges'])}")

    elif args.search:
        results = kg.search_nodes(args.search)
        print(f"  Found {len(results)} result(s):")
        for n in results:
            print(f"    - {n['id']} ({n.get('attributes', {}).get('type', '?')})")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
```

---

### Step 2.4: Seed Your Knowledge Graph

Create `~/your-project/scripts/seed_graph.py`:

```python
#!/usr/bin/env python3
"""Seed the knowledge graph with your initial entities and relationships."""

from knowledge_graph import KnowledgeGraph

kg = KnowledgeGraph()

# === ADD YOUR ENTITIES ===
# People
kg.add_entity("you", "person", role="founder")
# kg.add_entity("teammate", "person", role="developer")

# Projects
kg.add_entity("your-project", "project", domain="your-domain")

# Add more: companies, systems, servers, accounts, etc.

# === ADD RELATIONSHIPS ===
kg.add_relationship("you", "develops", "your-project",
                   context="Lead developer")

# === VERIFY ===
stats = kg.stats()
print(f"Graph seeded: {stats['total_nodes']} nodes, {stats['total_edges']} edges")
print("Run: python3 query_knowledge_graph.py --stats")
```

Run it:
```bash
cd ~/your-project/scripts && python3 seed_graph.py
```

---

## Phase 3: Hybrid Search & RLM-Graph

### Step 3.1: Hybrid Search

Create `~/your-project/scripts/hybrid_search.py`:

```python
#!/usr/bin/env python3
"""
Hybrid Search - Keyword + Graph expansion.

Combines keyword matching across memory files with knowledge graph
relationship expansion. Add vector/embedding search when you have
an embedding model configured.
"""

import subprocess
import json
import re
import sys
import os
from pathlib import Path
from typing import List, Dict, Set

sys.path.insert(0, str(Path(__file__).parent))
from knowledge_graph import KnowledgeGraph

WORKSPACE = Path(os.environ.get(
    "MEMORY_WORKSPACE",
    str(Path.home() / "your-project")
))
MEMORY_DIR = WORKSPACE / "memory"


def extract_entities(query: str) -> List[str]:
    """Extract potential entity names from query text."""
    entities = []
    # Multi-word capitalized phrases
    multi = re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b', query)
    entities.extend(multi)
    # Single capitalized words (skip first word - might be sentence start)
    words = query.split()
    for i, word in enumerate(words):
        if i > 0 and word[0].isupper():
            if not any(word in phrase for phrase in multi):
                entities.append(word)
    return list(set(entities))


def keyword_search(query: str, max_results: int = 20) -> List[Dict]:
    """Search memory files by keyword using grep."""
    results = []
    search_paths = [MEMORY_DIR, WORKSPACE / "MEMORY.md"]
    for path in search_paths:
        if not path.exists():
            continue
        try:
            cmd = ["grep", "-r", "-i", "-n", "-C", "2", query]
            if path.is_dir():
                cmd.extend(["--include=*.md", str(path)])
            else:
                cmd.append(str(path))
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10
            )
            if result.stdout:
                for block in result.stdout.split('--'):
                    content = block.strip()
                    if content:
                        results.append({
                            'content': content[:500],
                            'score': 0.5,
                            'source': 'keyword',
                        })
        except Exception:
            continue
    return results[:max_results]


def graph_expansion(entities: List[str], kg: KnowledgeGraph) -> List[Dict]:
    """Expand search via knowledge graph relationships."""
    expanded = []
    for entity in entities:
        if not kg.entity_exists(entity):
            continue
        for rel in kg.get_relationships(entity, direction="both"):
            content = f"{rel['source']} -[{rel['type']}]-> {rel['target']}"
            if rel.get('context'):
                content += f" ({rel['context']})"
            expanded.append({
                'content': content,
                'score': rel.get('strength', 0.7),
                'source': 'graph',
            })
    return expanded


def deduplicate_results(results: List[Dict]) -> List[Dict]:
    seen = set()
    unique = []
    for r in results:
        key = r['content'][:50]
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


def rank_results(results: List[Dict]) -> List[Dict]:
    priority = {'vector': 0, 'graph': 1, 'keyword': 2}
    return sorted(
        results,
        key=lambda x: (priority.get(x['source'], 99), -x['score'])
    )


def hybrid_search(query: str, top_k: int = 5,
                  verbose: bool = False) -> List[Dict]:
    """Combine keyword search and graph expansion."""
    all_results = []
    all_results.extend(keyword_search(query))

    kg = KnowledgeGraph()
    entities = extract_entities(query)
    all_results.extend(graph_expansion(entities, kg))

    unique = deduplicate_results(all_results)
    ranked = rank_results(unique)
    return ranked[:top_k]
```

---

### Step 3.2: RLM-Graph (Recursive Queries)

Create `~/your-project/scripts/rlm_graph.py`:

```python
#!/usr/bin/env python3
"""
RLM-Graph: Recursive queries with graph-based context partitioning.

When context exceeds token limits, partitions using graph structure
and recurses on each partition.
"""

import sys
from pathlib import Path
from typing import List, Dict, Optional
from dataclasses import dataclass, field
from enum import Enum

sys.path.insert(0, str(Path(__file__).parent))
from knowledge_graph import KnowledgeGraph
from hybrid_search import (
    hybrid_search, extract_entities,
    deduplicate_results, rank_results
)


class PartitionStrategy(Enum):
    EGO_GRAPH = "ego_graph"
    PATH_DECOMPOSITION = "path"
    CONNECTED_COMPONENT = "component"


@dataclass
class SubQuery:
    query_text: str
    focal_entities: List[str]
    partition_id: str
    strategy: PartitionStrategy
    depth: int
    parent_query: Optional['SubQuery'] = None


@dataclass
class RLMResult:
    query: str
    results: List[Dict]
    subqueries_executed: List[SubQuery]
    partition_strategy: Optional[PartitionStrategy]
    total_tokens_processed: int
    recursion_depth: int


class RLMGraph:
    """Recursive Learning Machine with graph-based context partitioning."""

    def __init__(self, max_context_tokens: int = 4000,
                 max_recursion_depth: int = 3,
                 verbose: bool = False):
        self.max_context_tokens = max_context_tokens
        self.max_recursion_depth = max_recursion_depth
        self.verbose = verbose
        self.kg = KnowledgeGraph()
        self._history: List[SubQuery] = []
        self._tokens = 0

    def query(self, query_text: str,
              focal_entities: Optional[List[str]] = None) -> RLMResult:
        self._history = []
        self._tokens = 0

        root = SubQuery(
            query_text=query_text,
            focal_entities=focal_entities or extract_entities(query_text),
            partition_id="root",
            strategy=PartitionStrategy.EGO_GRAPH,
            depth=0
        )
        results = self._execute(root)
        final = self._aggregate(results)

        return RLMResult(
            query=query_text,
            results=final,
            subqueries_executed=self._history,
            partition_strategy=self._pick_strategy(root),
            total_tokens_processed=self._tokens,
            recursion_depth=max((sq.depth for sq in self._history), default=0)
        )

    def _execute(self, sq: SubQuery) -> List[Dict]:
        self._history.append(sq)
        ctx_size = self._estimate_context(sq)
        self._tokens += ctx_size

        if ctx_size > self.max_context_tokens:
            if sq.depth >= self.max_recursion_depth:
                return hybrid_search(sq.query_text, top_k=5)
            return self._partition_and_recurse(sq)
        else:
            results = hybrid_search(sq.query_text, top_k=10)
            for r in results:
                r['subquery_depth'] = sq.depth
            return results

    def _estimate_context(self, sq: SubQuery) -> int:
        tokens = len(sq.query_text) // 4
        for e in sq.focal_entities:
            if self.kg.entity_exists(e):
                tokens += len(self.kg.get_neighbors(e, hops=1)) * 50
                tokens += len(self.kg.get_relationships(e, direction="both")) * 30
        return tokens

    def _partition_and_recurse(self, sq: SubQuery) -> List[Dict]:
        strategy = self._pick_strategy(sq)
        if (strategy == PartitionStrategy.PATH_DECOMPOSITION
                and len(sq.focal_entities) >= 2):
            subs = self._by_path(sq)
        else:
            subs = self._by_ego(sq)

        all_results = []
        for child in subs:
            all_results.extend(self._execute(child))
        return deduplicate_results(all_results)

    def _by_ego(self, sq: SubQuery) -> List[SubQuery]:
        return [
            SubQuery(
                query_text=f"{sq.query_text} (focus: {e})",
                focal_entities=[e],
                partition_id=f"{sq.partition_id}.ego_{i}",
                strategy=PartitionStrategy.EGO_GRAPH,
                depth=sq.depth + 1,
                parent_query=sq
            )
            for i, e in enumerate(sq.focal_entities)
            if self.kg.entity_exists(e)
        ]

    def _by_path(self, sq: SubQuery) -> List[SubQuery]:
        valid = [e for e in sq.focal_entities if self.kg.entity_exists(e)]
        if len(valid) < 2:
            return self._by_ego(sq)
        path = self.kg.find_path(valid[0], valid[-1], max_hops=5)
        if not path or len(path) <= 2:
            return self._by_ego(sq)
        return [
            SubQuery(
                query_text=f"{sq.query_text} (segment: {path[i]} -> {path[i+1]})",
                focal_entities=[path[i], path[i+1]],
                partition_id=f"{sq.partition_id}.path_{i}",
                strategy=PartitionStrategy.PATH_DECOMPOSITION,
                depth=sq.depth + 1,
                parent_query=sq
            )
            for i in range(len(path) - 1)
        ]

    def _pick_strategy(self, sq: SubQuery) -> PartitionStrategy:
        if len(sq.focal_entities) >= 2:
            kws = ['connected', 'relationship', 'link', 'between', 'path']
            if any(kw in sq.query_text.lower() for kw in kws):
                return PartitionStrategy.PATH_DECOMPOSITION
        return PartitionStrategy.EGO_GRAPH

    def _aggregate(self, results: List[Dict]) -> List[Dict]:
        unique = deduplicate_results(results)
        for r in unique:
            penalty = r.get('subquery_depth', 0) * 0.05
            r['score'] = max(0, r['score'] - penalty)
        return rank_results(unique)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("query", nargs="+")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    rlm = RLMGraph(verbose=args.verbose)
    result = rlm.query(' '.join(args.query))

    print(f"\nQuery: {result.query}")
    print(f"Strategy: {result.partition_strategy.value if result.partition_strategy else 'none'}")
    print(f"Depth: {result.recursion_depth}")
    print(f"Subqueries: {len(result.subqueries_executed)}")
    print(f"Tokens: {result.total_tokens_processed:,}")
    print(f"\nResults ({len(result.results)}):")
    for i, r in enumerate(result.results[:5], 1):
        print(f"  {i}. [{r['source']}] ({int(r['score']*100)}%)")
        print(f"     {r['content'][:100]}...")
```

---

## Final Setup: Tell Claude About Everything

Add this to your `CLAUDE.md`:

```markdown
## Memory System

You have a multi-layer memory system. Use it proactively.

### Automatic (happens without you doing anything)
- SessionStart hook loads context from last 48h via context_loader.py
- MEMORY.md is always in your system prompt
- Time is verified at session start

### Search Past Conversations
Use episodic-memory search before starting any task to check for
past decisions and solutions.

### Knowledge Graph
Query entity relationships:
```bash
python3 scripts/query_knowledge_graph.py --stats
python3 scripts/query_knowledge_graph.py --related-to "entity"
python3 scripts/query_knowledge_graph.py --find-path "A" "B"
python3 scripts/query_knowledge_graph.py --search "keyword"
```

### Session Handoffs
When stopping mid-task, create a handoff:
Write a markdown file to .planning/handoffs/YYYY-MM-DD-description.md
Include: what you were doing, progress, next steps, key files.

### Building the Knowledge Graph
When you learn about new entities or relationships, add them:
```python
from scripts.knowledge_graph import KnowledgeGraph
kg = KnowledgeGraph()
kg.add_entity("entity_id", "type", key="value")
kg.add_relationship("source", "rel_type", "target", context="why")
```
```

---

## Verification Checklist

```bash
# Phase 1
python3 ~/your-project/scripts/context_loader.py --hours 48  # Should show time
ls ~/your-project/.planning/working-memory/                    # Dir exists

# Phase 2
python3 ~/your-project/scripts/seed_graph.py                  # Seeds graph
python3 ~/your-project/scripts/query_knowledge_graph.py --stats # Shows stats

# Phase 3
python3 ~/your-project/scripts/hybrid_search.py "test query"   # Returns results
python3 ~/your-project/scripts/rlm_graph.py "test query"       # Returns results
```
