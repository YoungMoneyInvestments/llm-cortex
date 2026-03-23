#!/usr/bin/env python3
"""
AIR Routing Storage — SQLite adapter for routing rules and tool event telemetry.

Manages a dedicated database (air-routing.db) in the Cortex data directory,
storing compiled routing rules and raw tool-call events that feed the pattern
compiler and confidence scorer.

Tables:
  routing_rules  — Compiled trigger → route mappings with confidence scores.
  tool_events    — Raw per-tool-call telemetry for pattern mining.
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from uuid import uuid4

from src.air.config import AIRConfig

logger = logging.getLogger("cortex-air")

# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS routing_rules (
    id TEXT PRIMARY KEY,
    trigger_pattern TEXT NOT NULL,
    trigger_hash TEXT NOT NULL,
    optimal_route TEXT NOT NULL,
    failed_routes TEXT,
    confidence REAL DEFAULT 0.5,
    hit_count INTEGER DEFAULT 0,
    miss_count INTEGER DEFAULT 0,
    last_used TEXT,
    created_at TEXT NOT NULL,
    project_id TEXT,
    classifier_source TEXT,
    metadata TEXT
);

CREATE INDEX IF NOT EXISTS idx_routing_trigger ON routing_rules(trigger_hash);
CREATE INDEX IF NOT EXISTS idx_routing_confidence ON routing_rules(confidence DESC);
CREATE INDEX IF NOT EXISTS idx_routing_project ON routing_rules(project_id);

CREATE TABLE IF NOT EXISTS tool_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    tool_input TEXT,
    tool_output_summary TEXT,
    success INTEGER DEFAULT 1,
    latency_ms INTEGER,
    sequence_num INTEGER,
    project_id TEXT,
    metadata TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_session ON tool_events(session_id);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON tool_events(timestamp);
"""


def _generate_id() -> str:
    """Generate an AIR-prefixed short ID."""
    return f"air-{uuid4().hex[:12]}"


def _now_iso() -> str:
    """Return current UTC time as ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


class RoutingStorage:
    """SQLite storage adapter for AIR routing rules and tool events."""

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            config = AIRConfig.from_env()
            db_path = config.db_path
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """Open a connection with Row factory and WAL mode."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _ensure_schema(self) -> None:
        """Create tables and indexes if they don't exist."""
        with self._connect() as conn:
            conn.executescript(_SCHEMA_SQL)
            logger.debug("AIR schema ensured at %s", self.db_path)

    @staticmethod
    def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
        """Convert a sqlite3.Row to a plain dict, or None."""
        if row is None:
            return None
        return dict(row)

    @staticmethod
    def _rows_to_dicts(rows: list) -> list[dict]:
        """Convert a list of sqlite3.Row objects to plain dicts."""
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Tool events
    # ------------------------------------------------------------------

    def add_event(self, event: dict) -> int:
        """
        Insert a tool event record.

        Required keys: session_id, tool_name.
        Optional: timestamp, tool_input, tool_output_summary, success,
                  latency_ms, sequence_num, project_id, metadata.

        Returns the row id of the inserted event.
        """
        event.setdefault("timestamp", _now_iso())
        event.setdefault("success", 1)

        # Serialize JSON fields
        for json_key in ("tool_input", "metadata"):
            val = event.get(json_key)
            if val is not None and not isinstance(val, str):
                event[json_key] = json.dumps(val)

        sql = """
            INSERT INTO tool_events
                (session_id, timestamp, tool_name, tool_input,
                 tool_output_summary, success, latency_ms, sequence_num,
                 project_id, metadata)
            VALUES
                (:session_id, :timestamp, :tool_name, :tool_input,
                 :tool_output_summary, :success, :latency_ms, :sequence_num,
                 :project_id, :metadata)
        """
        params = {
            "session_id": event["session_id"],
            "timestamp": event["timestamp"],
            "tool_name": event["tool_name"],
            "tool_input": event.get("tool_input"),
            "tool_output_summary": event.get("tool_output_summary"),
            "success": event.get("success", 1),
            "latency_ms": event.get("latency_ms"),
            "sequence_num": event.get("sequence_num"),
            "project_id": event.get("project_id"),
            "metadata": event.get("metadata"),
        }
        with self._connect() as conn:
            cursor = conn.execute(sql, params)
            row_id = cursor.lastrowid
            logger.debug("Inserted tool event %d for session %s", row_id, event["session_id"])
            return row_id

    def get_max_sequence_num(self, session_id: str) -> int:
        """Return the highest sequence_num for a session, or -1 if none."""
        sql = """
            SELECT COALESCE(MAX(sequence_num), -1) AS max_seq
            FROM tool_events WHERE session_id = ?
        """
        with self._connect() as conn:
            row = conn.execute(sql, (session_id,)).fetchone()
            return row["max_seq"]

    def get_events_by_session(self, session_id: str) -> list[dict]:
        """Return all events for a session, ordered by sequence_num."""
        sql = """
            SELECT * FROM tool_events
            WHERE session_id = ?
            ORDER BY sequence_num ASC, id ASC
        """
        with self._connect() as conn:
            rows = conn.execute(sql, (session_id,)).fetchall()
            return self._rows_to_dicts(rows)

    def get_recent_events(self, hours: int = 48) -> list[dict]:
        """Return events from the last N hours, ordered by timestamp."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        sql = """
            SELECT * FROM tool_events
            WHERE timestamp >= ?
            ORDER BY timestamp DESC
        """
        with self._connect() as conn:
            rows = conn.execute(sql, (cutoff,)).fetchall()
            return self._rows_to_dicts(rows)

    # ------------------------------------------------------------------
    # Routing rules
    # ------------------------------------------------------------------

    def add_rule(self, rule: dict) -> str:
        """
        Insert a routing rule.

        Required keys: trigger_pattern, trigger_hash, optimal_route.
        Optional: failed_routes, confidence, hit_count, miss_count,
                  last_used, project_id, classifier_source, metadata.

        Returns the generated rule ID.
        """
        rule_id = _generate_id()
        now = _now_iso()

        # Serialize JSON fields
        for json_key in ("failed_routes", "metadata"):
            val = rule.get(json_key)
            if val is not None and not isinstance(val, str):
                rule[json_key] = json.dumps(val)

        sql = """
            INSERT INTO routing_rules
                (id, trigger_pattern, trigger_hash, optimal_route,
                 failed_routes, confidence, hit_count, miss_count,
                 last_used, created_at, project_id, classifier_source, metadata)
            VALUES
                (:id, :trigger_pattern, :trigger_hash, :optimal_route,
                 :failed_routes, :confidence, :hit_count, :miss_count,
                 :last_used, :created_at, :project_id, :classifier_source, :metadata)
        """
        params = {
            "id": rule_id,
            "trigger_pattern": rule["trigger_pattern"],
            "trigger_hash": rule["trigger_hash"],
            "optimal_route": rule["optimal_route"],
            "failed_routes": rule.get("failed_routes"),
            "confidence": rule.get("confidence", 0.5),
            "hit_count": rule.get("hit_count", 0),
            "miss_count": rule.get("miss_count", 0),
            "last_used": rule.get("last_used"),
            "created_at": now,
            "project_id": rule.get("project_id"),
            "classifier_source": rule.get("classifier_source"),
            "metadata": rule.get("metadata"),
        }
        with self._connect() as conn:
            conn.execute(sql, params)
            logger.debug("Inserted routing rule %s (trigger_hash=%s)", rule_id, rule["trigger_hash"])
            return rule_id

    def get_rule(self, rule_id: str) -> Optional[dict]:
        """Retrieve a single rule by ID."""
        sql = "SELECT * FROM routing_rules WHERE id = ?"
        with self._connect() as conn:
            row = conn.execute(sql, (rule_id,)).fetchone()
            return self._row_to_dict(row)

    def get_rule_by_trigger(
        self, trigger_hash: str, project_id: Optional[str] = None
    ) -> Optional[dict]:
        """
        Find the best rule matching a trigger hash.

        If project_id is given, prefer project-scoped rules; fall back to
        global rules (project_id IS NULL).  Returns the highest-confidence
        match.
        """
        if project_id is not None:
            # Try project-scoped first
            sql = """
                SELECT * FROM routing_rules
                WHERE trigger_hash = ? AND project_id = ?
                ORDER BY confidence DESC
                LIMIT 1
            """
            with self._connect() as conn:
                row = conn.execute(sql, (trigger_hash, project_id)).fetchone()
                if row is not None:
                    return self._row_to_dict(row)

        # Fall back to global (no project scope)
        sql = """
            SELECT * FROM routing_rules
            WHERE trigger_hash = ? AND project_id IS NULL
            ORDER BY confidence DESC
            LIMIT 1
        """
        with self._connect() as conn:
            row = conn.execute(sql, (trigger_hash,)).fetchone()
            return self._row_to_dict(row)

    def update_rule(self, rule_id: str, updates: dict) -> bool:
        """
        Update specific fields on a routing rule.

        Accepts a dict of column → value pairs. Only known columns are
        applied; unknown keys are silently ignored. Returns True if a row
        was updated.
        """
        allowed = {
            "trigger_pattern", "trigger_hash", "optimal_route",
            "failed_routes", "confidence", "hit_count", "miss_count",
            "last_used", "project_id", "classifier_source", "metadata",
        }
        filtered = {k: v for k, v in updates.items() if k in allowed}
        if not filtered:
            return False

        # Serialize JSON fields
        for json_key in ("failed_routes", "metadata"):
            val = filtered.get(json_key)
            if val is not None and not isinstance(val, str):
                filtered[json_key] = json.dumps(val)

        # SAFETY: column names are drawn exclusively from the `allowed` set
        # above (hardcoded, not user-controlled). Values use parameterized
        # bindings (:name) — no SQL injection risk.
        set_clause = ", ".join(f"{col} = :{col}" for col in filtered)
        sql = f"UPDATE routing_rules SET {set_clause} WHERE id = :_rule_id"
        filtered["_rule_id"] = rule_id

        with self._connect() as conn:
            cursor = conn.execute(sql, filtered)  # nosemgrep: sqlalchemy-execute-raw-query
            updated = cursor.rowcount > 0
            if updated:
                logger.debug("Updated rule %s: %s", rule_id, list(filtered.keys()))
            return updated

    def delete_rule(self, rule_id: str) -> bool:
        """Delete a routing rule by ID. Returns True if deleted."""
        sql = "DELETE FROM routing_rules WHERE id = ?"
        with self._connect() as conn:
            cursor = conn.execute(sql, (rule_id,))
            deleted = cursor.rowcount > 0
            if deleted:
                logger.debug("Deleted rule %s", rule_id)
            return deleted

    def get_injectable_rules(
        self,
        threshold: float = 0.5,
        project_id: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        """
        Return rules suitable for injection into system prompts.

        Filters by minimum confidence threshold.  If project_id is given,
        returns both project-scoped and global rules (project-scoped first).
        """
        if project_id is not None:
            sql = """
                SELECT * FROM routing_rules
                WHERE confidence >= ?
                  AND (project_id = ? OR project_id IS NULL)
                ORDER BY
                    CASE WHEN project_id IS NOT NULL THEN 0 ELSE 1 END,
                    confidence DESC
                LIMIT ?
            """
            params = (threshold, project_id, limit)
        else:
            sql = """
                SELECT * FROM routing_rules
                WHERE confidence >= ?
                  AND project_id IS NULL
                ORDER BY confidence DESC
                LIMIT ?
            """
            params = (threshold, limit)

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return self._rows_to_dicts(rows)

    def prune_rules(self, threshold: float = 0.2) -> int:
        """
        Delete routing rules with confidence below *threshold*.

        Returns the number of rules deleted.
        """
        sql = "DELETE FROM routing_rules WHERE confidence < ?"
        with self._connect() as conn:
            cursor = conn.execute(sql, (threshold,))
            count = cursor.rowcount
            if count:
                logger.info("Pruned %d low-confidence routing rules (threshold=%.2f)", count, threshold)
            return count

    # ------------------------------------------------------------------
    # Aliases and convenience methods for cross-module compatibility
    # ------------------------------------------------------------------

    def store_event(self, event: dict) -> int:
        """Alias for :meth:`add_event` (used by harvester)."""
        return self.add_event(event)

    def get_recent_session_ids(self, hours: int = 48) -> list[str]:
        """Return distinct session IDs with events in the last *hours*."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        sql = """
            SELECT DISTINCT session_id FROM tool_events
            WHERE timestamp >= ?
            ORDER BY MAX(timestamp) DESC
        """
        # GROUP BY needed for ORDER BY to work correctly on aggregates
        sql = """
            SELECT session_id FROM tool_events
            WHERE timestamp >= ?
            GROUP BY session_id
            ORDER BY MAX(timestamp) DESC
        """
        with self._connect() as conn:
            rows = conn.execute(sql, (cutoff,)).fetchall()
            return [r["session_id"] for r in rows]

    def record_hit(self, rule_id: str) -> bool:
        """Increment hit_count and update last_used for a rule."""
        sql = """
            UPDATE routing_rules
            SET hit_count = hit_count + 1, last_used = ?
            WHERE id = ?
        """
        with self._connect() as conn:
            cursor = conn.execute(sql, (_now_iso(), rule_id))
            return cursor.rowcount > 0

    def update_confidence(self, rule_id: str, new_confidence: float) -> bool:
        """Update just the confidence field for a rule."""
        return self.update_rule(rule_id, {"confidence": new_confidence})

    def get_routes_by_confidence(
        self,
        project_id: Optional[str] = None,
        min_confidence: float = 0.5,
        limit: int = 50,
    ) -> list[dict]:
        """Return rules above *min_confidence*, ordered by confidence desc."""
        return self.get_injectable_rules(
            threshold=min_confidence, project_id=project_id, limit=limit
        )

    def lookup_by_hash(
        self, trigger_hash: str, project_id: Optional[str] = None
    ) -> list[dict]:
        """Return all rules matching *trigger_hash*, optionally scoped to project."""
        if project_id is not None:
            sql = """
                SELECT * FROM routing_rules
                WHERE trigger_hash = ? AND project_id = ?
                ORDER BY confidence DESC
            """
            params = (trigger_hash, project_id)
        else:
            sql = """
                SELECT * FROM routing_rules
                WHERE trigger_hash = ? AND project_id IS NULL
                ORDER BY confidence DESC
            """
            params = (trigger_hash,)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return self._rows_to_dicts(rows)

    def list_active_rules(self, project_id: Optional[str] = None) -> list[dict]:
        """Return all rules above prune threshold, optionally scoped to project."""
        config = AIRConfig.from_env()
        if project_id is not None:
            sql = """
                SELECT * FROM routing_rules
                WHERE confidence >= ? AND (project_id = ? OR project_id IS NULL)
                ORDER BY confidence DESC
            """
            params = (config.prune_threshold, project_id)
        else:
            sql = """
                SELECT * FROM routing_rules
                WHERE confidence >= ?
                ORDER BY confidence DESC
            """
            params = (config.prune_threshold,)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return self._rows_to_dicts(rows)

    def get_all_rules(self) -> list[dict]:
        """Return all routing rules (used by scorer decay batch)."""
        sql = "SELECT * FROM routing_rules ORDER BY confidence DESC"
        with self._connect() as conn:
            rows = conn.execute(sql).fetchall()
            return self._rows_to_dicts(rows)

    def get_stats(self) -> dict:
        """
        Return aggregate statistics across rules and events.

        Keys: rule_count, event_count, avg_confidence, max_confidence,
              min_confidence, total_hits, total_misses, unique_sessions.
        """
        with self._connect() as conn:
            rule_row = conn.execute("""
                SELECT
                    COUNT(*) AS rule_count,
                    COALESCE(AVG(confidence), 0) AS avg_confidence,
                    COALESCE(MAX(confidence), 0) AS max_confidence,
                    COALESCE(MIN(confidence), 0) AS min_confidence,
                    COALESCE(SUM(hit_count), 0) AS total_hits,
                    COALESCE(SUM(miss_count), 0) AS total_misses
                FROM routing_rules
            """).fetchone()

            event_row = conn.execute("""
                SELECT
                    COUNT(*) AS event_count,
                    COUNT(DISTINCT session_id) AS unique_sessions
                FROM tool_events
            """).fetchone()

            return {
                "rule_count": rule_row["rule_count"],
                "avg_confidence": round(rule_row["avg_confidence"], 4),
                "max_confidence": round(rule_row["max_confidence"], 4),
                "min_confidence": round(rule_row["min_confidence"], 4),
                "total_hits": rule_row["total_hits"],
                "total_misses": rule_row["total_misses"],
                "event_count": event_row["event_count"],
                "unique_sessions": event_row["unique_sessions"],
            }
