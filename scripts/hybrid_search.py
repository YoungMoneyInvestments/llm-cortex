#!/usr/bin/env python3
"""
Hybrid Search - Multi-source Search Fusion (Layer 5)

Combines keyword matching across memory files with knowledge graph
relationship expansion. Add vector/embedding search when you have
an embedding model configured.

Configure:
    Set CORTEX_WORKSPACE to your project root (default: ~/cortex)
"""

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Set

sys.path.insert(0, str(Path(__file__).parent))
from knowledge_graph import KnowledgeGraph

WORKSPACE = Path(os.environ.get("CORTEX_WORKSPACE", str(Path.home() / "cortex")))
MEMORY_DIR = WORKSPACE / "memory"


def extract_entities(query: str) -> List[str]:
    """Extract potential entity names from query text."""
    entities = []
    multi = re.findall(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b", query)
    entities.extend(multi)
    words = query.split()
    for i, word in enumerate(words):
        if i > 0 and len(word) > 0 and word[0].isupper():
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
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.stdout:
                for block in result.stdout.split("--"):
                    content = block.strip()
                    if content:
                        results.append(
                            {"content": content[:500], "score": 0.5, "source": "keyword"}
                        )
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
            if rel.get("context"):
                content += f" ({rel['context']})"
            expanded.append(
                {"content": content, "score": rel.get("strength", 0.7), "source": "graph"}
            )
    return expanded


def deduplicate_results(results: List[Dict]) -> List[Dict]:
    seen = set()
    unique = []
    for r in results:
        key = r["content"][:50]
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


def rank_results(results: List[Dict]) -> List[Dict]:
    priority = {"vector": 0, "graph": 1, "keyword": 2}
    return sorted(
        results, key=lambda x: (priority.get(x["source"], 99), -x["score"])
    )


def hybrid_search(
    query: str, top_k: int = 5, verbose: bool = False
) -> List[Dict]:
    """Combine keyword search and graph expansion."""
    all_results = []
    all_results.extend(keyword_search(query))

    kg = KnowledgeGraph()
    entities = extract_entities(query)
    all_results.extend(graph_expansion(entities, kg))

    unique = deduplicate_results(all_results)
    ranked = rank_results(unique)
    return ranked[:top_k]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Hybrid search")
    parser.add_argument("query", nargs="+")
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()

    results = hybrid_search(" ".join(args.query), top_k=args.top_k)
    print(f"Found {len(results)} result(s):")
    for i, r in enumerate(results, 1):
        print(f"  {i}. [{r['source']}] ({int(r['score']*100)}%)")
        print(f"     {r['content'][:100]}...")
