from __future__ import annotations


def make_result(
    result_id: str,
    summary: str,
    *,
    origin: str,
    score: float,
    timestamp: str | None = None,
    source: str = "post_tool_use",
    tool: str | None = None,
    agent: str | None = "main",
) -> dict:
    return {
        "id": result_id,
        "summary": summary,
        "source": source,
        "tool": tool,
        "agent": agent,
        "timestamp": timestamp,
        "score": score,
        "origin": origin,
    }


RANKING_CASES = {
    "coverage_vs_recency": {
        "query": "gamma exposure hedging",
        "observations": [
            make_result(
                "obs-older-full",
                "Gamma exposure hedging plan for dealer risk.",
                origin="observations",
                score=-5.0,
                timestamp="2026-03-10T10:00:00+00:00",
                tool="search",
            ),
            make_result(
                "obs-newer-partial",
                "Gamma exposure note for dealer desk.",
                origin="observations",
                score=-5.0,
                timestamp="2026-03-13T09:30:00+00:00",
                tool="search",
            ),
        ],
        "vector_store": [
            make_result(
                "vec-recent-light",
                "Hedging checklist.",
                origin="vector_store",
                score=-5.0,
                timestamp="2026-03-13T09:45:00+00:00",
            ),
        ],
        "session_summaries": [],
        "expected_order": [
            "obs-older-full",
            "obs-newer-partial",
            "vec-recent-light",
        ],
    },
    "stable_tie_break": {
        "query": "portfolio review",
        "observations": [
            make_result(
                "obs-zeta",
                "Portfolio review for account balances.",
                origin="observations",
                score=-4.0,
                timestamp="2026-03-12T12:00:00+00:00",
                tool="search",
            ),
            make_result(
                "obs-alpha",
                "Portfolio review for account balances.",
                origin="observations",
                score=-4.0,
                timestamp="2026-03-12T12:00:00+00:00",
                tool="search",
            ),
        ],
        "vector_store": [],
        "session_summaries": [],
        "expected_order": ["obs-alpha", "obs-zeta"],
    },
    "dedup_preference": {
        "query": "exit manager brokerbridge",
        "observations": [
            make_result(
                "obs-rich",
                "BrokerBridge exit manager failed after repeated gateway timeout during paper execution.",
                origin="observations",
                score=-8.0,
                timestamp="2026-03-13T08:00:00+00:00",
                tool="bash",
            ),
        ],
        "vector_store": [
            make_result(
                "vec-thin",
                "BrokerBridge exit manager failed after repeated gateway timeout.",
                origin="vector_store",
                score=-8.0,
                timestamp="2026-03-11T08:00:00+00:00",
            ),
        ],
        "session_summaries": [],
        "expected_ids": ["obs-rich"],
    },
}
