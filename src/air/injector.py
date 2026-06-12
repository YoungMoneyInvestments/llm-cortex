"""
AIR Route Injector — Formats learned routing rules and injects them into
the LLM's context through CLAUDE.md managed sections and per-message hooks.

Two injection surfaces:
  1. CLAUDE.md managed section — high-confidence rules (>= inject_threshold_high)
     Persisted between sessions, auto-managed between markers.
  2. UserPromptSubmit hook hint — medium-confidence rules (inject_threshold_low .. high)
     Ephemeral, per-message, lower commitment.

Author: Cameron Bennion (Magnum Opus Capital / Young Money Investments)
License: Proprietary — All rights reserved
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from src.air.config import AIRConfig

if TYPE_CHECKING:
    from src.air.harvester import CortexHarvester
    from src.air.router import RoutingRouter
    from src.air.scorer import ConfidenceScorer
    from src.air.storage import RoutingStorage

logger = logging.getLogger("cortex-air")

_MARKER_START = "<!-- AIR:START -->"
_MARKER_END = "<!-- AIR:END -->"
_AIR_VERSION = "0.1.0"


class RouteInjector:
    """Formats routing rules and injects them into LLM context surfaces."""

    def __init__(
        self,
        router: "RoutingRouter",
        storage: "RoutingStorage",
        scorer: "ConfidenceScorer",
        config: AIRConfig,
    ) -> None:
        self._router = router
        self._storage = storage
        self._scorer = scorer
        self._config = config

    # ------------------------------------------------------------------
    # CLAUDE.md injection
    # ------------------------------------------------------------------

    def generate_claudemd_section(self, project_id: str = None) -> str:
        """Generate the managed CLAUDE.md section with high-confidence routes."""
        rules = self._get_high_confidence_rules(project_id)

        table_rows: list[str] = []
        for rule in rules:
            confidence = self._scorer.effective_confidence(rule)
            trigger = rule.get("trigger_pattern", "?")
            route = rule.get("optimal_route", "?")
            hits = rule.get("hit_count", 0)
            failed = ""
            if rule.get("failed_routes"):
                try:
                    failed_list = json.loads(rule["failed_routes"])
                    if isinstance(failed_list, list) and failed_list:
                        failed = ", ".join(failed_list[:3])
                except (json.JSONDecodeError, TypeError):
                    pass

            table_rows.append(
                f"| {trigger} | `{route}` | {confidence:.2f} | {hits} |"
            )

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rule_count = len(table_rows)

        lines = [
            _MARKER_START,
            "",
            "## AIR — Learned Tool Routes (auto-managed, do not edit)",
            "",
            "These routes were learned from observed tool-call patterns.",
            "They skip unnecessary intermediate steps.",
            "",
        ]

        if table_rows:
            lines.extend([
                "| When I say... | Do this instead | Confidence | Uses |",
                "|---------------|-----------------|------------|------|",
            ])
            lines.extend(table_rows)
        else:
            lines.append("*No high-confidence routes learned yet.*")

        lines.extend([
            "",
            f"<!-- AIR v{_AIR_VERSION} | {rule_count} rules | Updated: {timestamp} -->",
            "",
            _MARKER_END,
        ])

        return "\n".join(lines)

    def inject_claudemd(
        self,
        claudemd_path: Path,
        project_id: str = None,
    ) -> bool:
        """Write the managed AIR section into a CLAUDE.md file.

        Replaces existing AIR:START/AIR:END section in-place, or appends.
        Returns True if file was modified.
        """
        claudemd_path = Path(claudemd_path)
        new_section = self.generate_claudemd_section(project_id)

        if claudemd_path.exists():
            try:
                existing = claudemd_path.read_text(encoding="utf-8")
            except OSError:
                logger.error("Failed to read %s", claudemd_path)
                return False
        else:
            existing = ""

        start_idx = existing.find(_MARKER_START)
        end_idx = existing.find(_MARKER_END)

        if start_idx != -1 and end_idx != -1:
            end_idx += len(_MARKER_END)
            updated = existing[:start_idx] + new_section + existing[end_idx:]
        else:
            separator = "\n\n" if existing and not existing.endswith("\n\n") else (
                "\n" if existing and not existing.endswith("\n") else ""
            )
            updated = existing + separator + new_section + "\n"

        if updated == existing:
            return False

        try:
            claudemd_path.parent.mkdir(parents=True, exist_ok=True)
            claudemd_path.write_text(updated, encoding="utf-8")
            logger.info(
                "Injected AIR section into %s (%d rules)",
                claudemd_path, len(self._get_high_confidence_rules(project_id)),
            )
            return True
        except OSError:
            logger.error("Failed to write %s", claudemd_path, exc_info=True)
            return False

    # ------------------------------------------------------------------
    # Per-message hook injection (UserPromptSubmit)
    # ------------------------------------------------------------------

    def generate_message_context(
        self,
        user_message: str,
        project_id: str = None,
    ) -> Optional[str]:
        """Generate a context hint for a single user message.

        For the UserPromptSubmit hook. Matches medium-confidence rules
        (inject_threshold_low <= confidence < inject_threshold_high).
        """
        if not user_message or not user_message.strip():
            return None

        match = self._router.match(user_message, project_id=project_id)
        if match is None:
            return None

        rule = match.get("rule") or match
        confidence = self._scorer.effective_confidence(rule)

        low = self._config.inject_threshold_low
        high = self._config.inject_threshold_high

        if confidence < low or confidence >= high:
            return None

        trigger = rule.get("trigger_pattern", "?")
        route = rule.get("optimal_route", "?")

        return (
            f'[AIR hint: For "{trigger}", the optimal approach is: '
            f"`{route}` (confidence: {confidence:.2f})]"
        )

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def get_injection_stats(
        self, harvester: "Optional[CortexHarvester]" = None
    ) -> dict:
        """Return injection stats for the AIR pipeline.

        Parameters
        ----------
        harvester : CortexHarvester, optional
            If provided, used to compute ``cold_start_complete`` by comparing
            the observed session count against ``config.cold_start_cycles``.
            When omitted, falls back to rule count > 0 as a proxy.
        """
        all_rules = self._storage.list_active_rules()

        high_count = 0
        medium_count = 0

        for rule in all_rules:
            conf = self._scorer.effective_confidence(rule)
            if conf >= self._config.inject_threshold_high:
                high_count += 1
            elif conf >= self._config.inject_threshold_low:
                medium_count += 1

        if harvester is not None:
            observed_sessions = harvester.get_session_count(
                hours=24 * 365  # all-time count via 1-year window
            )
            cold_start_complete = (
                observed_sessions >= self._config.cold_start_cycles
            )
        else:
            # Proxy: any learned rules means at least some sessions have been
            # compiled, so consider cold-start complete.
            cold_start_complete = len(all_rules) > 0

        return {
            "high_confidence_rules": high_count,
            "medium_confidence_rules": medium_count,
            "total_rules": len(all_rules),
            "cold_start_complete": cold_start_complete,
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_high_confidence_rules(self, project_id: str = None) -> list[dict]:
        all_rules = self._storage.list_active_rules(project_id=project_id)
        high_rules = [
            r for r in all_rules
            if self._scorer.effective_confidence(r) >= self._config.inject_threshold_high
        ]
        high_rules.sort(
            key=lambda r: self._scorer.effective_confidence(r),
            reverse=True,
        )
        return high_rules
