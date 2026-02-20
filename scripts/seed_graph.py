#!/usr/bin/env python3
"""
Seed the knowledge graph with initial entities and relationships.

Edit this file to add your own entities and relationships, then run:
    python3 seed_graph.py

Configure:
    Set CORTEX_WORKSPACE to your project root (default: ~/cortex)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from knowledge_graph import KnowledgeGraph

kg = KnowledgeGraph()

# === EXAMPLE ENTITIES ===
# Uncomment and customize these for your project

# People
# kg.add_entity("you", "person", role="founder")
# kg.add_entity("teammate", "person", role="developer")

# Projects
# kg.add_entity("my-project", "project", domain="software")

# Systems
# kg.add_entity("database", "system", tech="postgresql")
# kg.add_entity("api-server", "system", tech="fastapi")

# === EXAMPLE RELATIONSHIPS ===
# kg.add_relationship("you", "develops", "my-project", context="Lead developer")
# kg.add_relationship("my-project", "uses", "database", context="Primary data store")
# kg.add_relationship("api-server", "reads_from", "database", context="REST API layer")

# === VERIFY ===
stats = kg.stats()
print(f"Graph seeded: {stats['total_nodes']} nodes, {stats['total_edges']} edges")
print("Run: python3 query_knowledge_graph.py --stats")
