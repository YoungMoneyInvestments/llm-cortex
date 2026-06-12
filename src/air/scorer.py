"""
AIR Confidence Scorer — Manages confidence scoring, decay, and pruning.

Pure policy module: all math and thresholds live here. No direct I/O
except through the RoutingStorage parameter in apply_decay_batch.

Asymmetric by design — penalties hit harder than rewards so bad routes
die fast while good routes accrete slowly.

Author: Cameron Bennion (Magnum Opus Capital / Young Money Investments)
License: Proprietary — All rights reserved
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
    """Stateless confidence math driven entirely by AIRConfig thresholds."""

    def __init__(self, config: AIRConfig) -> None:
        self._cfg = config

    def reward(self, current_confidence: float) -> float:
        """Boost confidence on successful route use."""
        return min(1.0, current_confidence + self._cfg.confidence_reward)

    def penalize(self, current_confidence: float) -> float:
        """Reduce confidence on failed route use (hits harder than reward)."""
        return max(0.0, current_confidence - self._cfg.confidence_penalty)

    def decay(self, current_confidence: float, days_since_use: float) -> float:
        """Apply time-based exponential decay (default: 0.95 per week)."""
        return current_confidence * (
            self._cfg.confidence_decay_rate ** (days_since_use / 7.0)
        )

    def update(self, current: float, success: bool) -> float:
        """Dispatch to reward or penalize based on outcome."""
        return self.reward(current) if success else self.penalize(current)

    def effective_confidence(self, rule: dict) -> float:
        """Compute confidence with time decay applied."""
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

    def should_prune(self, confidence: float) -> bool:
        return confidence < self._cfg.prune_threshold

    def should_inject_high(self, confidence: float) -> bool:
        """True if eligible for CLAUDE.md injection (high tier)."""
        return confidence >= self._cfg.inject_threshold_high

    def should_inject_low(self, confidence: float) -> bool:
        """True if eligible for per-message hook injection (medium tier)."""
        return (
            self._cfg.inject_threshold_low
            <= confidence
            < self._cfg.inject_threshold_high
        )

    def apply_decay_batch(self, storage: "RoutingStorage") -> Dict[str, int]:
        """Apply time-based decay to every rule in storage.

        Returns {"decayed": int, "pruned": int, "unchanged": int}.
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
            elif new_confidence != current_conf:
                storage.update_rule(rule_id, {"confidence": new_confidence})
                stats["decayed"] += 1
            else:
                stats["unchanged"] += 1

        logger.info(
            "Decay batch complete: %d decayed, %d pruned, %d unchanged",
            stats["decayed"], stats["pruned"], stats["unchanged"],
        )
        return stats
