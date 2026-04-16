#!/usr/bin/env python3
"""
Evaluation harness: measures whether the memory system can recall answers
to known questions from stored memories.

Run as pytest:
    pytest tests/test_memory_recall.py -v

Run standalone for a formatted report:
    python tests/test_memory_recall.py
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

# Allow importing from sibling src/ directory
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# ── Constants ────────────────────────────────────────────────────────────────

WORKER_BASE = "http://localhost:37778"
HEALTH_URL = f"{WORKER_BASE}/api/health"
EVAL_DIR = Path.home() / ".cortex" / "eval"

CANONICAL_QA = [
    {
        "q": "What database does TradingCore use?",
        "expected_keywords": ["postgresql", "tradingcore", "postgres"],
    },
    {
        "q": "What port does the memory worker run on?",
        "expected_keywords": ["37778"],
    },
    {
        "q": "What is the Storage VPS IP address?",
        "expected_keywords": ["100.67.112.3", "storage"],
    },
    {
        "q": "What is the SSH port for the Storage VPS?",
        "expected_keywords": ["47822"],
    },
    {
        "q": "What environment variable enables test mode in BrokerBridge?",
        "expected_keywords": ["brokerbridge_test_mode", "test_mode"],
    },
    {
        "q": "Where are OpenClaw auth profiles stored?",
        "expected_keywords": ["auth-profiles.json", "clawdbot", "openclaw"],
    },
    {
        "q": "What Python package is used for HTTP calls in this project?",
        "expected_keywords": ["httpx"],
    },
    {
        "q": "What is the Cortex memory worker port?",
        "expected_keywords": ["37778"],
    },
    {
        "q": "What trading education brand does Cameron run?",
        "expected_keywords": ["ymi", "young money"],
    },
    {
        "q": "Where is the BrokerBridge codebase located?",
        "expected_keywords": ["brokerbridge", "mcp-servers", "projects"],
    },
]


# ── Helpers ──────────────────────────────────────────────────────────────────


def _worker_running() -> bool:
    """Return True if the memory worker health endpoint responds OK."""
    try:
        resp = httpx.get(HEALTH_URL, timeout=2.0)
        return resp.status_code == 200
    except Exception:
        return False


def _search(query: str, limit: int = 3) -> list[dict]:
    """Search via MemoryRetriever (direct DB access, no HTTP)."""
    from memory_retriever import MemoryRetriever

    retriever = MemoryRetriever()
    return retriever.search(query, limit=limit)


def _result_text(results: list[dict]) -> str:
    """Flatten top results into a single lowercase string for keyword matching."""
    parts = []
    for r in results:
        for field in ("summary", "text", "content", "snippet"):
            val = r.get(field)
            if val:
                parts.append(str(val))
    return " ".join(parts).lower()


def run_recall_evaluation() -> dict:
    """
    Run all CANONICAL_QA pairs against the memory system.

    Returns:
        {
            "recall_rate": float,
            "hits": int,
            "total": int,
            "results": [{"question", "hit", "top_snippet", "latency_ms"}, ...]
        }
    """
    hits = 0
    rows = []

    for pair in CANONICAL_QA:
        q = pair["q"]
        keywords = [kw.lower() for kw in pair["expected_keywords"]]

        t0 = time.perf_counter()
        try:
            results = _search(q, limit=3)
        except Exception as exc:
            results = []
            full_text = ""
            top_snippet = f"ERROR: {exc}"
        else:
            full_text = _result_text(results) if results else ""
            top_snippet = full_text[:200] if full_text else "(no results)"
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)

        hit = any(kw in full_text.lower() for kw in keywords) if full_text else False
        if hit:
            hits += 1

        rows.append(
            {
                "question": q,
                "hit": hit,
                "top_snippet": top_snippet,
                "latency_ms": latency_ms,
                "expected_keywords": keywords,
            }
        )

    total = len(CANONICAL_QA)
    return {
        "recall_rate": hits / total if total else 0.0,
        "hits": hits,
        "total": total,
        "results": rows,
    }


# ── Pytest tests ─────────────────────────────────────────────────────────────


def test_worker_is_running():
    """Health check — skip gracefully if worker isn't up."""
    if not _worker_running():
        pytest.skip("Memory worker not running at localhost:37778")
    resp = httpx.get(HEALTH_URL, timeout=2.0)
    assert resp.status_code == 200


def test_search_returns_results():
    """Single search must return at least one result for a common term."""
    if not _worker_running():
        pytest.skip("Memory worker not running at localhost:37778")
    try:
        results = _search("BrokerBridge trading", limit=5)
    except FileNotFoundError:
        pytest.skip("Observations DB not found — memory worker has no data yet")
    assert isinstance(results, list), "search() must return a list"
    assert len(results) > 0, "Expected at least one result for 'BrokerBridge trading'"


def test_memory_recall_rate():
    """End-to-end recall: assert >= 50% of canonical Q&A pairs are answered."""
    if not _worker_running():
        pytest.skip("Memory worker not running at localhost:37778")
    try:
        report = run_recall_evaluation()
    except FileNotFoundError:
        pytest.skip("Observations DB not found — memory worker has no data yet")

    rate = report["recall_rate"]
    assert rate >= 0.5, (
        f"Recall rate {rate:.0%} is below the 50% floor "
        f"({report['hits']}/{report['total']} questions answered). "
        "Memory system may be degraded or underpopulated."
    )


def test_evaluation_report():
    """Run full eval and persist results to ~/.cortex/eval/recall_report_<ts>.json."""
    if not _worker_running():
        pytest.skip("Memory worker not running at localhost:37778")
    try:
        report = run_recall_evaluation()
    except FileNotFoundError:
        pytest.skip("Observations DB not found — memory worker has no data yet")

    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = EVAL_DIR / f"recall_report_{ts}.json"
    report_path.write_text(json.dumps(report, indent=2))

    assert report_path.exists(), "Report file was not written"
    loaded = json.loads(report_path.read_text())
    assert "recall_rate" in loaded
    assert "results" in loaded
    assert len(loaded["results"]) == len(CANONICAL_QA)


# ── CLI mode ─────────────────────────────────────────────────────────────────


def _print_report(report: dict) -> None:
    rate = report["recall_rate"]
    hits = report["hits"]
    total = report["total"]

    print(f"\n{'='*60}")
    print(f"  Cortex Memory Recall Evaluation")
    print(f"  Recall rate: {rate:.0%}  ({hits}/{total})")
    print(f"{'='*60}")
    for row in report["results"]:
        status = "HIT " if row["hit"] else "MISS"
        snippet = row["top_snippet"][:80].replace("\n", " ")
        print(f"\n[{status}] {row['question']}")
        print(f"       {row['latency_ms']} ms | {snippet}")
    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    if not _worker_running():
        print("Memory worker not running at localhost:37778 — cannot evaluate.")
        sys.exit(1)

    try:
        report = run_recall_evaluation()
    except FileNotFoundError as exc:
        print(f"Cannot run evaluation: {exc}")
        sys.exit(1)

    _print_report(report)

    # Write report
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = EVAL_DIR / f"recall_report_{ts}.json"
    report_path.write_text(json.dumps(report, indent=2))
    print(f"Report written to: {report_path}")

    sys.exit(0 if report["recall_rate"] >= 0.5 else 1)
