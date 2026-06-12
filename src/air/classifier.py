"""
AIR Intent Classifier — Determines user intent from tool-call sequences.

Supports two modes via AIRConfig.classifier_mode:
  - "api"   : Claude API (Haiku) for high-quality classification
  - "local" : TF-IDF / keyword heuristic for zero-cost, zero-latency

The classifier takes a user message and the observed tool-call sequence,
returning a structured classification with intent label, normalized trigger
pattern, trigger hash (for fast routing-table lookup), confidence score,
and the source that produced the classification.

Author: Cameron Bennion (Magnum Opus Capital / Young Money Investments)
License: Proprietary — All rights reserved
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from collections import Counter
from typing import Any

from src.air.config import AIRConfig

logger = logging.getLogger("cortex-air")

# ---------------------------------------------------------------------------
# Built-in intent taxonomy for the local (TF-IDF / heuristic) classifier.
# ---------------------------------------------------------------------------

_INTENT_TAXONOMY: dict[str, dict[str, Any]] = {
    "git_commit": {
        "keywords": [
            "commit", "save", "checkpoint", "lock in", "lock it in",
            "save changes", "commit changes", "wrap up",
        ],
        "tools": ["git add", "git commit"],
        "pattern": "commit changes",
    },
    "git_push": {
        "keywords": [
            "push", "push it", "push up", "send it", "push to remote",
            "push changes", "upload",
        ],
        "tools": ["git push"],
        "pattern": "push changes",
    },
    "git_branch": {
        "keywords": [
            "branch", "new branch", "create branch", "checkout",
            "switch branch", "feature branch",
        ],
        "tools": ["git branch", "git checkout", "git switch"],
        "pattern": "branch operation",
    },
    "git_status": {
        "keywords": [
            "status", "git status", "what changed", "show changes",
            "diff", "show diff", "what's different",
        ],
        "tools": ["git status", "git diff", "git log"],
        "pattern": "check status",
    },
    "file_read": {
        "keywords": [
            "read", "show", "display", "cat", "open", "look at",
            "what's in", "contents of", "print",
        ],
        "tools": ["Read", "cat", "head", "tail"],
        "pattern": "read file",
    },
    "file_write": {
        "keywords": [
            "write", "create file", "new file", "save to",
            "write to", "output to",
        ],
        "tools": ["Write", "echo", "tee"],
        "pattern": "write file",
    },
    "file_search": {
        "keywords": [
            "find", "search", "grep", "look for", "locate",
            "where is", "which file",
        ],
        "tools": ["Grep", "Glob", "find", "rg", "grep"],
        "pattern": "search files",
    },
    "file_edit": {
        "keywords": [
            "edit", "change", "modify", "update", "replace",
            "fix", "refactor", "rename",
        ],
        "tools": ["Edit", "sed", "awk"],
        "pattern": "edit file",
    },
    "code_run": {
        "keywords": [
            "run", "execute", "start", "launch", "invoke",
            "run it", "fire it up", "spin up",
        ],
        "tools": ["Bash", "python", "node", "npm start"],
        "pattern": "run code",
    },
    "code_test": {
        "keywords": [
            "test", "tests", "pytest", "unittest", "does it pass",
            "run tests", "test it", "check tests",
        ],
        "tools": ["pytest", "jest", "npm test", "unittest"],
        "pattern": "run tests",
    },
    "code_debug": {
        "keywords": [
            "debug", "breakpoint", "trace", "error", "bug",
            "why does", "what's wrong", "troubleshoot",
        ],
        "tools": ["pdb", "debugger", "traceback"],
        "pattern": "debug code",
    },
    "search_web": {
        "keywords": [
            "search web", "google", "look up online", "web search",
            "find online", "browse",
        ],
        "tools": ["WebSearch", "WebFetch"],
        "pattern": "web search",
    },
    "search_docs": {
        "keywords": [
            "docs", "documentation", "reference", "api docs",
            "man page", "help for", "how to use",
        ],
        "tools": ["deepwiki", "read_wiki"],
        "pattern": "search docs",
    },
    "skill_invoke": {
        "keywords": [
            "skill", "slash command", "use skill", "invoke",
            "plugin", "extension",
        ],
        "tools": ["Skill"],
        "pattern": "invoke skill",
    },
}


class IntentClassifier:
    """Classifies user intent from a message and observed tool-call sequence."""

    def __init__(self, config: AIRConfig) -> None:
        self._config = config

        self._keyword_index: dict[str, set[str]] = {}
        for intent, spec in _INTENT_TAXONOMY.items():
            for phrase in spec["keywords"]:
                for token in phrase.lower().split():
                    self._keyword_index.setdefault(token, set()).add(intent)

        self._tool_index: dict[str, set[str]] = {}
        for intent, spec in _INTENT_TAXONOMY.items():
            for tool in spec["tools"]:
                self._tool_index.setdefault(tool.lower(), set()).add(intent)

        logger.debug(
            "IntentClassifier initialized (mode=%s, intents=%d)",
            config.classifier_mode,
            len(_INTENT_TAXONOMY),
        )

    def classify(
        self,
        user_message: str,
        tool_sequence: list[dict],
    ) -> dict[str, Any]:
        """Classify the user's intent.

        Returns dict with: intent, trigger_pattern, trigger_hash,
        confidence, source.
        """
        if self._config.classifier_mode == "api":
            return self._classify_api(user_message, tool_sequence)
        return self._classify_local(user_message, tool_sequence)

    # -- API classifier (Claude Haiku) -----------------------------------------

    def _classify_api(
        self,
        user_message: str,
        tool_sequence: list[dict],
    ) -> dict[str, Any]:
        """Use Claude Haiku API to classify intent."""
        try:
            import httpx
        except ImportError:
            logger.warning("httpx not installed — falling back to local")
            return self._classify_local(user_message, tool_sequence)

        api_key = self._config.anthropic_api_key
        if not api_key:
            logger.warning("ANTHROPIC_API_KEY not set — falling back to local")
            return self._classify_local(user_message, tool_sequence)

        formatted_tools = self._format_tool_sequence(tool_sequence)

        prompt = (
            "You are an intent classifier for an LLM agentic coding assistant. "
            "Given a user message and the tool-call sequence that followed, "
            "identify:\n"
            "1. What the user was trying to do (intent label)\n"
            "2. A short normalized trigger pattern (2-4 words)\n"
            "3. Your confidence (0.0-1.0)\n\n"
            f'User message: "{user_message}"\n\n'
            f"Tool sequence:\n{formatted_tools}\n\n"
            "Return ONLY valid JSON with these exact fields:\n"
            "{\n"
            '  "intent": "<intent_label>",\n'
            '  "trigger_pattern": "<normalized 2-4 word pattern>",\n'
            '  "confidence": <float 0.0-1.0>\n'
            "}"
        )

        try:
            response = httpx.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 256,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=10.0,
            )
            response.raise_for_status()

            data = response.json()
            text = data["content"][0]["text"]

            json_match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
            if not json_match:
                logger.warning("API response missing JSON: %s", text[:200])
                return self._classify_local(user_message, tool_sequence)

            parsed = json.loads(json_match.group())
            intent = str(parsed.get("intent", "unknown"))
            trigger_pattern = str(parsed.get("trigger_pattern", user_message[:40]))
            confidence = max(0.0, min(1.0, float(parsed.get("confidence", 0.5))))
            trigger_hash = self._compute_trigger_hash(trigger_pattern)

            return {
                "intent": intent,
                "trigger_pattern": trigger_pattern,
                "trigger_hash": trigger_hash,
                "confidence": confidence,
                "source": "api",
            }

        except Exception as exc:
            logger.warning("API classify failed (%s) — falling back to local", exc)
            return self._classify_local(user_message, tool_sequence)

    # -- Local classifier (TF-IDF / heuristic) ---------------------------------

    def _classify_local(
        self,
        user_message: str,
        tool_sequence: list[dict],
    ) -> dict[str, Any]:
        """Classify intent using keyword/tool TF-IDF heuristic matching."""
        msg_lower = user_message.lower()
        msg_tokens = re.findall(r"[a-z_]+", msg_lower)

        tool_names: list[str] = []
        for call in tool_sequence:
            name = str(call.get("tool", "")).strip()
            if name:
                tool_names.append(name.lower())
            args = call.get("args", {})
            if isinstance(args, dict):
                cmd = str(args.get("command", ""))
                for known_tool in self._tool_index:
                    if known_tool in cmd.lower():
                        tool_names.append(known_tool)

        scores: Counter[str] = Counter()
        total_intents = len(_INTENT_TAXONOMY)

        for token in msg_tokens:
            matching_intents = self._keyword_index.get(token)
            if matching_intents:
                idf = math.log(total_intents / len(matching_intents)) + 1.0
                for intent in matching_intents:
                    scores[intent] += idf

        for intent, spec in _INTENT_TAXONOMY.items():
            for phrase in spec["keywords"]:
                if len(phrase.split()) > 1 and phrase.lower() in msg_lower:
                    scores[intent] += 3.0

        for tool_name in tool_names:
            matching_intents = self._tool_index.get(tool_name)
            if matching_intents:
                idf = math.log(total_intents / len(matching_intents)) + 1.0
                for intent in matching_intents:
                    scores[intent] += idf * 1.5

        if not scores:
            trigger_pattern = _normalize_trigger(user_message)
            return {
                "intent": "unknown",
                "trigger_pattern": trigger_pattern,
                "trigger_hash": self._compute_trigger_hash(trigger_pattern),
                "confidence": 0.0,
                "source": "local",
            }

        ranked = scores.most_common()
        best_intent, best_score = ranked[0]
        second_score = ranked[1][1] if len(ranked) > 1 else 0.0

        gap = best_score - second_score
        confidence = 1.0 / (1.0 + math.exp(-0.6 * (gap - 2.0)))
        confidence = round(max(0.05, min(0.99, confidence)), 4)

        trigger_pattern = _INTENT_TAXONOMY[best_intent]["pattern"]
        trigger_hash = self._compute_trigger_hash(trigger_pattern)

        return {
            "intent": best_intent,
            "trigger_pattern": trigger_pattern,
            "trigger_hash": trigger_hash,
            "confidence": confidence,
            "source": "local",
        }

    @staticmethod
    def _compute_trigger_hash(pattern: str) -> str:
        """SHA-256 hash of the lowercased, stripped trigger pattern."""
        normalized = pattern.lower().strip()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def _format_tool_sequence(tools: list[dict]) -> str:
        """Format a tool-call sequence into a compact string for API prompts."""
        lines: list[str] = []
        for i, call in enumerate(tools, 1):
            tool = call.get("tool", "unknown")
            args = call.get("args", {})
            result = call.get("result", "")
            error = call.get("error", "")

            args_str = ""
            if isinstance(args, dict):
                parts = [f"{k}={v!r}" for k, v in list(args.items())[:3]]
                args_str = ", ".join(parts)
            elif args:
                args_str = str(args)[:120]

            line = f"  {i}. {tool}({args_str})"
            if error:
                line += f" -> ERROR: {str(error)[:80]}"
            elif result:
                line += f" -> {str(result)[:60]}"
            lines.append(line)

        return "\n".join(lines) if lines else "  (empty sequence)"


def _normalize_trigger(message: str) -> str:
    """Produce a short normalized trigger from an arbitrary user message."""
    cleaned = re.sub(r"[^a-zA-Z0-9\s]", "", message.lower()).strip()
    words = cleaned.split()[:5]
    return " ".join(words) if words else "unknown"
