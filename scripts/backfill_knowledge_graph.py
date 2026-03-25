#!/usr/bin/env python3
"""Backfill knowledge graph with improved relationship extraction.

Re-processes all observations that have summaries through the improved
EntityExtractor, creating entities and relationships (including the new
directed types: messaged, discussed, traded_with, member_of, works_at)
and ticker entities.

Usage:
    # Full backfill
    python backfill_knowledge_graph.py

    # Dry run — preview without modifying the graph
    python backfill_knowledge_graph.py --dry-run

    # Process only first N observations (for testing)
    python backfill_knowledge_graph.py --limit 50

    # Combine flags
    python backfill_knowledge_graph.py --dry-run --limit 100
"""

import argparse
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

# Ensure sibling modules are importable
sys.path.insert(0, str(Path(__file__).parent))

from memory_worker import EntityExtractor


def backfill(dry_run: bool = False, limit: int = 0):
    """Run the backfill process.

    Args:
        dry_run: If True, extract entities but do not write to the knowledge graph.
        limit: If > 0, process only this many observations.
    """
    obs_db_path = Path.home() / "clawd" / "data" / "cortex-observations.db"
    kg_db_path = Path.home() / "clawd" / "data" / "cortex-knowledge-graph.db"

    if not obs_db_path.exists():
        print(f"ERROR: Observations DB not found at {obs_db_path}")
        sys.exit(1)

    # Connect to observations DB (read-only)
    obs_db = sqlite3.connect(str(obs_db_path))
    obs_db.row_factory = sqlite3.Row

    # Initialize KnowledgeGraph (writes to kg_db_path)
    from knowledge_graph import KnowledgeGraph

    if dry_run:
        # For dry run, use an in-memory graph so nothing is persisted
        kg = KnowledgeGraph.__new__(KnowledgeGraph)
        import networkx as nx
        kg.db_path = kg_db_path
        kg.graph = nx.MultiDiGraph()
        # Override DB methods to no-op
        kg._db_save_entity = lambda *a, **kw: None
        kg._db_save_relationship = lambda *a, **kw: 0
        kg._db_touch_relationship = lambda *a, **kw: None
        kg._init_db = lambda: None
        kg._load_from_db = lambda: None
        print("[DRY RUN] No changes will be written to the knowledge graph.\n")
    else:
        kg = KnowledgeGraph(db_path=kg_db_path)
        print(f"Knowledge graph loaded: {kg.graph.number_of_nodes()} entities, "
              f"{kg.graph.number_of_edges()} relationships\n")

    # Inject kg into memory_worker module scope so _extract_directed_relationships works
    import memory_worker
    memory_worker.kg = kg

    # Read observations with summaries
    query = "SELECT id, summary, raw_input, raw_output FROM observations WHERE summary IS NOT NULL ORDER BY id"
    if limit > 0:
        query += f" LIMIT {limit}"

    rows = obs_db.execute(query).fetchall()
    total = len(rows)
    print(f"Found {total} observations with summaries to process.\n")

    if total == 0:
        obs_db.close()
        return

    extractor = EntityExtractor()

    # Stats tracking
    stats = {
        "observations_processed": 0,
        "observations_with_entities": 0,
        "total_entities": 0,
        "total_relationships": 0,
        "entities_by_type": defaultdict(int),
        "relationships_by_type": defaultdict(int),
    }

    start_time = time.time()
    report_interval = max(1, total // 20)  # Report progress every ~5%

    for i, row in enumerate(rows):
        obs_id = row["id"]
        summary = row["summary"]
        raw_input = row["raw_input"]
        raw_output = row["raw_output"]

        # Extract entities
        entities = extractor.extract(summary, raw_input, raw_output)
        stats["observations_processed"] += 1

        if not entities:
            if (i + 1) % report_interval == 0:
                elapsed = time.time() - start_time
                print(f"  [{i+1}/{total}] {elapsed:.1f}s elapsed...")
            continue

        stats["observations_with_entities"] += 1
        stats["total_entities"] += len(entities)

        for name, etype in entities:
            stats["entities_by_type"][etype] += 1

        if not dry_run:
            # Add entities to knowledge graph
            for name, etype in entities:
                kg.add_entity(name, etype)

            # Create co_mentioned relationships
            if len(entities) >= 2:
                capped = entities[:10]
                for a_idx in range(len(capped)):
                    for b_idx in range(a_idx + 1, len(capped)):
                        name_a, _ = capped[a_idx]
                        name_b, _ = capped[b_idx]
                        kg.add_relationship(
                            name_a, "co_mentioned", name_b,
                            context=f"obs:{obs_id}",
                            strength=0.5,
                            observation_id=obs_id,
                        )
                        stats["relationships_by_type"]["co_mentioned"] += 1
                        stats["total_relationships"] += 1

            # Extract directed relationships
            combined_text = "\n".join(
                t for t in [summary, raw_input, raw_output] if t
            )
            directed_count = extractor._extract_directed_relationships(
                entities, combined_text, obs_id,
            ) or 0
            stats["total_relationships"] += directed_count
            # We can't easily break down directed_count by type here,
            # so we'll count from the graph delta later

        else:
            # Dry run: still count co_mentioned for stats
            if len(entities) >= 2:
                capped = entities[:10]
                n = len(capped)
                co_mentioned_count = n * (n - 1) // 2
                stats["relationships_by_type"]["co_mentioned"] += co_mentioned_count
                stats["total_relationships"] += co_mentioned_count

            # Dry run: count directed relationships by running extraction
            # against a temporary kg (entities need to exist for add_relationship)
            for name, etype in entities:
                kg.add_entity(name, etype)

            combined_text = "\n".join(
                t for t in [summary, raw_input, raw_output] if t
            )
            before_edges = kg.graph.number_of_edges()
            extractor._extract_directed_relationships(entities, combined_text, obs_id)
            after_edges = kg.graph.number_of_edges()
            directed_count = after_edges - before_edges
            stats["total_relationships"] += directed_count

        # Progress report
        if (i + 1) % report_interval == 0 or (i + 1) == total:
            elapsed = time.time() - start_time
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            print(f"  [{i+1}/{total}] {elapsed:.1f}s elapsed, "
                  f"{rate:.0f} obs/s, "
                  f"{stats['total_entities']} entities, "
                  f"{stats['total_relationships']} relationships")

    elapsed = time.time() - start_time
    obs_db.close()

    # Print final stats
    print(f"\n{'='*60}")
    print(f"  Backfill {'(DRY RUN) ' if dry_run else ''}Complete")
    print(f"{'='*60}")
    print(f"  Time:                    {elapsed:.1f}s")
    print(f"  Observations processed:  {stats['observations_processed']}")
    print(f"  With entities:           {stats['observations_with_entities']}")
    print(f"  Total entities found:    {stats['total_entities']}")
    print(f"  Total relationships:     {stats['total_relationships']}")
    print()

    if stats["entities_by_type"]:
        print("  Entities by type:")
        for etype, count in sorted(stats["entities_by_type"].items(),
                                   key=lambda x: -x[1]):
            print(f"    {etype:20s} {count}")
        print()

    if stats["relationships_by_type"]:
        print("  Relationships by type (co_mentioned only — directed types")
        print("  are counted in total but not individually broken down):")
        for rtype, count in sorted(stats["relationships_by_type"].items(),
                                   key=lambda x: -x[1]):
            print(f"    {rtype:20s} {count}")
        print()

    if not dry_run:
        final_stats = kg.get_stats()
        print(f"  Final graph state:")
        print(f"    Entities:       {final_stats['node_count']}")
        print(f"    Relationships:  {final_stats['edge_count']}")
        print()
        if final_stats.get("relationships_by_type"):
            print("  Graph relationships by type:")
            for rtype, count in sorted(
                final_stats["relationships_by_type"].items(),
                key=lambda x: -x[1],
            ):
                print(f"    {rtype:20s} {count}")
            print()

    print(f"{'='*60}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Backfill knowledge graph with improved relationship extraction."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview extraction without modifying the knowledge graph.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process only the first N observations (0 = all).",
    )
    args = parser.parse_args()
    backfill(dry_run=args.dry_run, limit=args.limit)


if __name__ == "__main__":
    main()
