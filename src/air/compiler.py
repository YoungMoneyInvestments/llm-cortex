"""
Pattern Compiler for Adaptive Inference Routing (AIR).

Analyses tool-call event sequences to detect miss-then-recover patterns and
compiles them into routing rules. A "miss->recover" pattern occurs when:

    1. The user asks for something.
    2. The system tries tool A (fails or produces wrong result).
    3. The system tries tool B (succeeds).
    4. This same pattern repeats across sessions.

Adapted for llm-cortex: reads events from CortexHarvester (cortex-observations.db)
instead of RoutingStorage's tool_events table.

Author: Cameron Bennion (Magnum Opus Capital / Young Money Investments)
License: Proprietary — All rights reserved
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from src.air.config import AIRConfig
    from src.air.classifier import IntentClassifier
    from src.air.harvester import CortexHarvester
    from src.air.scorer import ConfidenceScorer
    from src.air.storage import RoutingStorage

logger = logging.getLogger("cortex-air")

FAILURE_INDICATORS: list[str] = [
    "Unknown skill",
    "Error:",
    "not found",
    "failed",
    "No such file",
    "command not found",
    "ENOENT",
    "permission denied",
    "timed out",
]

_CYCLE_GAP_SECONDS: float = 30.0


class PatternCompiler:
    """Analyse event streams and compile miss->recover patterns into routing rules.

    Parameters
    ----------
    harvester : CortexHarvester
        Reads tool-call events from cortex-observations.db.
    storage : RoutingStorage
        Persistent store for routing rules (air_routes.db).
    classifier : IntentClassifier
        Classifies user intent from a message and tool sequence.
    config : AIRConfig
        Framework-wide configuration (confidence thresholds, etc.).
    scorer : ConfidenceScorer | None
        Optional confidence scorer. Instantiated lazily if omitted.
    """

    def __init__(
        self,
        harvester: "CortexHarvester",
        storage: "RoutingStorage",
        classifier: "IntentClassifier",
        config: "AIRConfig",
        scorer: Optional["ConfidenceScorer"] = None,
    ) -> None:
        self._harvester = harvester
        self._storage = storage
        self._classifier = classifier
        self._config = config

        if scorer is not None:
            self._scorer = scorer
        else:
            from src.air.scorer import ConfidenceScorer
            self._scorer = ConfidenceScorer(config)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compile_session(self, session_id: str) -> list[dict]:
        """Analyse a single session's events and return new/updated rules.

        1. Fetch events from cortex via harvester.
        2. Group into cycles (sequences between gaps).
        3. Detect miss->recover patterns per cycle.
        4. Classify intent and create/update routing rules.
        """
        events = self._harvester.get_events_by_session(session_id)
        if not events:
            logger.debug("No events found for session %s", session_id)
            return []

        cycles = self._group_into_cycles(events)
        rules: list[dict] = []

        for cycle in cycles:
            if not self._is_compilable(cycle):
                continue

            pattern = self._detect_miss_recover(cycle)
            if pattern is None:
                continue

            tool_sequence = [
                {
                    "tool": ev.get("tool_name", ""),
                    "args": ev.get("tool_input", ""),
                    "result": ev.get("tool_output_summary", ""),
                    "error": "" if ev.get("success", 1) else ev.get("tool_output_summary", ""),
                }
                for ev in cycle
            ]

            user_message = self._extract_user_message(cycle)

            try:
                intent = self._classifier.classify(user_message, tool_sequence)
            except Exception:
                logger.warning(
                    "Classification failed for session %s cycle; skipping",
                    session_id,
                    exc_info=True,
                )
                continue

            if not intent or not intent.get("trigger_hash"):
                continue

            project_id = self._extract_project_id(cycle)
            rule = self._create_or_update_rule(intent, pattern, project_id)
            if rule is not None:
                rules.append(rule)
                logger.info(
                    "Compiled rule %s (trigger=%s, confidence=%.2f) from session %s",
                    rule.get("id", "?"),
                    intent.get("trigger_pattern", "?"),
                    rule.get("confidence", 0.0),
                    session_id,
                )

        logger.info(
            "Compiled %d rule(s) from session %s (%d events, %d cycles)",
            len(rules), session_id, len(events), len(cycles),
        )
        return rules

    def compile_recent(self, hours: int = 48) -> list[dict]:
        """Compile patterns from all sessions with events in the last N hours."""
        session_ids = self._harvester.get_recent_session_ids(hours=hours)
        if not session_ids:
            logger.info("No recent sessions in the last %d hours", hours)
            return []

        logger.info(
            "Compiling patterns from %d session(s) in last %d hours",
            len(session_ids), hours,
        )

        all_rules: list[dict] = []
        for sid in session_ids:
            try:
                rules = self.compile_session(sid)
                all_rules.extend(rules)
            except Exception:
                logger.error(
                    "Failed to compile session %s; continuing",
                    sid, exc_info=True,
                )

        logger.info(
            "compile_recent complete: %d rule(s) from %d session(s)",
            len(all_rules), len(session_ids),
        )
        return all_rules

    # ------------------------------------------------------------------
    # Cycle grouping
    # ------------------------------------------------------------------

    def _group_into_cycles(self, events: list[dict]) -> list[list[dict]]:
        """Group sequential events into cycles split by time gaps or seq resets."""
        if not events:
            return []

        cycles: list[list[dict]] = []
        current_cycle: list[dict] = [events[0]]

        for prev_ev, ev in zip(events, events[1:]):
            should_split = False

            try:
                ts_prev = datetime.fromisoformat(prev_ev["timestamp"])
                ts_curr = datetime.fromisoformat(ev["timestamp"])
                gap = (ts_curr - ts_prev).total_seconds()
                if gap > _CYCLE_GAP_SECONDS:
                    should_split = True
            except (KeyError, ValueError, TypeError):
                pass

            prev_seq = prev_ev.get("sequence_num")
            curr_seq = ev.get("sequence_num")
            if (
                prev_seq is not None
                and curr_seq is not None
                and prev_seq > 1
                and curr_seq <= 1
            ):
                should_split = True

            if should_split:
                if current_cycle:
                    cycles.append(current_cycle)
                current_cycle = [ev]
            else:
                current_cycle.append(ev)

        if current_cycle:
            cycles.append(current_cycle)

        return cycles

    # ------------------------------------------------------------------
    # Pattern detection
    # ------------------------------------------------------------------

    def _detect_miss_recover(self, cycle: list[dict]) -> Optional[dict]:
        """Scan a cycle for failure(s) followed by a recovery success."""
        failed_tools: list[str] = []
        failure_started = False

        for event in cycle:
            is_failure = self._is_failure(event)

            if is_failure:
                failure_started = True
                failed_tools.append(event.get("tool_name", "unknown"))
            elif failure_started:
                return {
                    "failed_tools": failed_tools,
                    "successful_tool": event.get("tool_name", "unknown"),
                    "successful_input": (
                        event.get("tool_input", "") or ""
                    )[:500],
                    "wasted_calls": len(failed_tools),
                }

        return None

    def _is_failure(self, event: dict) -> bool:
        """Determine whether an event represents a failed tool dispatch."""
        if event.get("success") == 0:
            return True

        output = str(event.get("tool_output_summary", "") or "")
        output_lower = output.lower()
        for indicator in FAILURE_INDICATORS:
            if indicator.lower() in output_lower:
                return True

        return False

    # ------------------------------------------------------------------
    # Rule creation / reinforcement
    # ------------------------------------------------------------------

    def _create_or_update_rule(
        self,
        intent: dict,
        pattern: dict,
        project_id: Optional[str] = None,
    ) -> Optional[dict]:
        """Create a new routing rule or reinforce an existing one."""
        trigger_hash = intent["trigger_hash"]
        existing = self._storage.get_rule_by_trigger(trigger_hash, project_id)

        if existing is not None:
            return self._reinforce_rule(existing, intent, pattern)

        return self._create_new_rule(intent, pattern, project_id)

    def _reinforce_rule(
        self, existing: dict, intent: dict, pattern: dict
    ) -> Optional[dict]:
        """Reinforce an existing rule by bumping hit_count and confidence."""
        rule_id = existing["id"]
        current_confidence = existing.get("confidence", self._config.confidence_init)
        new_confidence = self._scorer.reward(current_confidence)
        hit_count = existing.get("hit_count", 0) + 1

        existing_failed = []
        if existing.get("failed_routes"):
            try:
                existing_failed = json.loads(existing["failed_routes"])
                if not isinstance(existing_failed, list):
                    existing_failed = []
            except (json.JSONDecodeError, TypeError):
                existing_failed = []

        new_failed = pattern.get("failed_tools", [])
        merged_failed = list(dict.fromkeys(existing_failed + new_failed))

        now = datetime.now(timezone.utc).isoformat()

        updates = {
            "confidence": new_confidence,
            "hit_count": hit_count,
            "last_used": now,
            "failed_routes": json.dumps(merged_failed),
        }
        success = self._storage.update_rule(rule_id, updates)

        if success:
            updated = dict(existing)
            updated.update(updates)
            return updated

        return None

    def _create_new_rule(
        self,
        intent: dict,
        pattern: dict,
        project_id: Optional[str] = None,
    ) -> Optional[dict]:
        """Create a brand-new routing rule from a classified pattern."""
        rule = {
            "trigger_pattern": intent.get("trigger_pattern", ""),
            "trigger_hash": intent["trigger_hash"],
            "optimal_route": pattern.get("successful_tool", ""),
            "failed_routes": json.dumps(pattern.get("failed_tools", [])),
            "confidence": self._config.confidence_init,
            "hit_count": 1,
            "miss_count": 0,
            "last_used": datetime.now(timezone.utc).isoformat(),
            "project_id": project_id,
            "classifier_source": intent.get("source", "unknown"),
            "metadata": json.dumps({
                "intent": intent.get("intent", ""),
                "successful_input": pattern.get("successful_input", ""),
                "wasted_calls": pattern.get("wasted_calls", 0),
                "classifier_confidence": intent.get("confidence", 0.0),
            }),
        }

        try:
            rule_id = self._storage.add_rule(rule)
            rule["id"] = rule_id
            return rule
        except Exception:
            logger.error(
                "Failed to create rule for trigger_hash=%s",
                intent["trigger_hash"],
                exc_info=True,
            )
            return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _is_compilable(self, cycle: list[dict]) -> bool:
        """Quick check: cycle needs >= 2 events and at least one failure."""
        if len(cycle) < 2:
            return False
        return any(self._is_failure(ev) for ev in cycle)

    @staticmethod
    def _extract_user_message(cycle: list[dict]) -> str:
        """Best-effort extraction of the user's original message."""
        for ev in cycle:
            meta = ev.get("metadata")
            if meta:
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta)
                    except (json.JSONDecodeError, TypeError):
                        meta = {}
                if isinstance(meta, dict):
                    msg = meta.get("user_message") or meta.get("user_prompt")
                    if msg:
                        return str(msg)

        first_input = cycle[0].get("tool_input", "")
        return str(first_input) if first_input else ""

    @staticmethod
    def _extract_project_id(cycle: list[dict]) -> Optional[str]:
        """Return the project_id from the first event that carries one."""
        for ev in cycle:
            pid = ev.get("project_id")
            if pid:
                return str(pid)
        return None
