"""
Cortex Telemetry Harvester — Reads tool-call events directly from
cortex-observations.db and normalizes them for AIR pattern compilation.

Unlike the standalone AIR harvester that stores events in its own DB,
this adapter reads from the existing cortex observation pipeline. No
data duplication — cortex is the single source of truth for telemetry.

Provides session-oriented query methods consumed by PatternCompiler.

Author: Cameron Bennion (Magnum Opus Capital / Young Money Investments)
License: Proprietary — All rights reserved
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

from src.air.config import AIRConfig

logger = logging.getLogger("cortex-air")

# Substrings in tool output that signal a failed tool dispatch.
_ERROR_SIGNALS = [
    "error:",
    "unknown skill",
    "not found",
    "failed",
    "traceback",
    "exception",
    "permission denied",
    "command not found",
    "enoent",
    "timed out",
]


class CortexHarvester:
    """Read-only adapter over cortex-observations.db for AIR telemetry.

    Reads tool-call observations (source='post_tool_use') from the cortex
    database and normalizes them into the event format expected by
    PatternCompiler: session_id, tool_name, tool_input, tool_output_summary,
    success, timestamp, sequence_num.

    Parameters
    ----------
    config : AIRConfig
        Provides cortex_db_path for the observation database.
    cortex_db_path : Path, optional
        Override the cortex DB path (useful for testing).
    """

    def __init__(
        self,
        config: Optional[AIRConfig] = None,
        cortex_db_path: Optional[Path] = None,
    ) -> None:
        self._config = config or AIRConfig.from_env()
        self._db_path = cortex_db_path or self._config.cortex_db_path

        if not self._db_path.exists():
            logger.warning(
                "Cortex DB not found at %s — harvester will return empty results",
                self._db_path,
            )

    def _connect(self) -> sqlite3.Connection:
        """Open a read-only connection to cortex-observations.db."""
        uri = f"file:{self._db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    # Public API — event queries (used by PatternCompiler)
    # ------------------------------------------------------------------

    def get_events_by_session(self, session_id: str) -> list[dict[str, Any]]:
        """Retrieve all tool-call events for a session, normalized for AIR.

        Returns events ordered by timestamp (sequence_num assigned in order).
        Only includes observations where source='post_tool_use'.
        """
        if not self._db_path.exists():
            return []

        sql = """
            SELECT id, session_id, timestamp, tool_name, agent,
                   raw_input, raw_output, summary, source
            FROM observations
            WHERE session_id = ? AND source = 'post_tool_use'
            ORDER BY timestamp ASC, id ASC
        """
        with self._connect() as conn:
            rows = conn.execute(sql, (session_id,)).fetchall()

        return [
            self._normalize_row(dict(row), seq_num=i)
            for i, row in enumerate(rows)
        ]

    def get_recent_events(self, hours: int = 48) -> list[dict[str, Any]]:
        """Return tool-call events from the last N hours, normalized.

        Events are ordered by timestamp descending (newest first).
        """
        if not self._db_path.exists():
            return []

        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=hours)
        ).isoformat()

        sql = """
            SELECT id, session_id, timestamp, tool_name, agent,
                   raw_input, raw_output, summary, source
            FROM observations
            WHERE source = 'post_tool_use' AND timestamp >= ?
            ORDER BY timestamp DESC
        """
        with self._connect() as conn:
            rows = conn.execute(sql, (cutoff,)).fetchall()

        return [self._normalize_row(dict(row), seq_num=0) for row in rows]

    def get_recent_session_ids(self, hours: int = 48) -> list[str]:
        """Return distinct session IDs with tool events in the last N hours.

        Ordered by most recent event first.
        """
        if not self._db_path.exists():
            return []

        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=hours)
        ).isoformat()

        sql = """
            SELECT session_id
            FROM observations
            WHERE source = 'post_tool_use' AND timestamp >= ?
            GROUP BY session_id
            ORDER BY MAX(timestamp) DESC
        """
        with self._connect() as conn:
            rows = conn.execute(sql, (cutoff,)).fetchall()
            return [r["session_id"] for r in rows]

    def get_session_count(self, hours: int = 48) -> int:
        """Count distinct sessions with tool events in the last N hours."""
        return len(self.get_recent_session_ids(hours=hours))

    def get_event_count(self) -> int:
        """Total number of tool-call observations in cortex."""
        if not self._db_path.exists():
            return 0

        sql = """
            SELECT COUNT(*) as cnt
            FROM observations
            WHERE source = 'post_tool_use'
        """
        with self._connect() as conn:
            row = conn.execute(sql).fetchone()
            return row["cnt"]

    # ------------------------------------------------------------------
    # Normalization — cortex schema → AIR event format
    # ------------------------------------------------------------------

    def _normalize_row(
        self, row: dict[str, Any], seq_num: int = 0
    ) -> dict[str, Any]:
        """Convert a cortex observation row into the AIR event format.

        Cortex schema:
            id, session_id, timestamp, source, tool_name, agent,
            raw_input, raw_output, summary, status, ...

        AIR event format:
            session_id, tool_name, tool_input, tool_output_summary,
            success, timestamp, sequence_num, project_id, source, agent
        """
        raw_output = str(row.get("raw_output") or "")
        summary = str(row.get("summary") or "")
        raw_input = row.get("raw_input") or ""

        # Prefer raw_output for error detection; fall back to summary
        # (cortex truncates raw_output for most events, but summary is
        # always populated by the compression worker).
        output_for_detection = raw_output if raw_output else summary

        return {
            "session_id": row.get("session_id", "unknown"),
            "tool_name": row.get("tool_name", "unknown"),
            "tool_input": raw_input,
            "tool_output_summary": self._summarize_output(
                raw_output if raw_output else summary
            ),
            "success": 0 if self._looks_like_error(output_for_detection) else 1,
            "timestamp": row.get("timestamp", ""),
            "sequence_num": seq_num,
            "project_id": None,  # cortex doesn't track project scope
            "source": row.get("source", "post_tool_use"),
            "agent": row.get("agent", "main"),
        }

    @staticmethod
    def _summarize_output(raw_output: str, max_len: int = 200) -> str:
        """Truncate tool output for compact storage in patterns."""
        if not raw_output:
            return ""
        text = raw_output.strip()
        if len(text) <= max_len:
            return text
        return text[:max_len] + "..."

    @staticmethod
    def _looks_like_error(output: str) -> bool:
        """Heuristic check for error indicators in tool output."""
        if not output:
            return False
        check = output[:500].lower()
        return any(signal in check for signal in _ERROR_SIGNALS)
