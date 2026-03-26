#!/usr/bin/env python3
"""
Knowledge Graph Layer - Explicit Relationship Tracking

Converts implicit relationships buried in prose into first-class entities.
Persists to SQLite (~/clawd/data/cortex-knowledge-graph.db) with NetworkX
MultiDiGraph for fast in-memory traversal.

Examples:
    from knowledge_graph import KnowledgeGraph

    kg = KnowledgeGraph()
    kg.add_relationship("Cameron", "knows", "Preston",
                       context="capital raiser, day job conflict")
    kg.add_relationship("Magnum Opus", "blocked_by", "NAV Fund Services",
                       context="IB read-only access needed")

    # Query
    blocked = kg.get_relationships("Magnum Opus", rel_type="blocked_by")
"""

import json
import logging
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import networkx as nx

# ── Config ──────────────────────────────────────────────────────────────────

DATA_DIR = Path.home() / "clawd" / "data"
DB_PATH = DATA_DIR / "cortex-knowledge-graph.db"

# ── Logging ─────────────────────────────────────────────────────────────────

logger = logging.getLogger("knowledge_graph")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    ))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# ── Schema Definitions ──────────────────────────────────────────────────────

ALLOWED_ENTITY_TYPES: Set[str] = {
    "person", "company", "system", "concept", "project",
    "tool", "location", "ticker", "unknown",
}

ALLOWED_RELATIONSHIP_TYPES: Set[str] = {
    "knows", "uses", "owns", "blocked_by", "competes_with",
    "works_on", "depends_on", "frustrated_with", "implements",
    "manages", "created", "part_of", "co_mentioned",
    "messaged", "discussed", "traded_with", "member_of", "works_at",
}


def _normalize_id(name: str) -> str:
    """Normalize entity ID: lowercase, strip whitespace, spaces to underscores."""
    return re.sub(r'\s+', '_', name.strip().lower())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class KnowledgeGraph:
    """Explicit relationship graph with SQLite persistence and NetworkX traversal."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.graph = nx.MultiDiGraph()
        self._init_db()
        self._load_from_db()

    # ═══════════════════════════════════════════════════════════════════════
    # DATABASE LAYER
    # ═══════════════════════════════════════════════════════════════════════

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        """Create tables if they don't exist."""
        conn = self._get_conn()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    entity_type TEXT NOT NULL DEFAULT 'unknown',
                    attributes TEXT NOT NULL DEFAULT '{}',
                    display_name TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS relationships (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    target TEXT NOT NULL,
                    rel_type TEXT NOT NULL,
                    context TEXT,
                    strength REAL NOT NULL DEFAULT 1.0,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    last_accessed_at TEXT NOT NULL,
                    FOREIGN KEY (source) REFERENCES entities(id),
                    FOREIGN KEY (target) REFERENCES entities(id)
                );

                CREATE TABLE IF NOT EXISTS aliases (
                    alias TEXT PRIMARY KEY,
                    canonical_id TEXT NOT NULL,
                    FOREIGN KEY (canonical_id) REFERENCES entities(id)
                );

                CREATE INDEX IF NOT EXISTS idx_rel_source ON relationships(source);
                CREATE INDEX IF NOT EXISTS idx_rel_target ON relationships(target);
                CREATE INDEX IF NOT EXISTS idx_rel_type ON relationships(rel_type);
                CREATE INDEX IF NOT EXISTS idx_rel_last_accessed ON relationships(last_accessed_at);
                CREATE INDEX IF NOT EXISTS idx_entity_type ON entities(entity_type);
                CREATE INDEX IF NOT EXISTS idx_alias_canonical ON aliases(canonical_id);
            """)
            conn.commit()
        finally:
            conn.close()

    def _load_from_db(self):
        """Load full graph from SQLite into NetworkX."""
        conn = self._get_conn()
        try:
            # Load entities
            for row in conn.execute("SELECT * FROM entities"):
                attrs = json.loads(row["attributes"])
                attrs["type"] = row["entity_type"]
                if row["display_name"]:
                    attrs["display_name"] = row["display_name"]
                self.graph.add_node(row["id"], **attrs)

            # Load relationships
            for row in conn.execute("SELECT * FROM relationships"):
                metadata = json.loads(row["metadata"])
                self.graph.add_edge(
                    row["source"], row["target"],
                    key=row["id"],
                    rel_type=row["rel_type"],
                    context=row["context"],
                    strength=row["strength"],
                    created_at=row["created_at"],
                    last_accessed_at=row["last_accessed_at"],
                    db_id=row["id"],
                    **metadata,
                )

            node_count = self.graph.number_of_nodes()
            edge_count = self.graph.number_of_edges()
            if node_count > 0:
                logger.info(
                    "Loaded knowledge graph: %d entities, %d relationships",
                    node_count, edge_count,
                )
        finally:
            conn.close()

    def _db_save_entity(self, entity_id: str, entity_type: str,
                        display_name: Optional[str], attributes: Dict):
        """Upsert a single entity to SQLite."""
        now = _now_iso()
        attrs_json = json.dumps(attributes, default=str)
        conn = self._get_conn()
        try:
            conn.execute("""
                INSERT INTO entities (id, entity_type, attributes, display_name, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    entity_type = excluded.entity_type,
                    attributes = excluded.attributes,
                    display_name = excluded.display_name,
                    updated_at = excluded.updated_at
            """, (entity_id, entity_type, attrs_json, display_name, now, now))
            conn.commit()
        finally:
            conn.close()

    def _db_save_relationship(self, source: str, target: str, rel_type: str,
                              context: Optional[str], strength: float,
                              metadata: Dict, created_at: str) -> int:
        """Insert a relationship into SQLite, return its ID."""
        now = _now_iso()
        meta_json = json.dumps(metadata, default=str)
        conn = self._get_conn()
        try:
            cursor = conn.execute("""
                INSERT INTO relationships
                    (source, target, rel_type, context, strength, metadata, created_at, last_accessed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (source, target, rel_type, context, strength, meta_json, created_at, now))
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def _db_touch_relationship(self, db_id: int):
        """Update last_accessed_at for a relationship."""
        conn = self._get_conn()
        try:
            conn.execute(
                "UPDATE relationships SET last_accessed_at = ? WHERE id = ?",
                (_now_iso(), db_id),
            )
            conn.commit()
        finally:
            conn.close()

    # ═══════════════════════════════════════════════════════════════════════
    # ENTITY RESOLUTION / CANONICALIZATION
    # ═══════════════════════════════════════════════════════════════════════

    def resolve_entity(self, name: str) -> Optional[str]:
        """Resolve a name to its canonical entity ID.

        Checks: exact match -> alias table -> fuzzy match (>0.8 similarity).
        Returns canonical ID or None.
        """
        normalized = _normalize_id(name)

        # Exact match
        if normalized in self.graph:
            return normalized

        # Alias lookup
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT canonical_id FROM aliases WHERE alias = ?",
                (normalized,),
            ).fetchone()
            if row:
                return row["canonical_id"]
        finally:
            conn.close()

        # Fuzzy match against all entity IDs
        best_match = None
        best_score = 0.0
        for node_id in self.graph.nodes():
            score = SequenceMatcher(None, normalized, node_id).ratio()
            if score > best_score:
                best_score = score
                best_match = node_id

        if best_score > 0.8 and best_match is not None:
            logger.info(
                "Fuzzy resolved '%s' -> '%s' (score=%.2f)",
                name, best_match, best_score,
            )
            return best_match

        return None

    def add_alias(self, alias: str, canonical_id: str):
        """Register an alias for an entity."""
        normalized_alias = _normalize_id(alias)
        normalized_canonical = _normalize_id(canonical_id)

        if normalized_canonical not in self.graph:
            logger.warning(
                "Cannot add alias '%s' -> '%s': canonical entity does not exist",
                alias, canonical_id,
            )
            return

        conn = self._get_conn()
        try:
            conn.execute("""
                INSERT INTO aliases (alias, canonical_id)
                VALUES (?, ?)
                ON CONFLICT(alias) DO UPDATE SET canonical_id = excluded.canonical_id
            """, (normalized_alias, normalized_canonical))
            conn.commit()
            logger.info("Alias added: '%s' -> '%s'", normalized_alias, normalized_canonical)
        finally:
            conn.close()

    def get_aliases(self, entity_id: str) -> List[str]:
        """Get all aliases for an entity."""
        normalized = _normalize_id(entity_id)
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT alias FROM aliases WHERE canonical_id = ?",
                (normalized,),
            ).fetchall()
            return [row["alias"] for row in rows]
        finally:
            conn.close()

    # ═══════════════════════════════════════════════════════════════════════
    # SCHEMA VALIDATION
    # ═══════════════════════════════════════════════════════════════════════

    def _validate_entity_type(self, entity_type: str) -> str:
        """Validate entity type, warn on unknown."""
        normalized = entity_type.lower().strip()
        if normalized not in ALLOWED_ENTITY_TYPES:
            logger.warning(
                "Unknown entity type '%s'. Allowed: %s",
                entity_type, ", ".join(sorted(ALLOWED_ENTITY_TYPES)),
            )
        return normalized

    def _validate_rel_type(self, rel_type: str) -> str:
        """Validate relationship type, warn on unknown."""
        normalized = rel_type.lower().strip()
        if normalized not in ALLOWED_RELATIONSHIP_TYPES:
            logger.warning(
                "Unknown relationship type '%s'. Allowed: %s",
                rel_type, ", ".join(sorted(ALLOWED_RELATIONSHIP_TYPES)),
            )
        return normalized

    @staticmethod
    def get_schema() -> Dict[str, List[str]]:
        """Return allowed entity and relationship types."""
        return {
            "entity_types": sorted(ALLOWED_ENTITY_TYPES),
            "relationship_types": sorted(ALLOWED_RELATIONSHIP_TYPES),
        }

    # ═══════════════════════════════════════════════════════════════════════
    # NODE MANAGEMENT
    # ═══════════════════════════════════════════════════════════════════════

    def add_entity(self, entity_id: str, entity_type: str, **attributes):
        """Add or update an entity node."""
        normalized_id = _normalize_id(entity_id)
        validated_type = self._validate_entity_type(entity_type)

        # Store original name as display_name if it differs
        display_name = entity_id.strip() if normalized_id != entity_id.strip() else None

        # Update in-memory graph
        self.graph.add_node(normalized_id, type=validated_type, **attributes)
        if display_name:
            self.graph.nodes[normalized_id]["display_name"] = display_name

        # Persist to SQLite
        self._db_save_entity(normalized_id, validated_type, display_name, attributes)

        # Auto-register alias from original name
        if display_name:
            self.add_alias(entity_id, normalized_id)

    def get_entity(self, entity_id: str) -> Optional[Dict]:
        """Get entity attributes."""
        resolved = self.resolve_entity(entity_id)
        if resolved and resolved in self.graph:
            return dict(self.graph.nodes[resolved])
        return None

    def entity_exists(self, entity_id: str) -> bool:
        """Check if entity exists (checks aliases too)."""
        return self.resolve_entity(entity_id) is not None

    # ═══════════════════════════════════════════════════════════════════════
    # RELATIONSHIP MANAGEMENT
    # ═══════════════════════════════════════════════════════════════════════

    def add_relationship(self, source: str, rel_type: str, target: str,
                         context: Optional[str] = None,
                         strength: float = 1.0,
                         **metadata):
        """Add directed relationship between entities."""
        source_id = _normalize_id(source)
        target_id = _normalize_id(target)
        validated_type = self._validate_rel_type(rel_type)

        # Ensure entities exist
        if not self.graph.has_node(source_id):
            self.add_entity(source, "unknown")
        if not self.graph.has_node(target_id):
            self.add_entity(target, "unknown")

        now = _now_iso()

        # Persist to SQLite first to get the ID
        db_id = self._db_save_relationship(
            source_id, target_id, validated_type,
            context, strength, metadata, now,
        )

        # Add to in-memory graph
        self.graph.add_edge(
            source_id, target_id,
            key=db_id,
            rel_type=validated_type,
            context=context,
            strength=strength,
            created_at=now,
            last_accessed_at=now,
            db_id=db_id,
            **metadata,
        )

    def get_relationships(self, entity: str,
                          rel_type: Optional[str] = None,
                          direction: str = "out") -> List[Dict]:
        """Get relationships for an entity.

        Args:
            entity: Entity ID (resolved via aliases)
            rel_type: Filter by relationship type (optional)
            direction: 'out' (outgoing), 'in' (incoming), 'both'
        """
        resolved = self.resolve_entity(entity)
        if not resolved or resolved not in self.graph:
            return []

        relationships = []

        if direction in ("out", "both"):
            for _, target, data in self.graph.edges(resolved, data=True):
                if rel_type is None or data.get("rel_type") == rel_type:
                    # Touch access timestamp
                    db_id = data.get("db_id")
                    if db_id:
                        self._db_touch_relationship(db_id)
                    relationships.append({
                        "source": resolved,
                        "target": target,
                        "type": data.get("rel_type"),
                        "direction": "outgoing",
                        **data,
                    })

        if direction in ("in", "both"):
            for source, _, data in self.graph.in_edges(resolved, data=True):
                if rel_type is None or data.get("rel_type") == rel_type:
                    db_id = data.get("db_id")
                    if db_id:
                        self._db_touch_relationship(db_id)
                    relationships.append({
                        "source": source,
                        "target": resolved,
                        "type": data.get("rel_type"),
                        "direction": "incoming",
                        **data,
                    })

        return relationships

    def find_path(self, source: str, target: str,
                  max_hops: int = 3) -> Optional[List]:
        """Find shortest path between entities."""
        source_id = self.resolve_entity(source)
        target_id = self.resolve_entity(target)
        if not source_id or not target_id:
            return None
        try:
            path = nx.shortest_path(self.graph, source_id, target_id)
            if len(path) <= max_hops + 1:
                return path
        except nx.NetworkXNoPath:
            pass
        return None

    def get_neighbors(self, entity: str, hops: int = 1) -> List[str]:
        """Get neighboring entities within N hops."""
        resolved = self.resolve_entity(entity)
        if not resolved or resolved not in self.graph:
            return []

        neighbors = set()
        ego = nx.ego_graph(self.graph, resolved, radius=hops)
        for node in ego.nodes():
            if node != resolved:
                neighbors.add(node)

        return list(neighbors)

    # ═══════════════════════════════════════════════════════════════════════
    # TTL / DECAY
    # ═══════════════════════════════════════════════════════════════════════

    def decay_strength(self, max_age_days: int = 90):
        """Reduce strength of old, unaccessed relationships.

        Relationships not accessed within max_age_days have their strength
        reduced proportionally. Fully decayed after 2x max_age_days.
        """
        now = datetime.now(timezone.utc)
        conn = self._get_conn()
        updated = 0
        try:
            rows = conn.execute(
                "SELECT id, strength, last_accessed_at FROM relationships"
            ).fetchall()

            for row in rows:
                try:
                    last_accessed = datetime.fromisoformat(row["last_accessed_at"])
                    if last_accessed.tzinfo is None:
                        last_accessed = last_accessed.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    continue

                age_days = (now - last_accessed).days
                if age_days <= max_age_days:
                    continue

                # Linear decay: strength goes to 0 over another max_age_days period
                overage = age_days - max_age_days
                decay_factor = max(0.0, 1.0 - (overage / max_age_days))
                new_strength = row["strength"] * decay_factor

                conn.execute(
                    "UPDATE relationships SET strength = ? WHERE id = ?",
                    (new_strength, row["id"]),
                )

                # Update in-memory graph
                for u, v, k, data in self.graph.edges(keys=True, data=True):
                    if data.get("db_id") == row["id"]:
                        self.graph[u][v][k]["strength"] = new_strength
                        break

                updated += 1

            conn.commit()
            logger.info("Decayed %d relationships (max_age=%d days)", updated, max_age_days)
        finally:
            conn.close()

    def prune_weak(self, min_strength: float = 0.1) -> int:
        """Remove relationships with strength below threshold."""
        conn = self._get_conn()
        pruned = 0
        try:
            rows = conn.execute(
                "SELECT id, source, target FROM relationships WHERE strength < ?",
                (min_strength,),
            ).fetchall()

            for row in rows:
                conn.execute("DELETE FROM relationships WHERE id = ?", (row["id"],))

                # Remove from in-memory graph
                edges_to_remove = []
                for u, v, k, data in self.graph.edges(keys=True, data=True):
                    if data.get("db_id") == row["id"]:
                        edges_to_remove.append((u, v, k))
                        break
                for u, v, k in edges_to_remove:
                    self.graph.remove_edge(u, v, key=k)

                pruned += 1

            conn.commit()
            logger.info("Pruned %d weak relationships (min_strength=%.2f)", pruned, min_strength)
        finally:
            conn.close()

        return pruned

    # ═══════════════════════════════════════════════════════════════════════
    # SPECIALIZED QUERIES
    # ═══════════════════════════════════════════════════════════════════════

    def get_blockers(self, entity: str) -> List[Dict]:
        """Get what's blocking an entity."""
        return self.get_relationships(entity, rel_type="blocked_by", direction="out")

    def get_blocking(self, entity: str) -> List[Dict]:
        """Get what this entity is blocking."""
        return self.get_relationships(entity, rel_type="blocked_by", direction="in")

    def get_contacts(self, entity: str) -> List[Dict]:
        """Get people/entities connected via 'knows' relationship."""
        return self.get_relationships(entity, rel_type="knows", direction="both")

    def get_frustrated_with(self, entity: str) -> List[Dict]:
        """Get frustration relationships."""
        return self.get_relationships(entity, rel_type="frustrated_with", direction="out")

    def get_competitors(self, entity: str) -> List[Dict]:
        """Get competitive relationships."""
        return self.get_relationships(entity, rel_type="competes_with", direction="both")

    # ═══════════════════════════════════════════════════════════════════════
    # RICHER QUERY METHODS
    # ═══════════════════════════════════════════════════════════════════════

    def query_by_type(self, entity_type: str) -> List[Dict]:
        """Get all entities of a given type."""
        normalized = entity_type.lower().strip()
        results = []
        for node_id, attrs in self.graph.nodes(data=True):
            if attrs.get("type") == normalized:
                results.append({"id": node_id, **attrs})
        return results

    def query_by_relationship(self, rel_type: str,
                              direction: str = "out") -> List[Dict]:
        """Find all relationships of a given type.

        Args:
            rel_type: Relationship type to filter by
            direction: 'out' for outgoing edges, 'in' for incoming, 'both'
        """
        normalized = rel_type.lower().strip()
        results = []
        seen_ids = set()

        for u, v, data in self.graph.edges(data=True):
            if data.get("rel_type") != normalized:
                continue
            db_id = data.get("db_id")
            if db_id in seen_ids:
                continue
            seen_ids.add(db_id)

            if direction == "in":
                results.append({
                    "source": u, "target": v,
                    "type": normalized, "direction": "incoming",
                    **data,
                })
            elif direction == "out":
                results.append({
                    "source": u, "target": v,
                    "type": normalized, "direction": "outgoing",
                    **data,
                })
            else:  # both
                results.append({
                    "source": u, "target": v,
                    "type": normalized,
                    **data,
                })

        return results

    def get_connected_components(self) -> List[List[str]]:
        """Find disconnected subgraphs (on undirected projection)."""
        undirected = self.graph.to_undirected()
        components = list(nx.connected_components(undirected))
        # Sort by size descending
        return sorted([sorted(c) for c in components], key=len, reverse=True)

    def get_most_connected(self, limit: int = 10) -> List[Tuple[str, int]]:
        """Get entities with most relationships (degree centrality)."""
        degree_map = dict(self.graph.degree())
        sorted_nodes = sorted(degree_map.items(), key=lambda x: x[1], reverse=True)
        return sorted_nodes[:limit]

    def search_entities(self, query: str) -> List[Dict]:
        """Fuzzy search across entity IDs, display names, and attributes."""
        query_lower = query.lower().strip()
        results = []

        for node_id, attrs in self.graph.nodes(data=True):
            score = 0.0

            # Check entity ID
            if query_lower in node_id:
                score = max(score, 0.9 if query_lower == node_id else 0.7)

            # Check display name
            display = attrs.get("display_name", "")
            if display and query_lower in display.lower():
                score = max(score, 0.8)

            # Check attributes as string
            attrs_str = json.dumps(attrs, default=str).lower()
            if query_lower in attrs_str:
                score = max(score, 0.5)

            # Fuzzy on ID
            if score == 0.0:
                ratio = SequenceMatcher(None, query_lower, node_id).ratio()
                if ratio > 0.6:
                    score = ratio * 0.6  # Scale fuzzy scores lower

            if score > 0.0:
                results.append({"id": node_id, "score": round(score, 3), **attrs})

        results.sort(key=lambda x: x["score"], reverse=True)
        return results

    # ═══════════════════════════════════════════════════════════════════════
    # PATTERN ANALYSIS
    # ═══════════════════════════════════════════════════════════════════════

    def find_common_relationships(self, entity1: str, entity2: str) -> List[str]:
        """Find entities both are connected to."""
        r1 = self.resolve_entity(entity1)
        r2 = self.resolve_entity(entity2)
        if not r1 or r1 not in self.graph or not r2 or r2 not in self.graph:
            return []

        neighbors1 = set(self.get_neighbors(r1, hops=1))
        neighbors2 = set(self.get_neighbors(r2, hops=1))
        return list(neighbors1.intersection(neighbors2))

    def get_relationship_summary(self, entity: str) -> Dict:
        """Get summary of all relationships for entity."""
        resolved = self.resolve_entity(entity)
        if not resolved or resolved not in self.graph:
            return {}

        outgoing = self.get_relationships(resolved, direction="out")
        incoming = self.get_relationships(resolved, direction="in")

        out_types: Dict[str, int] = {}
        in_types: Dict[str, int] = {}

        for rel in outgoing:
            rt = rel.get("type", "unknown")
            out_types[rt] = out_types.get(rt, 0) + 1

        for rel in incoming:
            rt = rel.get("type", "unknown")
            in_types[rt] = in_types.get(rt, 0) + 1

        return {
            "entity": resolved,
            "total_outgoing": len(outgoing),
            "total_incoming": len(incoming),
            "outgoing_by_type": out_types,
            "incoming_by_type": in_types,
            "neighbors": len(self.get_neighbors(resolved, hops=1)),
        }

    # ═══════════════════════════════════════════════════════════════════════
    # GRAPH STATISTICS
    # ═══════════════════════════════════════════════════════════════════════

    def get_stats(self) -> Dict[str, Any]:
        """Return graph statistics."""
        node_count = self.graph.number_of_nodes()
        edge_count = self.graph.number_of_edges()

        components = self.get_connected_components()

        degrees = [d for _, d in self.graph.degree()]
        avg_degree = sum(degrees) / len(degrees) if degrees else 0.0

        most_connected = self.get_most_connected(limit=5)

        # Count by entity type
        type_counts: Dict[str, int] = {}
        for _, attrs in self.graph.nodes(data=True):
            t = attrs.get("type", "unknown")
            type_counts[t] = type_counts.get(t, 0) + 1

        # Count by relationship type
        rel_counts: Dict[str, int] = {}
        for _, _, data in self.graph.edges(data=True):
            rt = data.get("rel_type", "unknown")
            rel_counts[rt] = rel_counts.get(rt, 0) + 1

        return {
            "node_count": node_count,
            "edge_count": edge_count,
            "connected_components": len(components),
            "avg_degree": round(avg_degree, 2),
            "most_connected_entities": [
                {"id": nid, "degree": deg} for nid, deg in most_connected
            ],
            "entities_by_type": type_counts,
            "relationships_by_type": rel_counts,
        }

    # ═══════════════════════════════════════════════════════════════════════
    # EXPORT / VISUALIZATION
    # ═══════════════════════════════════════════════════════════════════════

    def export_to_dot(self, filepath: str):
        """Export graph to DOT format for visualization."""
        nx.drawing.nx_pydot.write_dot(self.graph, filepath)

    def get_subgraph(self, entities: List[str],
                     hops: int = 1) -> 'KnowledgeGraph':
        """Get subgraph containing specified entities and their neighbors."""
        resolved = []
        for e in entities:
            r = self.resolve_entity(e)
            if r:
                resolved.append(r)

        nodes = set(resolved)
        for entity in resolved:
            if entity in self.graph:
                nodes.update(self.get_neighbors(entity, hops=hops))

        subgraph_data = self.graph.subgraph(nodes)

        # Create new KnowledgeGraph with subgraph (in-memory only)
        kg = KnowledgeGraph.__new__(KnowledgeGraph)
        kg.db_path = self.db_path
        kg.graph = subgraph_data.copy()
        return kg

    # ═══════════════════════════════════════════════════════════════════════
    # MIGRATION: Import from legacy JSON
    # ═══════════════════════════════════════════════════════════════════════

    def import_from_json(self, json_path: Path):
        """Import entities and relationships from legacy JSON format.

        Use this once to migrate from the old .planning/knowledge-graph.json.
        """
        if not json_path.exists():
            logger.warning("JSON file not found: %s", json_path)
            return

        with open(json_path) as f:
            data = json.load(f)

        imported_nodes = 0
        imported_edges = 0

        for node in data.get("nodes", []):
            attrs = dict(node.get("attributes", {}))
            entity_type = attrs.pop("type", "unknown")
            entity_id = node["id"]
            self.add_entity(entity_id, entity_type, **attrs)
            imported_nodes += 1

        for edge in data.get("edges", []):
            edge_attrs = dict(edge.get("attributes", {}))
            context = edge_attrs.pop("context", None)
            strength = edge_attrs.pop("strength", 1.0)
            created_at = edge_attrs.pop("created_at", None)

            source = edge["source"]
            target = edge["target"]
            rel_type = edge.get("rel_type", "unknown")

            self.add_relationship(
                source, rel_type, target,
                context=context,
                strength=strength,
                **edge_attrs,
            )
            imported_edges += 1

        logger.info(
            "Imported from JSON: %d entities, %d relationships",
            imported_nodes, imported_edges,
        )


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

def _cli_stats():
    """Show graph statistics."""
    kg = KnowledgeGraph()
    stats = kg.get_stats()

    print(f"\n{'='*50}")
    print("  Knowledge Graph Statistics")
    print(f"{'='*50}")
    print(f"  Entities:              {stats['node_count']}")
    print(f"  Relationships:         {stats['edge_count']}")
    print(f"  Connected Components:  {stats['connected_components']}")
    print(f"  Average Degree:        {stats['avg_degree']}")
    print()

    print("  Entities by Type:")
    for t, count in sorted(stats["entities_by_type"].items(), key=lambda x: -x[1]):
        print(f"    {t:20s} {count}")
    print()

    print("  Relationships by Type:")
    for t, count in sorted(stats["relationships_by_type"].items(), key=lambda x: -x[1]):
        print(f"    {t:20s} {count}")
    print()

    print("  Most Connected:")
    for item in stats["most_connected_entities"]:
        print(f"    {item['id']:30s} degree={item['degree']}")

    print(f"{'='*50}\n")


def _cli_search(query: str):
    """Search entities."""
    kg = KnowledgeGraph()
    results = kg.search_entities(query)
    if not results:
        print(f"No entities matching '{query}'")
        return
    print(f"\nSearch results for '{query}':")
    for r in results[:10]:
        score = r.pop("score", 0)
        eid = r.pop("id")
        etype = r.get("type", "unknown")
        print(f"  [{score:.2f}] {eid} ({etype})")


def _cli_import(json_path: str):
    """Import from legacy JSON."""
    kg = KnowledgeGraph()
    kg.import_from_json(Path(json_path))
    stats = kg.get_stats()
    print(f"Import complete: {stats['node_count']} entities, {stats['edge_count']} relationships")


def _cli_schema():
    """Show allowed types."""
    schema = KnowledgeGraph.get_schema()
    print("\nAllowed Entity Types:")
    for t in schema["entity_types"]:
        print(f"  - {t}")
    print("\nAllowed Relationship Types:")
    for t in schema["relationship_types"]:
        print(f"  - {t}")


def main():
    """CLI entry point."""
    if len(sys.argv) < 2:
        print("Usage: knowledge_graph.py <command> [args]")
        print()
        print("Commands:")
        print("  stats              Show graph statistics")
        print("  search <query>     Search entities")
        print("  schema             Show allowed types")
        print("  import <json>      Import from legacy JSON file")
        print("  decay [days]       Run decay on old relationships (default: 90)")
        print("  prune [strength]   Remove weak relationships (default: 0.1)")
        return

    cmd = sys.argv[1].lower()

    if cmd == "stats":
        _cli_stats()
    elif cmd == "search":
        if len(sys.argv) < 3:
            print("Usage: knowledge_graph.py search <query>")
            return
        _cli_search(" ".join(sys.argv[2:]))
    elif cmd == "schema":
        _cli_schema()
    elif cmd == "import":
        if len(sys.argv) < 3:
            print("Usage: knowledge_graph.py import <path-to-json>")
            return
        _cli_import(sys.argv[2])
    elif cmd == "decay":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 90
        kg = KnowledgeGraph()
        kg.decay_strength(max_age_days=days)
    elif cmd == "prune":
        threshold = float(sys.argv[2]) if len(sys.argv) > 2 else 0.1
        kg = KnowledgeGraph()
        pruned = kg.prune_weak(min_strength=threshold)
        print(f"Pruned {pruned} relationships")
    else:
        print(f"Unknown command: {cmd}")


if __name__ == "__main__":
    main()
