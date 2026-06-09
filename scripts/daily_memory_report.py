#!/usr/bin/env python3
"""Write the daily Cortex memory health note for Obsidian."""

from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


HOME = Path.home()
DATA_DIR = Path(os.environ.get("CORTEX_DATA_DIR", HOME / ".cortex" / "data")).expanduser()
OBS_DB = DATA_DIR / "cortex-observations.db"
VECTOR_DB = DATA_DIR / "cortex-vectors.db"
KEY_FILE = DATA_DIR / ".worker_api_key"
WORKER_URL = os.environ.get("CORTEX_WORKER_URL", "http://127.0.0.1:37778")
REPORT_PATH = HOME / "Knowledge" / "claude-memory" / "reference_cortex_memory_health.md"


def _read_key() -> str:
    env_key = os.environ.get("CORTEX_WORKER_API_KEY", "").strip()
    if env_key:
        return env_key
    try:
        return KEY_FILE.read_text().strip()
    except OSError:
        return ""


def _worker_health() -> dict:
    headers = {"Content-Type": "application/json"}
    key = _read_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(f"{WORKER_URL}/api/health", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        return {"status": "unreachable", "error": str(exc)}


def _group_counts(conn: sqlite3.Connection, sql: str) -> dict:
    return {str(key): count for key, count in conn.execute(sql).fetchall()}


def _observation_metrics() -> dict:
    if not OBS_DB.exists():
        return {"error": f"missing {OBS_DB}"}
    conn = sqlite3.connect(str(OBS_DB))
    try:
        return {
            "observations": conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0],
            "agents": _group_counts(
                conn,
                "SELECT COALESCE(agent,'unknown'), COUNT(*) FROM observations GROUP BY agent",
            ),
            "memory_types": _group_counts(
                conn,
                "SELECT COALESCE(memory_type,'unknown'), COUNT(*) FROM observations GROUP BY memory_type",
            ),
            "vector_sync": _group_counts(
                conn,
                "SELECT vector_synced, COUNT(*) FROM observations GROUP BY vector_synced",
            ),
            "sessions": _group_counts(
                conn,
                "SELECT status, COUNT(*) FROM sessions GROUP BY status",
            ),
            "pending_observations": conn.execute(
                "SELECT COUNT(*) FROM observations WHERE status != 'processed'"
            ).fetchone()[0],
        }
    finally:
        conn.close()


def _vector_metrics() -> dict:
    if not VECTOR_DB.exists():
        return {"error": f"missing {VECTOR_DB}"}
    conn = sqlite3.connect(str(VECTOR_DB))
    try:
        total, embedded = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(has_embedding), 0) FROM documents"
        ).fetchone()
        by_collection = _group_counts(
            conn,
            "SELECT collection, COUNT(*) FROM documents GROUP BY collection",
        )
        return {
            "documents": total,
            "embedded": embedded,
            "unembedded": total - embedded,
            "collections": by_collection,
        }
    finally:
        conn.close()


def _format_counts(counts: dict) -> str:
    if not counts:
        return "none"
    return ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))


def _issues(obs: dict, vec: dict, worker: dict) -> list[str]:
    issues: list[str] = []
    if worker.get("status") != "healthy":
        issues.append(f"worker status {worker.get('status', 'unknown')}")
    if obs.get("pending_observations", 0):
        issues.append(f"{obs['pending_observations']} observations not processed")
    vector_sync = obs.get("vector_sync", {})
    if str(0) in vector_sync:
        issues.append(f"{vector_sync[str(0)]} observations pending vector sync")
    if vec.get("unembedded", 0):
        issues.append(f"{vec['unembedded']} vector documents missing embeddings")
    sessions = obs.get("sessions", {})
    if sessions.get("active", 0):
        issues.append(f"{sessions['active']} sessions still active")
    if sessions.get("ended", 0):
        issues.append(f"{sessions['ended']} sessions ended but unsummarized")
    return issues


def main() -> int:
    generated_at = datetime.now(timezone.utc).isoformat()
    worker = _worker_health()
    obs = _observation_metrics()
    vec = _vector_metrics()
    issues = _issues(obs, vec, worker)

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    content = f"""---
name: cortex-memory-health
description: Daily Cortex memory health snapshot for agent memory plumbing
metadata:
  type: reference
---

# Cortex Memory Health

Fact — generated at `{generated_at}`.

## Status
- Worker: `{worker.get("status", "unknown")}`
- Observations: `{obs.get("observations", "unknown")}`
- Pending observations: `{obs.get("pending_observations", "unknown")}`
- Sessions: `{_format_counts(obs.get("sessions", {}))}`
- Vector documents: `{vec.get("documents", "unknown")}`
- Embedded vector documents: `{vec.get("embedded", "unknown")}`
- Unembedded vector documents: `{vec.get("unembedded", "unknown")}`

## Distribution
- Agents: `{_format_counts(obs.get("agents", {}))}`
- Memory types: `{_format_counts(obs.get("memory_types", {}))}`
- Vector sync states: `{_format_counts(obs.get("vector_sync", {}))}`
- Vector collections: `{_format_counts(vec.get("collections", {}))}`

## Issues
{chr(10).join(f"- {issue}" for issue in issues) if issues else "- none"}
"""
    REPORT_PATH.write_text(content, encoding="utf-8")
    print(f"wrote {REPORT_PATH}")
    if issues:
        # Issues are reported in the log line and the report's Issues section.
        # Exit 0 anyway: the job itself succeeded (report written). Nonzero is
        # reserved for real crashes (unhandled exceptions -> SystemExit != 0),
        # so launchd last-exit-status stays meaningful.
        print("issues: " + "; ".join(issues))
        return 0
    print("issues: none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
