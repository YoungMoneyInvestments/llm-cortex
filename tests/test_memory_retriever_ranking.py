from __future__ import annotations

from pathlib import Path

from memory_retriever import MemoryRetriever
from tests.fixtures.retrieval_cases import RANKING_CASES


class StubRetriever(MemoryRetriever):
    def __init__(self, case: dict):
        super().__init__(obs_db_path=Path("/tmp/unused-observations.db"), vec_db_path=Path("/tmp/unused-vectors.db"))
        self.case = case

    def _search_observations(self, query, limit, source, agent, session_id):
        return [dict(item) for item in self.case.get("observations", [])]

    def _search_vector_store(self, query, limit):
        return [dict(item) for item in self.case.get("vector_store", [])]

    def _search_session_summaries(self, query, limit):
        return [dict(item) for item in self.case.get("session_summaries", [])]


def test_search_prefers_coverage_before_recency_when_scores_tie():
    retriever = StubRetriever(RANKING_CASES["coverage_vs_recency"])

    results = retriever.search(RANKING_CASES["coverage_vs_recency"]["query"], limit=5)

    assert [result["id"] for result in results] == RANKING_CASES["coverage_vs_recency"]["expected_order"]


def test_search_uses_stable_tie_breakers_for_equal_ranked_results():
    retriever = StubRetriever(RANKING_CASES["stable_tie_break"])

    results = retriever.search(RANKING_CASES["stable_tie_break"]["query"], limit=5)

    assert [result["id"] for result in results] == RANKING_CASES["stable_tie_break"]["expected_order"]


def test_search_deduplicates_similar_results_and_keeps_preferred_version():
    retriever = StubRetriever(RANKING_CASES["dedup_preference"])

    results = retriever.search(RANKING_CASES["dedup_preference"]["query"], limit=5)

    assert [result["id"] for result in results] == RANKING_CASES["dedup_preference"]["expected_ids"]


def test_search_keeps_public_result_shape_stable():
    retriever = StubRetriever(RANKING_CASES["coverage_vs_recency"])

    result = retriever.search(RANKING_CASES["coverage_vs_recency"]["query"], limit=1)[0]

    assert set(result.keys()) == {
        "id",
        "summary",
        "source",
        "tool",
        "agent",
        "timestamp",
        "score",
        "origin",
    }
