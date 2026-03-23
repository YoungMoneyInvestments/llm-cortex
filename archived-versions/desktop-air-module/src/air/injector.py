#!/usr/bin/env python3
"""
AIR Route Injector — Formats learned routing rules and injects them into
the LLM's context through CLAUDE.md managed sections and per-message hooks.

The injector is the final stage of the AIR pipeline: it takes compiled,
confidence-scored routing rules and presents them where the model can use
them — either as a persistent CLAUDE.md section (high-confidence rules) or
as a per-message context hint (medium-confidence rules).

Two injection surfaces:
  1. CLAUDE.md managed section  — high-confidence rules (>= inject_threshold_high)
     Persisted between sessions, human-readable, auto-managed between markers.
  2. UserPromptSubmit hook hint — medium-confidence rules (inject_threshold_low .. high)
     Ephemeral, per-message, lower commitment.

Author: Cameron Bennion (Magnum Opus Capital / Young Money Investments)
License: Proprietary — All rights reserved
"""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.air.config import AIRConfig

logger = logging.getLogger("cortex-air")

# -- Managed section markers (never change these — downstream parsers depend on them) --
_MARKER_START = "<!-- AIR:START -->"
_MARKER_END = "<!-- AIR:END -->"

_AIR_VERSION = "0.1.0"


class RouteInjector:
    """Formats routing rules and injects them into LLM context surfaces.

    Parameters
    ----------
    router : RoutingRouter
        Provides rule lookup and matching against user messages.
    storage : RoutingStorage
        Persistent store for routing rules (query, list, stats).
    scorer : ConfidenceScorer
        Computes current effective confidence for rules (including decay).
    config : AIRConfig
        Thresholds, paths, and tuning knobs.
    """

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
        """Generate the managed CLAUDE.md section with high-confidence routes.

        Only rules whose effective confidence (after decay) meets or exceeds
        ``config.inject_threshold_high`` are included.  Rules are sorted by
        confidence descending so the most reliable routes appear first.

        Parameters
        ----------
        project_id : str, optional
            Scope rules to a specific project.  ``None`` returns global rules.

        Returns
        -------
        str
            A complete managed section including start/end markers, ready to
            be spliced into a CLAUDE.md file.
        """
        rules = self._get_high_confidence_rules(project_id)

        # Build the table rows
        table_rows: list[str] = []
        for rule in rules:
            confidence = self._scorer.effective_confidence(rule)
            trigger_display = self._trigger_display(rule)
            action_display = self._action_display(rule)
            use_count = rule.get("observation_count", 0)
            table_rows.append(
                f"| {trigger_display} | {action_display} "
                f"| {confidence:.2f} | {use_count} |"
            )

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rule_count = len(table_rows)

        lines = [
            _MARKER_START,
            "",
            "# AIR — Learned Tool Routes (auto-managed, do not edit)",
            "",
            "These routes were learned from observed tool-call patterns. They represent",
            "optimized shortcuts that skip unnecessary intermediate steps.",
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

        If the file already contains the managed section (delimited by
        ``<!-- AIR:START -->`` / ``<!-- AIR:END -->``), that section is
        replaced in-place.  Otherwise the section is appended.

        Parameters
        ----------
        claudemd_path : Path
            Absolute or relative path to the target CLAUDE.md file.
        project_id : str, optional
            Scope rules to a specific project.

        Returns
        -------
        bool
            ``True`` if the file was modified (content changed), ``False``
            if the new section is identical to what was already on disk or
            the file could not be read.
        """
        claudemd_path = Path(claudemd_path)
        new_section = self.generate_claudemd_section(project_id)

        # Read existing content (or start empty if file doesn't exist yet)
        if claudemd_path.exists():
            try:
                existing = claudemd_path.read_text(encoding="utf-8")
            except OSError:
                logger.error("Failed to read %s", claudemd_path)
                return False
        else:
            existing = ""

        # Attempt in-place replacement between markers
        start_idx = existing.find(_MARKER_START)
        end_idx = existing.find(_MARKER_END)

        if start_idx != -1 and end_idx != -1:
            # Found existing managed section — replace it
            end_idx += len(_MARKER_END)
            updated = existing[:start_idx] + new_section + existing[end_idx:]
        else:
            # No existing section — append with a separator
            separator = "\n\n" if existing and not existing.endswith("\n\n") else (
                "\n" if existing and not existing.endswith("\n") else ""
            )
            updated = existing + separator + new_section + "\n"

        # Only write if content actually changed
        if updated == existing:
            logger.debug("CLAUDE.md section unchanged, skipping write")
            return False

        try:
            claudemd_path.parent.mkdir(parents=True, exist_ok=True)
            claudemd_path.write_text(updated, encoding="utf-8")
            logger.info(
                "Injected AIR section into %s (%d rules)",
                claudemd_path,
                len(self._get_high_confidence_rules(project_id)),
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

        Intended for the ``UserPromptSubmit`` hook.  Matches the message
        against routing rules in the *medium* confidence band
        (``inject_threshold_low`` <= confidence < ``inject_threshold_high``).

        Rules at or above the high threshold are already in CLAUDE.md and
        do not need per-message reinforcement.

        Parameters
        ----------
        user_message : str
            The raw user prompt text.
        project_id : str, optional
            Scope to a specific project.

        Returns
        -------
        str or None
            A bracketed hint string if a match is found, otherwise ``None``.
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
            # Below threshold or already covered by CLAUDE.md
            return None

        trigger_display = self._trigger_display(rule)
        action_display = self._action_display(rule)

        return (
            f'[AIR hint: For "{trigger_display}", the optimal approach is: '
            f"{action_display} (confidence: {confidence:.2f})]"
        )

    # ------------------------------------------------------------------
    # Display helpers
    # ------------------------------------------------------------------

    def format_rule_for_display(self, rule: dict) -> str:
        """Format a single routing rule for human-readable display.

        Parameters
        ----------
        rule : dict
            A routing rule dict as returned by storage/router.

        Returns
        -------
        str
            Multi-line human-readable representation.
        """
        confidence = self._scorer.effective_confidence(rule)
        trigger = self._trigger_display(rule)
        action = self._action_display(rule)
        use_count = rule.get("observation_count", 0)
        skip = rule.get("skip_tools", [])
        skip_display = ", ".join(skip) if skip else "(none)"
        last_seen = rule.get("last_seen", "unknown")
        source = rule.get("classifier_source", "unknown")

        lines = [
            f"  PATTERN: {trigger}",
            f"  ACTION:  {action}",
            f"  SKIP:    {skip_display}",
            f"  CONFIDENCE: {confidence:.2f} ({use_count} observations, source: {source})",
            f"  LAST SEEN:  {last_seen}",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def get_injection_stats(self) -> dict:
        """Return statistics about currently injectable rules.

        Returns
        -------
        dict
            Keys: ``high_confidence_rules``, ``medium_confidence_rules``,
            ``total_rules``, ``cold_start_complete``.
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

        total_observations = sum(
            r.get("observation_count", 0) for r in all_rules
        )
        cold_start_complete = total_observations >= self._config.cold_start_cycles

        return {
            "high_confidence_rules": high_count,
            "medium_confidence_rules": medium_count,
            "total_rules": len(all_rules),
            "cold_start_complete": cold_start_complete,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_high_confidence_rules(self, project_id: str = None) -> list[dict]:
        """Return active rules at or above the high injection threshold.

        Results are sorted by effective confidence descending.
        """
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

    @staticmethod
    def _trigger_display(rule: dict) -> str:
        """Extract a concise trigger string from a rule dict."""
        examples = rule.get("trigger_examples") or rule.get("trigger_patterns") or []
        if isinstance(examples, str):
            return examples
        if examples:
            return " | ".join(f'"{ex}"' for ex in examples[:3])
        # Fallback to canonical trigger or hash
        return rule.get("canonical_trigger", rule.get("trigger_hash", "?"))

    @staticmethod
    def _action_display(rule: dict) -> str:
        """Extract a concise action string from a rule dict."""
        action = rule.get("action_json") or rule.get("resolved_action")
        if action is None:
            return "?"
        if isinstance(action, dict):
            tool = action.get("tool", "?")
            cmd = action.get("command_template") or action.get("command", "")
            return f"`{tool}: {cmd}`" if cmd else f"`{tool}`"
        # Already a string
        return f"`{action}`"
