#!/usr/bin/env python3
"""
Memory Retriever — Token-efficient 3-layer retrieval (Layer 0)

Implements the search -> timeline -> details pattern, minimizing
token waste when querying memory.

Layers:
  L1: search()      — Compact index: IDs + 1-line summaries (~20 tokens each)
  L2: timeline()    — Chronological context around an observation (~100 tokens each)
  L3: get_details() — Full observation text (variable, only when needed)

Configure:
    CORTEX_WORKSPACE  — Project root (default: ~/cortex)

Usage:
    from memory_retriever import MemoryRetriever

    retriever = MemoryRetriever()
    results = retriever.search("auth module bug")
    context = retriever.timeline(42, window=5)
    details = retriever.get_details([42, 43])
"""

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import os

# ── Config ──────────────────────────────────────────────────────────────────

WORKSPACE = Path(os.environ.get("CORTEX_WORKSPACE", str(Path.home() / "cortex")))
OBS_DB_PATH = WORKSPACE / "data" / "cortex-observations.db"
VEC_DB_PATH = WORKSPACE / "data" / "cortex-vectors.db"

# ── Main class ──────────────────────────────────────────────────────────────


class MemoryRetriever:
    """Token-efficient 3-layer memory retrieval."""

    def __init__(
        self,
        obs_db_path: Optional[Path] = None,
        vec_db_path: Optional[Path] = None,
    ):
        self.obs_db_path = obs_db_path or OBS_DB_PATH
        self.vec_db_path = vec_db_path or VEC_DB_PATH

        self._obs_conn: Optional[sqlite3.Connection] = None
        self._vec_conn: Optional[sqlite3.Connection] = None

    @property
    def obs_db(self) -> sqlite3.Connection:
        """Lazy connection to observations database."""
        if self._obs_conn is None:
            if not self.obs_db_path.exists():
                raise FileNotFoundError(
                    f"Observations DB not found: {self.obs_db_path}. "
                    "Start the memory worker first."
                )
            self._obs_conn = sqlite3.connect(str(self.obs_db_path))
            self._obs_conn.row_factory = sqlite3.Row
        return self._obs_conn

    @property
    def vec_db(self) -> sqlite3.Connection:
        """Lazy connection to vector store database."""
        if self._vec_conn is None:
            if not self.vec_db_path.exists():
                raise FileNotFoundError(
                    f"Vector DB not found: {self.vec_db_path}. "
                    "Initialize the vector store first."
                )
            self._vec_conn = sqlite3.connect(str(self.vec_db_path))
            self._vec_conn.row_factory = sqlite3.Row
        return self._vec_conn

    # ── L1: Search (compact index) ─────────────────────────────────────────

    def search(
        self,
        query: str,
        limit: int = 20,
        source: Optional[str] = None,
        agent: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> list:
        """
        L1: Compact index search. Returns IDs + 1-line summaries.
        Token cost: ~20 tokens per result.
        """
        results = []

        # Search observations database (summaries)
        results.extend(
            self._search_observations(query, limit, source, agent, session_id)
        )

        # Search vector store (FTS5)
        results.extend(self._search_vector_store(query, limit))

        # Deduplicate by underlying observation ID
        seen = set()
        deduped = []
        for r in results:
            key = r.get("obs_id") or r["id"]
            if key not in seen:
                seen.add(key)
                deduped.append(r)

        # Sort by relevance (score) and limit
        deduped.sort(key=lambda x: x.get("score", 0))
        return deduped[:limit]

    def _search_observations(
        self,
        query: str,
        limit: int,
        source: Optional[str],
        agent: Optional[str],
        session_id: Optional[str],
    ) -> list:
        """Search observations database using LIKE matching on summaries."""
        try:
            terms = query.lower().split()
            if not terms:
                return []

            conditions = []
            params = []
            for term in terms:
                conditions.append("(LOWER(summary) LIKE ? OR LOWER(tool_name) LIKE ?)")
                params.extend([f"%{term}%", f"%{term}%"])

            where = " AND ".join(conditions)

            if source:
                where += " AND source = ?"
                params.append(source)
            if agent:
                where += " AND agent = ?"
                params.append(agent)
            if session_id:
                where += " AND session_id = ?"
                params.append(session_id)

            params.append(limit)

            rows = self.obs_db.execute(
                f"SELECT id, session_id, timestamp, source, tool_name, agent, summary "
                f"FROM observations "
                f"WHERE status = 'processed' AND {where} "
                f"ORDER BY id DESC LIMIT ?",
                params,
            ).fetchall()

            return [
                {
                    "id": f"obs-{r['id']}",
                    "obs_id": r["id"],
                    "summary": self._truncate_summary(r["summary"]),
                    "source": r["source"],
                    "tool": r["tool_name"],
                    "agent": r["agent"],
                    "timestamp": r["timestamp"],
                    "session_id": r["session_id"][:12] + "..." if r["session_id"] else None,
                    "score": 0,
                    "origin": "observations",
                }
                for r in rows
            ]
        except Exception:
            return []

    def _search_vector_store(self, query: str, limit: int) -> list:
        """Search vector store using FTS5."""
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            from unified_vector_store import get_vector_store
            store = get_vector_store(self.vec_db_path)
            results = store.search(query, limit=limit)
            return [
                {
                    "id": r["id"],
                    "summary": self._truncate_summary(r["text"]),
                    "collection": r.get("collection", "unknown"),
                    "timestamp": r.get("created_at"),
                    "score": r.get("score", 0),
                    "origin": "vector_store",
                }
                for r in results
            ]
        except Exception:
            return []

    def _truncate_summary(self, text: Optional[str], max_len: int = 120) -> str:
        """Truncate text to create a compact summary line."""
        if not text:
            return ""
        clean = text.replace("\n", " ").strip()
        if len(clean) > max_len:
            return clean[:max_len] + "..."
        return clean

    # ── L2: Timeline (chronological context) ────────────────────────────────

    def timeline(
        self,
        observation_id: int,
        window: int = 5,
    ) -> list:
        """
        L2: Chronological context around a specific observation.
        Token cost: ~100 tokens per item.
        """
        target = self.obs_db.execute(
            "SELECT id, session_id, timestamp, source, tool_name, agent, summary "
            "FROM observations WHERE id = ?",
            (observation_id,),
        ).fetchone()

        if not target:
            return []

        session_id = target["session_id"]
        target_id = target["id"]

        rows = self.obs_db.execute(
            "SELECT id, session_id, timestamp, source, tool_name, agent, summary "
            "FROM observations "
            "WHERE session_id = ? AND id BETWEEN ? AND ? "
            "ORDER BY id ASC",
            (session_id, target_id - window, target_id + window),
        ).fetchall()

        return [
            {
                "id": r["id"],
                "is_target": r["id"] == target_id,
                "source": r["source"],
                "tool": r["tool_name"],
                "agent": r["agent"],
                "summary": self._truncate_summary(r["summary"], 200),
                "timestamp": r["timestamp"],
            }
            for r in rows
        ]

    # ── L3: Full details ────────────────────────────────────────────────────

    def get_details(self, observation_ids: list) -> list:
        """
        L3: Full observation details. Only fetch what you actually need.
        Token cost: variable (full text).
        """
        if not observation_ids:
            return []

        placeholders = ",".join("?" for _ in observation_ids)
        rows = self.obs_db.execute(
            f"SELECT * FROM observations WHERE id IN ({placeholders})",
            observation_ids,
        ).fetchall()

        return [
            {
                "id": r["id"],
                "session_id": r["session_id"],
                "timestamp": r["timestamp"],
                "source": r["source"],
                "tool_name": r["tool_name"],
                "agent": r["agent"],
                "raw_input": r["raw_input"],
                "raw_output": r["raw_output"],
                "summary": r["summary"],
                "status": r["status"],
                "vector_synced": bool(r["vector_synced"]),
            }
            for r in rows
        ]

    # ── Convenience methods ─────────────────────────────────────────────────

    def recent_observations(
        self,
        limit: int = 20,
        hours: int = 48,
    ) -> list:
        """Get recent observations as compact index."""
        rows = self.obs_db.execute(
            "SELECT id, session_id, timestamp, source, tool_name, agent, summary "
            "FROM observations "
            "WHERE status = 'processed' "
            f"AND timestamp > datetime('now', '-{hours} hours') "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()

        return [
            {
                "id": r["id"],
                "summary": self._truncate_summary(r["summary"]),
                "source": r["source"],
                "tool": r["tool_name"],
                "agent": r["agent"],
                "timestamp": r["timestamp"],
            }
            for r in rows
        ]

    def session_summary(self, session_id: str) -> dict:
        """Get session info with observation count and summary."""
        session = self.obs_db.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()

        if not session:
            return {"error": f"Session {session_id} not found"}

        obs_count = self.obs_db.execute(
            "SELECT COUNT(*) as c FROM observations WHERE session_id = ?",
            (session_id,),
        ).fetchone()["c"]

        tools_used = self.obs_db.execute(
            "SELECT tool_name, COUNT(*) as c FROM observations "
            "WHERE session_id = ? AND tool_name IS NOT NULL "
            "GROUP BY tool_name ORDER BY c DESC",
            (session_id,),
        ).fetchall()

        return {
            "session_id": session["id"],
            "agent": session["agent"],
            "started_at": session["started_at"],
            "ended_at": session["ended_at"],
            "status": session["status"],
            "user_prompt": session["user_prompt"],
            "observation_count": obs_count,
            "tools_used": {r["tool_name"]: r["c"] for r in tools_used},
        }

    def save_memory(self, content: str, metadata: Optional[dict] = None) -> str:
        """Manually save a memory for future retrieval."""
        sys.path.insert(0, str(Path(__file__).parent))
        from unified_vector_store import get_vector_store

        store = get_vector_store(self.vec_db_path)
        mem_id = f"manual-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
        store.add_knowledge(mem_id, content, metadata)
        return f"kg-{mem_id}"

    def close(self):
        """Close database connections."""
        if self._obs_conn:
            self._obs_conn.close()
        if self._vec_conn:
            self._vec_conn.close()


# ── CLI for testing ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    retriever = MemoryRetriever()

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python memory_retriever.py search <query>")
        print("  python memory_retriever.py timeline <obs_id>")
        print("  python memory_retriever.py details <obs_id> [obs_id2 ...]")
        print("  python memory_retriever.py recent [hours]")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "search":
        query = " ".join(sys.argv[2:])
        results = retriever.search(query)
        print(f"L1 Search: '{query}' -- {len(results)} results")
        for r in results:
            print(f"  [{r['id']}] {r.get('tool', r.get('collection', '?'))}: {r['summary']}")

    elif cmd == "timeline":
        obs_id = int(sys.argv[2])
        context = retriever.timeline(obs_id)
        print(f"L2 Timeline around obs #{obs_id} -- {len(context)} items")
        for r in context:
            marker = ">>>" if r.get("is_target") else "   "
            print(f"  {marker} [{r['id']}] {r.get('tool', '?')}: {r['summary']}")

    elif cmd == "details":
        obs_ids = [int(x) for x in sys.argv[2:]]
        details = retriever.get_details(obs_ids)
        print(f"L3 Details for {len(details)} observations")
        for d in details:
            print(f"\n--- Observation #{d['id']} ({d['source']}) ---")
            print(f"Tool: {d['tool_name']}")
            print(f"Input: {(d['raw_input'] or '')[:200]}")
            print(f"Output: {(d['raw_output'] or '')[:200]}")
            print(f"Summary: {d['summary']}")

    elif cmd == "recent":
        hours = int(sys.argv[2]) if len(sys.argv) > 2 else 48
        results = retriever.recent_observations(hours=hours)
        print(f"Recent observations (last {hours}h): {len(results)}")
        for r in results:
            print(f"  [{r['id']}] {r.get('tool', '?')}: {r['summary']}")

    retriever.close()
