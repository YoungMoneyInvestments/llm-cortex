#!/usr/bin/env python3
"""
RLM-Graph: Recursive Learning Machine (Layer 7)

Handles queries that exceed context limits by partitioning using
graph structure and recursing on each partition.

Four partition strategies:
  - Ego Graph: Split by entity neighborhoods
  - Path Decomposition: Split path into segments
  - Connected Components: Split by graph clusters
  - Entity Chunks: Fixed-size splits (fallback)
"""

import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent))
from knowledge_graph import KnowledgeGraph
from hybrid_search import (
    hybrid_search,
    extract_entities,
    deduplicate_results,
    rank_results,
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
    parent_query: Optional["SubQuery"] = None


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

    def __init__(
        self,
        max_context_tokens: int = 4000,
        max_recursion_depth: int = 3,
        verbose: bool = False,
    ):
        self.max_context_tokens = max_context_tokens
        self.max_recursion_depth = max_recursion_depth
        self.verbose = verbose
        self.kg = KnowledgeGraph()
        self._history: List[SubQuery] = []
        self._tokens = 0

    def query(
        self,
        query_text: str,
        focal_entities: Optional[List[str]] = None,
    ) -> RLMResult:
        self._history = []
        self._tokens = 0

        root = SubQuery(
            query_text=query_text,
            focal_entities=focal_entities or extract_entities(query_text),
            partition_id="root",
            strategy=PartitionStrategy.EGO_GRAPH,
            depth=0,
        )
        results = self._execute(root)
        final = self._aggregate(results)

        return RLMResult(
            query=query_text,
            results=final,
            subqueries_executed=self._history,
            partition_strategy=self._pick_strategy(root),
            total_tokens_processed=self._tokens,
            recursion_depth=max(
                (sq.depth for sq in self._history), default=0
            ),
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
                r["subquery_depth"] = sq.depth
            return results

    def _estimate_context(self, sq: SubQuery) -> int:
        tokens = len(sq.query_text) // 4
        for e in sq.focal_entities:
            if self.kg.entity_exists(e):
                tokens += len(self.kg.get_neighbors(e, hops=1)) * 50
                tokens += (
                    len(self.kg.get_relationships(e, direction="both")) * 30
                )
        return tokens

    def _partition_and_recurse(self, sq: SubQuery) -> List[Dict]:
        strategy = self._pick_strategy(sq)
        if (
            strategy == PartitionStrategy.PATH_DECOMPOSITION
            and len(sq.focal_entities) >= 2
        ):
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
                parent_query=sq,
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
                focal_entities=[path[i], path[i + 1]],
                partition_id=f"{sq.partition_id}.path_{i}",
                strategy=PartitionStrategy.PATH_DECOMPOSITION,
                depth=sq.depth + 1,
                parent_query=sq,
            )
            for i in range(len(path) - 1)
        ]

    def _pick_strategy(self, sq: SubQuery) -> PartitionStrategy:
        if len(sq.focal_entities) >= 2:
            kws = ["connected", "relationship", "link", "between", "path"]
            if any(kw in sq.query_text.lower() for kw in kws):
                return PartitionStrategy.PATH_DECOMPOSITION
        return PartitionStrategy.EGO_GRAPH

    def _aggregate(self, results: List[Dict]) -> List[Dict]:
        unique = deduplicate_results(results)
        for r in unique:
            penalty = r.get("subquery_depth", 0) * 0.05
            r["score"] = max(0, r["score"] - penalty)
        return rank_results(unique)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="RLM-Graph recursive search")
    parser.add_argument("query", nargs="+")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    rlm = RLMGraph(verbose=args.verbose)
    result = rlm.query(" ".join(args.query))

    print(f"\nQuery: {result.query}")
    print(
        f"Strategy: {result.partition_strategy.value if result.partition_strategy else 'none'}"
    )
    print(f"Depth: {result.recursion_depth}")
    print(f"Subqueries: {len(result.subqueries_executed)}")
    print(f"Tokens: {result.total_tokens_processed:,}")
    print(f"\nResults ({len(result.results)}):")
    for i, r in enumerate(result.results[:5], 1):
        print(f"  {i}. [{r['source']}] ({int(r['score']*100)}%)")
        print(f"     {r['content'][:100]}...")
