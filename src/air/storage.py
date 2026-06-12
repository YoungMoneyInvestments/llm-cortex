"""
AIR Routing Storage — SQLite adapter for routing rules in air_routes.db.

Manages compiled routing rules with confidence scores, hit/miss counts,
and lifecycle management (prune, decay). Tool-call telemetry is read
from cortex-observations.db via CortexHarvester — not duplicated here.

Tables:
  routing_rules  — Compiled trigger -> route mappings with confidence scores.

Author: Cameron Bennion (Magnum Opus Capital / Young Money Investments)
License: Proprietary — All rights reserved
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
"""


def _generate_id() -> str:
    return f"air-{uuid4().hex[:12]}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RoutingStorage:
    """SQLite storage adapter for AIR routing rules."""

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            config = AIRConfig.from_env()
            db_path = config.routes_db_path
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA_SQL)
            logger.debug("AIR schema ensured at %s", self.db_path)

    @staticmethod
    def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
        if row is None:
            return None
        return dict(row)

    @staticmethod
    def _rows_to_dicts(rows: list) -> list[dict]:
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Routing rules
    # ------------------------------------------------------------------

    def add_rule(self, rule: dict) -> str:
        rule_id = _generate_id()
        now = _now_iso()

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
            return rule_id

    def get_rule(self, rule_id: str) -> Optional[dict]:
        sql = "SELECT * FROM routing_rules WHERE id = ?"
        with self._connect() as conn:
            row = conn.execute(sql, (rule_id,)).fetchone()
            return self._row_to_dict(row)

    def get_rule_by_trigger(
        self, trigger_hash: str, project_id: Optional[str] = None
    ) -> Optional[dict]:
        if project_id is not None:
            sql = """
                SELECT * FROM routing_rules
                WHERE trigger_hash = ? AND project_id = ?
                ORDER BY confidence DESC LIMIT 1
            """
            with self._connect() as conn:
                row = conn.execute(sql, (trigger_hash, project_id)).fetchone()
                if row is not None:
                    return self._row_to_dict(row)

        sql = """
            SELECT * FROM routing_rules
            WHERE trigger_hash = ? AND project_id IS NULL
            ORDER BY confidence DESC LIMIT 1
        """
        with self._connect() as conn:
            row = conn.execute(sql, (trigger_hash,)).fetchone()
            return self._row_to_dict(row)

    def update_rule(self, rule_id: str, updates: dict) -> bool:
        allowed = {
            "trigger_pattern", "trigger_hash", "optimal_route",
            "failed_routes", "confidence", "hit_count", "miss_count",
            "last_used", "project_id", "classifier_source", "metadata",
        }
        filtered = {k: v for k, v in updates.items() if k in allowed}
        if not filtered:
            return False

        for json_key in ("failed_routes", "metadata"):
            val = filtered.get(json_key)
            if val is not None and not isinstance(val, str):
                filtered[json_key] = json.dumps(val)

        # SAFETY: column names from hardcoded `allowed` set only.
        set_clause = ", ".join(f"{col} = :{col}" for col in filtered)
        sql = f"UPDATE routing_rules SET {set_clause} WHERE id = :_rule_id"
        filtered["_rule_id"] = rule_id

        with self._connect() as conn:
            cursor = conn.execute(sql, filtered)
            return cursor.rowcount > 0

    def delete_rule(self, rule_id: str) -> bool:
        sql = "DELETE FROM routing_rules WHERE id = ?"
        with self._connect() as conn:
            cursor = conn.execute(sql, (rule_id,))
            return cursor.rowcount > 0

    def get_injectable_rules(
        self,
        threshold: float = 0.5,
        project_id: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
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
                WHERE confidence >= ? AND project_id IS NULL
                ORDER BY confidence DESC LIMIT ?
            """
            params = (threshold, limit)

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return self._rows_to_dicts(rows)

    def prune_rules(self, threshold: float = 0.2) -> int:
        sql = "DELETE FROM routing_rules WHERE confidence < ?"
        with self._connect() as conn:
            cursor = conn.execute(sql, (threshold,))
            count = cursor.rowcount
            if count:
                logger.info("Pruned %d low-confidence rules (threshold=%.2f)", count, threshold)
            return count

    def record_hit(self, rule_id: str) -> bool:
        sql = """
            UPDATE routing_rules
            SET hit_count = hit_count + 1, last_used = ?
            WHERE id = ?
        """
        with self._connect() as conn:
            cursor = conn.execute(sql, (_now_iso(), rule_id))
            return cursor.rowcount > 0

    def record_miss(self, rule_id: str) -> bool:
        """Increment miss_count for a rule that produced a wrong/failed outcome."""
        sql = """
            UPDATE routing_rules
            SET miss_count = miss_count + 1
            WHERE id = ?
        """
        with self._connect() as conn:
            cursor = conn.execute(sql, (rule_id,))
            return cursor.rowcount > 0

    def update_confidence(self, rule_id: str, new_confidence: float) -> bool:
        return self.update_rule(rule_id, {"confidence": new_confidence})

    def get_routes_by_confidence(
        self,
        project_id: Optional[str] = None,
        min_confidence: float = 0.5,
        limit: int = 50,
    ) -> list[dict]:
        return self.get_injectable_rules(
            threshold=min_confidence, project_id=project_id, limit=limit
        )

    def lookup_by_hash(
        self, trigger_hash: str, project_id: Optional[str] = None
    ) -> list[dict]:
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
        """Return active rules above the prune threshold.

        When ``project_id`` is provided, returns project-specific rules merged
        with global (project_id IS NULL) rules, project-scoped first.

        When ``project_id`` is None, returns only global rules
        (project_id IS NULL). This is intentional: callers without a project
        context should not see rules that only apply to other projects, as
        those rules would never actually be injected for them.
        """
        config = AIRConfig.from_env()
        if project_id is not None:
            sql = """
                SELECT * FROM routing_rules
                WHERE confidence >= ? AND (project_id = ? OR project_id IS NULL)
                ORDER BY
                    CASE WHEN project_id IS NOT NULL THEN 0 ELSE 1 END,
                    confidence DESC
            """
            params = (config.prune_threshold, project_id)
        else:
            sql = """
                SELECT * FROM routing_rules
                WHERE confidence >= ? AND project_id IS NULL
                ORDER BY confidence DESC
            """
            params = (config.prune_threshold,)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return self._rows_to_dicts(rows)

    def get_all_rules(self) -> list[dict]:
        sql = "SELECT * FROM routing_rules ORDER BY confidence DESC"
        with self._connect() as conn:
            rows = conn.execute(sql).fetchall()
            return self._rows_to_dicts(rows)

    def get_stats(self) -> dict:
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

            return {
                "rule_count": rule_row["rule_count"],
                "avg_confidence": round(rule_row["avg_confidence"], 4),
                "max_confidence": round(rule_row["max_confidence"], 4),
                "min_confidence": round(rule_row["min_confidence"], 4),
                "total_hits": rule_row["total_hits"],
                "total_misses": rule_row["total_misses"],
            }
