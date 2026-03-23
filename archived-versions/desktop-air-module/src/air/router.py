#!/usr/bin/env python3
"""
AIR Routing Router — Lookup engine for matching user messages to optimal routes.

Given an incoming user message, the router normalizes it, computes a trigger
hash, and looks up the best matching routing rule from storage.  Cross-project
fallback is supported when confidence exceeds the cross-project threshold.

Usage:
    from src.air.config import AIRConfig
    from src.air.storage import RoutingStorage
    from src.air.scorer import ConfidenceScorer
    from src.air.router import RoutingRouter

    cfg = AIRConfig.from_env()
    router = RoutingRouter(storage, scorer, cfg)
    match = router.lookup("search for files matching *.py", project_id="my-proj")
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

    # -- Public API ------------------------------------------------------------

    def lookup(
        self, user_message: str, project_id: Optional[str] = None
    ) -> Optional[dict]:
        """Match *user_message* to the best routing rule, if any.

        Steps:
            1. Normalize the message and compute its trigger hash.
            2. Exact-match in storage scoped to *project_id*.
            3. If no project-scoped hit, try a cross-project lookup when the
               best candidate's confidence exceeds ``cross_project_threshold``.
            4. Return the match only if its confidence meets the injection
               threshold; otherwise return ``None``.

        Returns a dict on hit::

            {
                "rule_id":         str,
                "trigger_pattern": str,
                "optimal_route":   str,
                "confidence":      float,
                "hit_count":       int,
                "source":          "project" | "cross_project",
            }
        """
        normalized = self._normalize_message(user_message)
        trigger_hash = self._compute_hash(normalized)

        # 1. Project-scoped lookup
        match = self._try_lookup(trigger_hash, project_id)
        if match is not None:
            match["source"] = "project"
            if match["confidence"] >= self._config.inject_threshold_low:
                logger.debug(
                    "Router hit (project): rule=%s confidence=%.3f",
                    match["rule_id"],
                    match["confidence"],
                )
                self._storage.record_hit(match["rule_id"])
                return match
            logger.debug(
                "Router match below injection threshold: rule=%s confidence=%.3f < %.3f",
                match["rule_id"],
                match["confidence"],
                self._config.inject_threshold_low,
            )

        # 2. Cross-project fallback
        if project_id is not None:
            cross_match = self._try_lookup(trigger_hash, project_id=None)
            if cross_match is not None:
                cross_match["source"] = "cross_project"
                if cross_match["confidence"] >= self._config.cross_project_threshold:
                    logger.debug(
                        "Router hit (cross-project): rule=%s confidence=%.3f",
                        cross_match["rule_id"],
                        cross_match["confidence"],
                    )
                    self._storage.record_hit(cross_match["rule_id"])
                    return cross_match
                logger.debug(
                    "Cross-project match below threshold: rule=%s confidence=%.3f < %.3f",
                    cross_match["rule_id"],
                    cross_match["confidence"],
                    self._config.cross_project_threshold,
                )

        return None

    def match(
        self, user_message: str, project_id: Optional[str] = None
    ) -> Optional[dict]:
        """Alias for :meth:`lookup` (used by injector)."""
        return self.lookup(user_message, project_id=project_id)

    def record_outcome(self, rule_id: str, success: bool) -> float:
        """Record whether a routed action succeeded or failed.

        Delegates to :class:`ConfidenceScorer` to update the rule's confidence
        score and persists the new value to storage.

        Returns:
            The updated confidence score for *rule_id*.
        """
        rule = self._storage.get_rule(rule_id)
        if rule is None:
            logger.warning("record_outcome called for unknown rule_id=%s", rule_id)
            return 0.0

        new_confidence = self._scorer.update(
            current=rule["confidence"],
            success=success,
        )

        self._storage.update_confidence(rule_id, new_confidence)

        logger.info(
            "Outcome recorded: rule=%s success=%s confidence=%.3f -> %.3f",
            rule_id,
            success,
            rule["confidence"],
            new_confidence,
        )
        return new_confidence

    def get_top_routes(
        self, project_id: Optional[str] = None, limit: int = 20
    ) -> list[dict]:
        """Return the highest-confidence routes suitable for prompt injection.

        Only rules whose confidence meets ``inject_threshold_low`` are included.
        Results are ordered by confidence descending, then by hit_count
        descending.
        """
        routes = self._storage.get_routes_by_confidence(
            project_id=project_id,
            min_confidence=self._config.inject_threshold_low,
            limit=limit,
        )
        logger.debug(
            "get_top_routes: project=%s returned %d routes", project_id, len(routes)
        )
        return routes

    def resolve_conflicts(self, matches: list[dict]) -> dict:
        """Pick the single best rule when multiple candidates match.

        Tie-breaking order:
            1. Highest ``confidence``
            2. Most recent ``last_used`` timestamp
            3. Highest ``hit_count``

        Raises ``ValueError`` if *matches* is empty.
        """
        if not matches:
            raise ValueError("resolve_conflicts called with empty matches list")

        best = max(
            matches,
            key=lambda m: (
                m.get("confidence", 0.0),
                m.get("last_used", ""),
                m.get("hit_count", 0),
            ),
        )
        if len(matches) > 1:
            logger.debug(
                "Resolved conflict among %d matches -> rule=%s (confidence=%.3f)",
                len(matches),
                best.get("rule_id"),
                best.get("confidence", 0.0),
            )
        return best

    # -- Internal helpers ------------------------------------------------------

    @staticmethod
    def _normalize_message(message: str) -> str:
        """Lowercase, strip punctuation, and collapse whitespace."""
        text = message.lower()
        text = re.sub(r"[^\w\s]", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _compute_hash(normalized: str) -> str:
        """SHA-256 hex digest of the normalized message for fast lookup."""
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_rule(rule: dict) -> dict:
        """Ensure rule dict has 'rule_id' key (storage uses 'id')."""
        if "rule_id" not in rule and "id" in rule:
            rule["rule_id"] = rule["id"]
        return rule

    def _try_lookup(
        self, trigger_hash: str, project_id: Optional[str]
    ) -> Optional[dict]:
        """Query storage for rules matching *trigger_hash* and *project_id*.

        If multiple rules share the same hash (unlikely but possible), resolve
        via :meth:`resolve_conflicts`.
        """
        matches = self._storage.lookup_by_hash(
            trigger_hash=trigger_hash, project_id=project_id
        )
        if not matches:
            return None
        matches = [self._normalize_rule(m) for m in matches]
        if len(matches) == 1:
            return matches[0]
        return self.resolve_conflicts(matches)
