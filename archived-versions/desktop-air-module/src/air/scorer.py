#!/usr/bin/env python3
"""
AIR Confidence Scorer — Manages confidence scoring, decay, and pruning.

Pure policy module: all math and thresholds live here.  No direct I/O
except through the ``RoutingStorage`` parameter in ``apply_decay_batch``.

Asymmetric by design — penalties hit harder than rewards so bad routes
die fast while good routes accrete slowly.

Usage:
    from src.air.config import AIRConfig
    from src.air.scorer import ConfidenceScorer

    cfg = AIRConfig.from_env()
    scorer = ConfidenceScorer(cfg)

    new_conf = scorer.reward(0.6)    # -> 0.7
    new_conf = scorer.penalize(0.6)  # -> 0.4
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Dict

from src.air.config import AIRConfig

if TYPE_CHECKING:
    from src.air.storage import RoutingStorage

logger = logging.getLogger("cortex-air")


class ConfidenceScorer:
    """Stateless confidence math driven entirely by ``AIRConfig`` thresholds."""

    def __init__(self, config: AIRConfig) -> None:
        self._cfg = config

    # -- Point adjustments -----------------------------------------------------

    def reward(self, current_confidence: float) -> float:
        """Boost confidence on successful route use.

        Formula: ``min(1.0, current + config.confidence_reward)``
        """
        return min(1.0, current_confidence + self._cfg.confidence_reward)

    def penalize(self, current_confidence: float) -> float:
        """Reduce confidence on failed route use.

        Penalises harder than reward — bad routes should die fast.
        Formula: ``max(0.0, current - config.confidence_penalty)``
        """
        return max(0.0, current_confidence - self._cfg.confidence_penalty)

    # -- Time-based decay ------------------------------------------------------

    def decay(self, current_confidence: float, days_since_use: float) -> float:
        """Apply time-based exponential decay.

        Formula: ``current * (decay_rate ** (days_since_use / 7.0))``
        Default decay_rate is 0.95 per week.
        """
        return current_confidence * (
            self._cfg.confidence_decay_rate ** (days_since_use / 7.0)
        )

    # -- Convenience dispatchers -----------------------------------------------

    def update(self, current: float, success: bool) -> float:
        """Dispatch to :meth:`reward` or :meth:`penalize` based on outcome."""
        return self.reward(current) if success else self.penalize(current)

    def effective_confidence(self, rule: dict) -> float:
        """Compute confidence with time decay applied for a rule dict.

        Uses the rule's ``confidence`` and ``last_used`` fields to compute
        the current effective confidence after decay.  If ``last_used`` is
        absent or unparseable, returns raw confidence.
        """
        raw = rule.get("confidence", 0.0)
        last_used = rule.get("last_used")
        if not last_used:
            return raw
        try:
            ts = datetime.fromisoformat(last_used.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            days = (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0
            if days <= 0:
                return raw
            return self.decay(raw, days)
        except (TypeError, ValueError):
            return raw

    # -- Threshold checks ------------------------------------------------------

    def should_prune(self, confidence: float) -> bool:
        """Return ``True`` if *confidence* falls below the prune threshold."""
        return confidence < self._cfg.prune_threshold

    def should_inject_high(self, confidence: float) -> bool:
        """Return ``True`` if eligible for CLAUDE.md injection (high tier)."""
        return confidence >= self._cfg.inject_threshold_high

    def should_inject_low(self, confidence: float) -> bool:
        """Return ``True`` if eligible for per-message hook injection (low tier).

        The rule must sit between ``inject_threshold_low`` and
        ``inject_threshold_high`` — high-confidence rules are handled
        separately via CLAUDE.md.
        """
        return (
            self._cfg.inject_threshold_low
            <= confidence
            < self._cfg.inject_threshold_high
        )

    # -- Batch maintenance -----------------------------------------------------

    def apply_decay_batch(self, storage: "RoutingStorage") -> Dict[str, int]:
        """Apply time-based decay to every rule in *storage*.

        Workflow:
            1. Fetch all rules from storage.
            2. For each rule, compute days since ``last_used``.
            3. Apply decay formula.
            4. Prune rules that drop below threshold.
            5. Update remaining rules with new confidence.

        Returns:
            ``{"decayed": int, "pruned": int, "unchanged": int}``
        """
        stats: Dict[str, int] = {"decayed": 0, "pruned": 0, "unchanged": 0}
        now = datetime.now(timezone.utc)

        rules = storage.get_all_rules()
        logger.info("Decay batch: processing %d rules", len(rules))

        for rule in rules:
            last_used_str = rule.get("last_used")
            if not last_used_str:
                stats["unchanged"] += 1
                continue

            try:
                last_used_dt = datetime.fromisoformat(
                    last_used_str.replace("Z", "+00:00")
                )
                if last_used_dt.tzinfo is None:
                    last_used_dt = last_used_dt.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                stats["unchanged"] += 1
                continue

            days_since_use = (now - last_used_dt).total_seconds() / 86400.0

            if days_since_use <= 0:
                stats["unchanged"] += 1
                continue

            current_conf = rule.get("confidence", 0.5)
            new_confidence = self.decay(current_conf, days_since_use)
            rule_id = rule["id"]

            if self.should_prune(new_confidence):
                storage.delete_rule(rule_id)
                stats["pruned"] += 1
                logger.debug(
                    "Pruned rule %s (confidence %.4f -> %.4f, %.1f days idle)",
                    rule_id,
                    current_conf,
                    new_confidence,
                    days_since_use,
                )
            elif new_confidence != current_conf:
                storage.update_rule(rule_id, {"confidence": new_confidence})
                stats["decayed"] += 1
                logger.debug(
                    "Decayed rule %s: %.4f -> %.4f (%.1f days idle)",
                    rule_id,
                    current_conf,
                    new_confidence,
                    days_since_use,
                )
            else:
                stats["unchanged"] += 1

        logger.info(
            "Decay batch complete: %d decayed, %d pruned, %d unchanged",
            stats["decayed"],
            stats["pruned"],
            stats["unchanged"],
        )
        return stats
