"""
AIR Routing Router — Lookup engine for matching user messages to optimal routes.

Given an incoming user message, the router normalizes it, computes a trigger
hash, and looks up the best matching routing rule from storage. Cross-project
fallback is supported when confidence exceeds the cross-project threshold.

Author: Cameron Bennion (Magnum Opus Capital / Young Money Investments)
License: Proprietary — All rights reserved
"""

import hashlib
import logging
import re
from typing import Optional

from src.air.config import AIRConfig
from src.air.scorer import ConfidenceScorer
from src.air.storage import RoutingStorage

logger = logging.getLogger("cortex-air")


class RoutingRouter:
    """Lookup engine that matches user messages against stored routing rules."""

    def __init__(
        self,
        storage: RoutingStorage,
        scorer: ConfidenceScorer,
        config: AIRConfig,
    ) -> None:
        self._storage = storage
        self._scorer = scorer
        self._config = config

    def lookup(
        self, user_message: str, project_id: Optional[str] = None
    ) -> Optional[dict]:
        """Match user_message to the best routing rule, if any.

        Returns a dict on hit with rule_id, trigger_pattern, optimal_route,
        confidence, hit_count, source.
        """
        normalized = self._normalize_message(user_message)
        trigger_hash = self._compute_hash(normalized)

        match = self._try_lookup(trigger_hash, project_id)
        if match is not None:
            match["source"] = "project"
            if match["confidence"] >= self._config.inject_threshold_low:
                self._storage.record_hit(match["rule_id"])
                return match

        if project_id is not None:
            cross_match = self._try_lookup(trigger_hash, project_id=None)
            if cross_match is not None:
                cross_match["source"] = "cross_project"
                if cross_match["confidence"] >= self._config.cross_project_threshold:
                    self._storage.record_hit(cross_match["rule_id"])
                    return cross_match

        return None

    def match(
        self, user_message: str, project_id: Optional[str] = None
    ) -> Optional[dict]:
        """Alias for lookup (used by injector)."""
        return self.lookup(user_message, project_id=project_id)

    def record_outcome(self, rule_id: str, success: bool) -> float:
        """Record whether a routed action succeeded or failed.

        On failure, increments miss_count in addition to penalizing confidence
        so that ``get_stats()`` reports accurate hit/miss ratios.
        """
        rule = self._storage.get_rule(rule_id)
        if rule is None:
            return 0.0

        new_confidence = self._scorer.update(
            current=rule["confidence"], success=success,
        )
        self._storage.update_confidence(rule_id, new_confidence)
        if not success:
            self._storage.record_miss(rule_id)
        return new_confidence

    def get_top_routes(
        self, project_id: Optional[str] = None, limit: int = 20
    ) -> list[dict]:
        """Return highest-confidence routes suitable for prompt injection."""
        return self._storage.get_routes_by_confidence(
            project_id=project_id,
            min_confidence=self._config.inject_threshold_low,
            limit=limit,
        )

    def resolve_conflicts(self, matches: list[dict]) -> dict:
        """Pick the single best rule when multiple candidates match."""
        if not matches:
            raise ValueError("resolve_conflicts called with empty matches list")

        return max(
            matches,
            key=lambda m: (
                m.get("confidence", 0.0),
                m.get("last_used", ""),
                m.get("hit_count", 0),
            ),
        )

    @staticmethod
    def _normalize_message(message: str) -> str:
        text = message.lower()
        text = re.sub(r"[^\w\s]", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _compute_hash(normalized: str) -> str:
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_rule(rule: dict) -> dict:
        if "rule_id" not in rule and "id" in rule:
            rule["rule_id"] = rule["id"]
        return rule

    def _try_lookup(
        self, trigger_hash: str, project_id: Optional[str]
    ) -> Optional[dict]:
        matches = self._storage.lookup_by_hash(
            trigger_hash=trigger_hash, project_id=project_id
        )
        if not matches:
            return None
        matches = [self._normalize_rule(m) for m in matches]
        if len(matches) == 1:
            return matches[0]
        return self.resolve_conflicts(matches)
