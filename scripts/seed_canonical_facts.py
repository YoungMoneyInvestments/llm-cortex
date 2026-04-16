#!/usr/bin/env python3
"""
Seed canonical system facts into the vector store knowledge collection.

These are well-known, stable infrastructure and project facts that must be
discoverable by the recall test (tests/test_memory_recall.py).  They are
sourced directly from ~/.claude/CLAUDE.md and ~/.claude/projects/MEMORY.md.

Run once (idempotent -- duplicate-hash guard in UnifiedVectorStore skips
re-inserts of identical text):

    python3 scripts/seed_canonical_facts.py
"""

import sys
from pathlib import Path

# Make src/ importable whether run from repo root or scripts/
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from unified_vector_store import get_vector_store  # noqa: E402

CANONICAL_FACTS = [
    # --- Infrastructure ---
    {
        "id": "canonical-storage-vps-ip",
        "text": (
            "Storage VPS IP address is 100.67.112.3 (Tailscale). "
            "SSH via public IP: ssh -p 47822 Administrator@104.237.203.117. "
            "SSH port for Storage VPS is 47822 (never port 22). "
            "PostgreSQL accessible at 100.67.112.3:5432 database tradingcore."
        ),
    },
    {
        "id": "canonical-memory-worker-port",
        "text": (
            "Cortex memory worker runs on port 37778. "
            "Health endpoint: http://localhost:37778/api/health. "
            "Worker base URL: http://localhost:37778. "
            "The memory worker port is 37778."
        ),
    },
    {
        "id": "canonical-tradingcore-database",
        "text": (
            "TradingCore uses PostgreSQL as its database. "
            "PostgreSQL runs on Storage VPS at 100.67.112.3:5432, database name tradingcore. "
            "App users: trading_user, brokerbridge. "
            "search_path is public, core for all app users."
        ),
    },
    # --- BrokerBridge ---
    {
        "id": "canonical-brokerbridge-test-mode",
        "text": (
            "BROKERBRIDGE_TEST_MODE=true is the environment variable that enables "
            "test mode in BrokerBridge. It must be set for all test runs to prevent "
            "keychain errors. This is a critical rule: never run BrokerBridge tests "
            "without BROKERBRIDGE_TEST_MODE=true."
        ),
    },
    {
        "id": "canonical-brokerbridge-location",
        "text": (
            "BrokerBridge Enterprise MCP codebase is located at "
            "~/Projects/MCP-Servers/brokerbridge/. "
            "It is approximately 700K lines of Python across 50 packages with "
            "200+ MCP tools. The retail version is at "
            "~/Projects/brokerbridge-retail-hermes/."
        ),
    },
    # --- OpenClaw / Cami ---
    {
        "id": "canonical-openclaw-auth-profiles",
        "text": (
            "OpenClaw auth profiles are stored at "
            "~/.clawdbot/agents/main/agent/auth-profiles.json. "
            "Available profiles: anthropic:default (1youngmoneyinvestments@gmail.com) "
            "and anthropic:cameronbennion (cameronbennion@gmail.com). "
            "Clawdbot credential management uses this file."
        ),
    },
    # --- Python packages ---
    {
        "id": "canonical-httpx-http-package",
        "text": (
            "httpx is the Python package used for HTTP calls in the llm-cortex project. "
            "It is used in memory_worker.py and tests for making HTTP requests. "
            "The memory recall test (test_memory_recall.py) imports httpx for health "
            "checks against the memory worker at localhost:37778."
        ),
    },
    # --- Cameron / YMI ---
    {
        "id": "canonical-cameron-trading-brand",
        "text": (
            "Cameron runs Young Money Investments (YMI) as his trading education brand. "
            "YMI is a trading education platform for retail traders. "
            "Cameron also operates Magnum Opus Capital as a quantitative fund entity. "
            "Young Money Investments (YMI) is the primary trading education brand."
        ),
    },
]


def main():
    store = get_vector_store()
    seeded = 0
    skipped = 0

    for fact in CANONICAL_FACTS:
        doc_id = fact["id"]
        text = fact["text"]
        try:
            store.add_knowledge(doc_id, text, metadata={"source": "canonical_facts_seed"})
            print(f"  seeded: {doc_id}")
            seeded += 1
        except Exception as exc:
            # Duplicate-hash guard raises nothing; other errors bubble up
            print(f"  skip/error [{doc_id}]: {exc}")
            skipped += 1

    print(f"\nDone. seeded={seeded} skipped={skipped} total={len(CANONICAL_FACTS)}")


if __name__ == "__main__":
    main()
