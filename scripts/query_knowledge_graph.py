#!/usr/bin/env python3
"""
Knowledge Graph Query Tool (Layer 6 CLI)

Usage:
    python3 query_knowledge_graph.py --stats
    python3 query_knowledge_graph.py --related-to "entity_name"
    python3 query_knowledge_graph.py --find-path "entity1" "entity2"
    python3 query_knowledge_graph.py --subgraph "entity" --depth 2
    python3 query_knowledge_graph.py --search "keyword"
"""

import argparse
import json
import os
from collections import defaultdict, deque
from pathlib import Path
from typing import Dict, List

GRAPH_PATH = Path(
    os.environ.get(
        "CORTEX_GRAPH_FILE",
        str(
            Path(os.environ.get("CORTEX_WORKSPACE", str(Path.home() / "cortex")))
            / ".planning"
            / "knowledge-graph.json"
        ),
    )
)


class KnowledgeGraphQuery:
    def __init__(self, graph_path: Path = GRAPH_PATH):
        with open(graph_path) as f:
            data = json.load(f)
        self.nodes = {n["id"]: n for n in data["nodes"]}
        self.edges = data["edges"]
        self.outgoing = defaultdict(list)
        self.incoming = defaultdict(list)
        for edge in self.edges:
            rel = edge.get("rel_type", edge.get("relation", "related"))
            self.outgoing[edge["source"]].append((rel, edge["target"]))
            self.incoming[edge["target"]].append((rel, edge["source"]))

    def search_nodes(self, query: str) -> List[Dict]:
        q = query.lower()
        return [n for n in self.nodes.values() if q in n["id"].lower()]

    def get_relationships(self, node_id: str) -> Dict:
        return {
            "outgoing": [
                {"relation": r, "target": t} for r, t in self.outgoing[node_id]
            ],
            "incoming": [
                {"relation": r, "source": s} for r, s in self.incoming[node_id]
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
                edges_list.append(
                    {"source": current, "relation": rel, "target": target}
                )
                if d < depth:
                    queue.append((target, d + 1))
            for rel, source in self.incoming[current]:
                edges_list.append(
                    {"source": source, "relation": rel, "target": current}
                )
                if d < depth:
                    queue.append((source, d + 1))
        return {
            "nodes": [self.nodes[n] for n in nodes_set if n in self.nodes],
            "edges": edges_list,
        }

    def stats(self) -> Dict:
        node_types = defaultdict(int)
        for node in self.nodes.values():
            node_types[node.get("attributes", {}).get("type", "unknown")] += 1
        return {
            "total_nodes": len(self.nodes),
            "total_edges": len(self.edges),
            "node_types": dict(node_types),
        }


def main():
    parser = argparse.ArgumentParser(description="Query knowledge graph")
    parser.add_argument("--related-to", help="Show relationships for entity")
    parser.add_argument("--find-path", nargs=2, metavar=("START", "END"))
    parser.add_argument("--subgraph", help="Get subgraph around entity")
    parser.add_argument("--depth", type=int, default=2)
    parser.add_argument("--stats", action="store_true")
    parser.add_argument("--search", help="Search nodes by keyword")
    args = parser.parse_args()

    kg = KnowledgeGraphQuery()

    if args.stats:
        s = kg.stats()
        print(f"\nKnowledge Graph Statistics")
        print(f"  Total nodes: {s['total_nodes']}")
        print(f"  Total edges: {s['total_edges']}")
        for t, c in sorted(s["node_types"].items(), key=lambda x: -x[1]):
            print(f"    {t}: {c}")

    elif args.related_to:
        results = kg.search_nodes(args.related_to)
        if not results:
            print(f"Not found: {args.related_to}")
        else:
            node = results[0]
            rels = kg.get_relationships(node["id"])
            print(f"\n{node['id']}")
            for r in rels["outgoing"]:
                print(f"  -{r['relation']}-> {r['target']}")
            for r in rels["incoming"]:
                print(f"  {r['source']} -{r['relation']}->")

    elif args.find_path:
        s = kg.search_nodes(args.find_path[0])
        e = kg.search_nodes(args.find_path[1])
        if s and e:
            paths = kg.find_path(s[0]["id"], e[0]["id"])
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
            sg = kg.get_subgraph(results[0]["id"], depth=args.depth)
            print(f"  Nodes: {len(sg['nodes'])}, Edges: {len(sg['edges'])}")

    elif args.search:
        results = kg.search_nodes(args.search)
        print(f"  Found {len(results)} result(s):")
        for n in results:
            print(
                f"    - {n['id']} ({n.get('attributes', {}).get('type', '?')})"
            )

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
