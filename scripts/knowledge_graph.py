#!/usr/bin/env python3
"""
Knowledge Graph - Entity/Relationship Tracking (Layer 6)

Converts implicit relationships into a queryable graph.
Stores as JSON-serialized NetworkX MultiDiGraph.

Dependencies: pip install networkx

Usage:
    from knowledge_graph import KnowledgeGraph
    kg = KnowledgeGraph()
    kg.add_entity("Alice", "person", role="developer")
    kg.add_relationship("Alice", "develops", "MyApp", context="lead dev")

Configure:
    Set CORTEX_WORKSPACE to your project root (default: ~/cortex)
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import networkx as nx

# ── Config ──────────────────────────────────────────────────────────────────

WORKSPACE = Path(os.environ.get("CORTEX_WORKSPACE", str(Path.home() / "cortex")))
GRAPH_FILE = Path(
    os.environ.get(
        "CORTEX_GRAPH_FILE",
        str(WORKSPACE / ".planning" / "knowledge-graph.json"),
    )
)


class KnowledgeGraph:
    """Entity-relationship graph for memory connections."""

    def __init__(self, graph_file: Optional[Path] = None):
        self.graph_file = graph_file or GRAPH_FILE
        self.graph = nx.MultiDiGraph()
        self.load()

    def load(self):
        """Load graph from JSON file."""
        if self.graph_file.exists():
            try:
                with open(self.graph_file) as f:
                    data = json.load(f)
                for node in data.get("nodes", []):
                    self.graph.add_node(node["id"], **node.get("attributes", {}))
                for edge in data.get("edges", []):
                    self.graph.add_edge(
                        edge["source"],
                        edge["target"],
                        key=edge.get("key"),
                        rel_type=edge.get(
                            "rel_type", edge.get("relation", "related")
                        ),
                        **edge.get("attributes", {}),
                    )
            except Exception as e:
                print(f"Error loading knowledge graph: {e}")

    def save(self):
        """Save graph to JSON file."""
        self.graph_file.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "nodes": [
                {"id": node, "attributes": dict(attrs)}
                for node, attrs in self.graph.nodes(data=True)
            ],
            "edges": [
                {
                    "source": u,
                    "target": v,
                    "key": k,
                    "rel_type": d.get("rel_type"),
                    "attributes": {
                        key: val for key, val in d.items() if key != "rel_type"
                    },
                }
                for u, v, k, d in self.graph.edges(keys=True, data=True)
            ],
            "updated_at": datetime.now().isoformat(),
        }
        with open(self.graph_file, "w") as f:
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

    def add_relationship(
        self,
        source: str,
        rel_type: str,
        target: str,
        context: Optional[str] = None,
        strength: float = 1.0,
        **metadata,
    ):
        """Add directed relationship. Auto-creates entities if missing."""
        if not self.entity_exists(source):
            self.add_entity(source, "unknown")
        if not self.entity_exists(target):
            self.add_entity(target, "unknown")
        self.graph.add_edge(
            source,
            target,
            rel_type=rel_type,
            context=context,
            strength=strength,
            created_at=datetime.now().isoformat(),
            **metadata,
        )
        self.save()

    def get_relationships(
        self,
        entity: str,
        rel_type: Optional[str] = None,
        direction: str = "out",
    ) -> List[Dict]:
        """Get relationships. direction: 'out', 'in', or 'both'."""
        relationships = []
        if direction in ("out", "both"):
            for _, target, data in self.graph.edges(entity, data=True):
                if rel_type is None or data.get("rel_type") == rel_type:
                    relationships.append(
                        {
                            "source": entity,
                            "target": target,
                            "type": data.get("rel_type"),
                            "direction": "outgoing",
                            **data,
                        }
                    )
        if direction in ("in", "both"):
            for source, _, data in self.graph.in_edges(entity, data=True):
                if rel_type is None or data.get("rel_type") == rel_type:
                    relationships.append(
                        {
                            "source": source,
                            "target": entity,
                            "type": data.get("rel_type"),
                            "direction": "incoming",
                            **data,
                        }
                    )
        return relationships

    def find_path(
        self, source: str, target: str, max_hops: int = 3
    ) -> Optional[List]:
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

    def get_subgraph(self, entities: List[str], hops: int = 1) -> "KnowledgeGraph":
        """Get subgraph containing entities + neighbors."""
        nodes = set(entities)
        for entity in entities:
            if entity in self.graph:
                nodes.update(self.get_neighbors(entity, hops=hops))
        kg = KnowledgeGraph.__new__(KnowledgeGraph)
        kg.graph = self.graph.subgraph(nodes).copy()
        kg.graph_file = self.graph_file
        return kg

    def stats(self) -> Dict:
        """Graph statistics."""
        from collections import Counter

        node_types = Counter(
            d.get("type", "unknown") for _, d in self.graph.nodes(data=True)
        )
        return {
            "total_nodes": self.graph.number_of_nodes(),
            "total_edges": self.graph.number_of_edges(),
            "node_types": dict(node_types),
        }
