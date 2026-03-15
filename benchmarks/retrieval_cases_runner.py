#!/usr/bin/env python3
"""Run checked-in retrieval fixtures through the current retriever."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from memory_retriever import MemoryRetriever
from tests.fixtures.retrieval_cases import RANKING_CASES


class FixtureRetriever(MemoryRetriever):
    def __init__(self, case: dict):
        super().__init__(
            obs_db_path=Path("/tmp/unused-observations.db"),
            vec_db_path=Path("/tmp/unused-vectors.db"),
        )
        self.case = case

    def _search_observations(self, query, limit, source, agent, session_id):
        return [dict(item) for item in self.case.get("observations", [])]

    def _search_vector_store(self, query, limit):
        return [dict(item) for item in self.case.get("vector_store", [])]

    def _search_session_summaries(self, query, limit):
        return [dict(item) for item in self.case.get("session_summaries", [])]


def list_cases() -> int:
    for name in sorted(RANKING_CASES):
        print(name)
    return 0


def run_case(name: str, limit: int) -> int:
    case = RANKING_CASES[name]
    retriever = FixtureRetriever(case)
    results = retriever.search(case["query"], limit=limit)

    print(f"CASE {name}")
    print(f"query: {case['query']}")
    for index, result in enumerate(results, start=1):
        print(
            f"{index:02d}. {result['id']} "
            f"score={result['score']:.3f} origin={result.get('origin')} "
            f"summary={result.get('summary', '')}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", nargs="*", default=[])
    parser.add_argument("--limit", type=int, default=5, help="Max results per case")
    parser.add_argument("--list", action="store_true", help="List available fixture cases")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list:
        return list_cases()

    selected = args.cases or sorted(RANKING_CASES)
    for name in selected:
        if name not in RANKING_CASES:
            print(f"Unknown case: {name}. Choose from: {sorted(RANKING_CASES)}", file=sys.stderr)
            return 2
        run_case(name, args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
