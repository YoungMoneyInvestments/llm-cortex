"""
Telemetry Harvester — Ingests raw tool-call events from Cortex hooks
and structures them for pattern compilation.

Sits between post_tool_use hooks and RoutingStorage. Normalizes raw
event dicts, assigns sequence numbers, handles missing fields, and
provides query methods for downstream consumers (PatternCompiler).

Author: Cameron Bennion (Magnum Opus Capital / Young Money Investments)
License: Proprietary — All rights reserved
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from src.air.config import AIRConfig
from src.air.storage import RoutingStorage

logger = logging.getLogger("cortex-air")


class TelemetryHarvester:
    """Adapter layer between raw hook events and structured AIR storage.

    Accepts raw event dicts from post_tool_use hooks (or batch imports),
    normalizes them into the storage schema, and provides session-oriented
    query methods for the PatternCompiler.
    """

    def __init__(self, storage: RoutingStorage, config: AIRConfig | None = None) -> None:
        self.storage = storage
        self.config = config or AIRConfig.from_env()
        # Track per-session sequence counters for ordering events within a session.
        # Key: session_id, Value: next sequence number to assign.
        self._session_seq: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ingest_event(self, event: dict[str, Any]) -> int:
        """Ingest a single raw event from a post_tool_use hook.

        Expected fields in *event*:
            session_id   (str)  — Claude Code session identifier
            tool_name    (str)  — Name of the tool that was invoked
            tool_input   (dict | str) — Arguments passed to the tool
            success      (bool) — Whether the tool call succeeded
            timestamp    (str, ISO 8601) — When the call occurred
            project_id   (str, optional) — Project scope for the event
            latency_ms   (int, optional) — Round-trip execution time

        Additional fields from the Cortex hook format are also accepted:
            raw_input, raw_output, source, agent

        Returns the storage-assigned event ID.
        """
        normalized = self._normalize_event(event)
        event_id: int = self.storage.store_event(normalized)
        logger.debug(
            "Ingested event %d: session=%s tool=%s seq=%d",
            event_id,
            normalized.get("session_id", "?"),
            normalized.get("tool_name", "?"),
            normalized.get("sequence_num", -1),
        )
        return event_id

    def ingest_batch(self, events: list[dict[str, Any]]) -> list[int]:
        """Ingest multiple raw events in order.

        Events should be pre-sorted chronologically when possible.
        Sequence numbers are assigned in list order per session.

        Returns a list of stored event IDs (same order as input).
        """
        if not events:
            return []

        ids: list[int] = []
        for event in events:
            ids.append(self.ingest_event(event))

        logger.info("Batch ingested %d events across sessions", len(ids))
        return ids

    def get_session_events(self, session_id: str) -> list[dict[str, Any]]:
        """Retrieve all events for a session, ordered by sequence_num.

        Returns a list of normalized event dicts as stored.
        """
        return self.storage.get_events_by_session(session_id)

    def get_recent_sessions(self, hours: int = 48) -> list[str]:
        """Get unique session IDs that have events in the last N hours.

        Returns session IDs ordered by most recent event first.
        """
        return self.storage.get_recent_session_ids(hours=hours)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _normalize_event(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Normalize a raw hook event dict into the storage schema.

        Handles both the AIR-native format (session_id, tool_name,
        tool_input, success, timestamp) and the Cortex hook format
        (session_id, tool_name, raw_input, raw_output, source, agent).

        Missing fields are filled with sensible defaults. A per-session
        sequence number is auto-assigned for ordering.
        """
        session_id = str(raw.get("session_id") or "unknown")
        tool_name = str(raw.get("tool_name") or "unknown")

        # Resolve tool input — accept both formats
        tool_input = raw.get("tool_input") or raw.get("raw_input") or ""
        if isinstance(tool_input, dict):
            # Keep structured input as-is for storage
            pass
        else:
            tool_input = str(tool_input)

        # Resolve success flag — derive from raw_output if not explicit
        if "success" in raw:
            success = bool(raw["success"])
        else:
            raw_output = str(raw.get("raw_output") or raw.get("tool_output") or "")
            success = not self._looks_like_error(raw_output)

        # Resolve timestamp
        timestamp = raw.get("timestamp")
        if not timestamp:
            timestamp = datetime.now(timezone.utc).isoformat()
        else:
            # Validate it parses; fall back to now if malformed
            try:
                datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
                timestamp = str(timestamp)
            except (ValueError, TypeError):
                logger.warning("Malformed timestamp '%s', using current time", timestamp)
                timestamp = datetime.now(timezone.utc).isoformat()

        # Assign sequence number within the session.
        # Query storage for the current max to handle fresh process instances
        # (each CLI invocation creates a new harvester, so in-memory state
        # would always reset to 0).
        if session_id not in self._session_seq:
            self._session_seq[session_id] = self.storage.get_max_sequence_num(session_id) + 1
        seq = self._session_seq[session_id]
        self._session_seq[session_id] = seq + 1

        # Summarize output for storage (avoid storing megabytes of tool output)
        raw_output_str = str(
            raw.get("raw_output") or raw.get("tool_output") or ""
        )
        output_summary = self._summarize_output(raw_output_str)

        return {
            "session_id": session_id,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "tool_output_summary": output_summary,
            "success": success,
            "timestamp": timestamp,
            "sequence_num": seq,
            "project_id": raw.get("project_id"),
            "latency_ms": raw.get("latency_ms"),
            "source": raw.get("source", "post_tool_use"),
            "agent": raw.get("agent", "main"),
        }

    def _summarize_output(self, raw_output: str, max_len: int = 200) -> str:
        """Truncate tool output for compact storage.

        Preserves the beginning of the output (most likely to contain
        error messages or meaningful result summaries) and appends an
        ellipsis indicator when truncated.
        """
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
        error_signals = [
            "error:",
            "unknown skill",
            "not found",
            "failed",
            "traceback",
            "exception",
            "permission denied",
            "command not found",
        ]
        return any(signal in check for signal in error_signals)
