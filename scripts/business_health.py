#!/usr/bin/env python3
"""
Business Health Snapshot - on-demand cross-system status check.

Run manually or hook into context_loader for session startup.
Aggregates: BB Retail activity, Discord RAG status, trading signals, MoltyTrades.

Usage:
    python3 business_health.py
    python3 business_health.py --save  # save to cortex as observation
"""

import argparse
import json
import os
import sqlite3
import subprocess
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

CORTEX_URL = "http://127.0.0.1:37778"
_KEY_FILE = Path.home() / ".cortex" / "data" / ".worker_api_key"


def get_cortex_key() -> str:
    try:
        return _KEY_FILE.read_text().strip() if _KEY_FILE.exists() else ""
    except OSError:
        return ""


def run(cmd: list, cwd=None) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10, cwd=cwd)
        return r.stdout.strip()
    except Exception:
        return ""


# -- Data collection functions ------------------------------------------------

def check_cortex_worker() -> dict:
    try:
        req = urllib.request.Request(f"{CORTEX_URL}/api/health", method="GET")
        with urllib.request.urlopen(req, timeout=3) as r:
            body = json.loads(r.read())
        return {"status": "healthy", "observations": body.get("total_observations", 0), "sessions": body.get("active_sessions", 0)}
    except Exception:
        return {"status": "unreachable"}


def check_bb_retail() -> dict:
    """Check BrokerBridge Retail SQLite for basic metrics."""
    result = {}
    # state.db - conversation messages
    state_db = Path.home() / ".brokerbridge" / "state.db"
    if state_db.exists():
        try:
            conn = sqlite3.connect(state_db, timeout=5)
            msg_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            session_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            conn.close()
            result["messages"] = msg_count
            result["sessions"] = session_count
        except Exception:
            pass
    # desk_proposals.db
    prop_db = Path.home() / ".brokerbridge" / "desk_proposals.db"
    if prop_db.exists():
        try:
            conn = sqlite3.connect(prop_db, timeout=5)
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            for tbl in tables:
                if "proposal" in tbl.lower():
                    count = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                    result[tbl] = count
            conn.close()
        except Exception:
            pass
    return {"status": "ok" if result else "no_data", **result}


def check_discord_rag() -> dict:
    """Check the Discord RAG SQLite for message counts."""
    db_path = Path.home() / "clawd" / "data" / "discord-embeddings.db"
    if not db_path.exists():
        return {"status": "db_not_found"}
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        conn.close()
        return {"status": "ok", "chunks_indexed": chunks}
    except Exception as e:
        return {"status": "error", "error": str(e)[:100]}


def check_moltytrades() -> dict:
    """Ping MoltyTrades API for basic health."""
    urls = [
        "https://moltytrades-production.up.railway.app/health",
        "https://moltytrades-production.up.railway.app/",
    ]
    for url in urls:
        try:
            req = urllib.request.Request(url, method="GET")
            req.add_header("User-Agent", "business-health-check/1.0")
            with urllib.request.urlopen(req, timeout=8) as r:
                return {"status": "up", "http": r.status}
        except urllib.error.HTTPError as e:
            return {"status": f"http_{e.code}", "note": "Railway trial may have expired"}
        except Exception as e:
            continue
    return {"status": "unreachable"}


def check_trading_signals() -> dict:
    """Check recent trading signal activity from Storage VPS."""
    # Try psql query on tradingcore
    psql = "/opt/homebrew/opt/libpq/bin/psql"
    conn_str = "postgresql://trading_user@100.67.112.3:5432/tradingcore"
    query = """
    SELECT COUNT(*) as signal_count, MAX(created_at) as last_signal
    FROM signals WHERE created_at > NOW() - INTERVAL '7 days'
    """
    output = run([psql, conn_str, "-t", "-c", query])
    if output and "|" in output:
        parts = output.strip().split("|")
        if len(parts) >= 2:
            return {"status": "ok", "signals_7d": parts[0].strip(), "last_signal": parts[1].strip()}
    return {"status": "unreachable_or_no_data"}


def check_github_prs() -> dict:
    """Check open PRs across key repos using gh CLI."""
    output = run(["gh", "pr", "list", "--repo", "YoungMoneyInvestments/moltytrades", "--json", "title,state,number"])
    if output:
        try:
            prs = json.loads(output)
            return {"status": "ok", "open_prs": len(prs), "prs": [p.get("title", "?")[:50] for p in prs[:3]]}
        except Exception:
            pass
    return {"status": "gh_unavailable"}


# -- Main ---------------------------------------------------------------------

def build_report() -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"Business Health Snapshot - {now}", "=" * 50]

    cortex = check_cortex_worker()
    lines.append(f"\nCortex Worker: {cortex.get('status')} | {cortex.get('observations',0):,} observations | {cortex.get('sessions',0)} active sessions")

    bb = check_bb_retail()
    if bb.get("status") == "ok":
        msg = bb.get("messages", "?")
        sess = bb.get("sessions", "?")
        lines.append(f"BB Retail: {msg} messages | {sess} sessions")
    else:
        lines.append(f"BB Retail: {bb.get('status')} ({bb.get('error','')})")

    discord = check_discord_rag()
    if discord.get("status") == "ok":
        lines.append(f"Discord RAG: {discord.get('chunks_indexed','?'):,} chunks indexed")
    else:
        lines.append(f"Discord RAG: {discord.get('status')}")

    molty = check_moltytrades()
    lines.append(f"MoltyTrades API: {molty.get('status')}")

    signals = check_trading_signals()
    if signals.get("status") == "ok":
        lines.append(f"Trading Signals (7d): {signals.get('signals_7d','?')} | last: {signals.get('last_signal','?')}")
    else:
        lines.append(f"Trading Signals: {signals.get('status')}")

    prs = check_github_prs()
    if prs.get("status") == "ok":
        lines.append(f"MoltyTrades PRs: {prs.get('open_prs',0)} open")

    return "\n".join(lines)


def save_to_cortex(report: str):
    key = get_cortex_key()
    if not key:
        print("No cortex API key - skipping save")
        return
    payload = json.dumps({
        "content": report,
        "tags": "business-health,weekly,snapshot"
    }).encode()
    req = urllib.request.Request(
        f"{CORTEX_URL}/api/memory/save",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            print(f"Saved to cortex: {r.status}")
    except Exception as e:
        print(f"Cortex save failed: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--save", action="store_true", help="Save snapshot to cortex")
    args = parser.parse_args()

    report = build_report()
    print(report)

    if args.save:
        save_to_cortex(report)
