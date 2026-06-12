#!/usr/bin/env python3
"""
AIR CLI — Entry points for hook integration.

Commands:
  compile     — Scan cortex telemetry, compile miss-then-recover patterns
  inject      — Write high-confidence routes to CLAUDE.md managed section
  lookup      — Match a user message against routing rules (stdout JSON)
  stats       — Print AIR stats
  decay       — Apply time-based confidence decay to all rules

Called from ~/.claude/helpers/hook-handler.cjs at:
  SessionEnd       -> compile + inject + decay
  SessionStart     -> inject (refresh CLAUDE.md)
  UserPromptSubmit -> lookup (per-message hint)

Author: Cameron Bennion (Magnum Opus Capital / Young Money Investments)
License: Proprietary — All rights reserved
"""

import json
import sys
from pathlib import Path

# Ensure project root is on sys.path for src.air imports
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _build_pipeline():
    """Instantiate the full AIR pipeline."""
    from src.air.config import AIRConfig
    from src.air.storage import RoutingStorage
    from src.air.harvester import CortexHarvester
    from src.air.classifier import IntentClassifier
    from src.air.compiler import PatternCompiler
    from src.air.scorer import ConfidenceScorer
    from src.air.router import RoutingRouter
    from src.air.injector import RouteInjector

    config = AIRConfig.from_env()
    harvester = CortexHarvester(config)
    storage = RoutingStorage()
    scorer = ConfidenceScorer(config)
    classifier = IntentClassifier(config)
    compiler = PatternCompiler(harvester, storage, classifier, config, scorer)
    router = RoutingRouter(storage, scorer, config)
    injector = RouteInjector(router, storage, scorer, config)

    return {
        "config": config,
        "harvester": harvester,
        "storage": storage,
        "scorer": scorer,
        "classifier": classifier,
        "compiler": compiler,
        "router": router,
        "injector": injector,
    }


def cmd_compile(hours: int = 48):
    """Compile miss-then-recover patterns from recent cortex sessions."""
    p = _build_pipeline()
    rules = p["compiler"].compile_recent(hours=hours)
    print(json.dumps({
        "status": "ok",
        "rules_compiled": len(rules),
        "stats": p["storage"].get_stats(),
    }))


def cmd_inject():
    """Write high-confidence routes to ~/.claude/CLAUDE.md."""
    p = _build_pipeline()
    claudemd = Path.home() / ".claude" / "CLAUDE.md"
    changed = p["injector"].inject_claudemd(claudemd)
    stats = p["injector"].get_injection_stats()
    print(json.dumps({
        "status": "ok",
        "changed": changed,
        "high_rules": stats["high_confidence_rules"],
        "medium_rules": stats["medium_confidence_rules"],
    }))


def cmd_lookup(message: str):
    """Look up a routing hint for a user message."""
    p = _build_pipeline()
    hint = p["injector"].generate_message_context(message)
    if hint:
        print(hint)
    # No output if no match — hook handler will skip


def cmd_stats():
    """Print AIR pipeline stats."""
    p = _build_pipeline()
    stats = p["storage"].get_stats()
    injection = p["injector"].get_injection_stats()
    events = p["harvester"].get_event_count()
    sessions = p["harvester"].get_session_count(hours=168)
    print(json.dumps({
        "cortex_events": events,
        "recent_sessions_7d": sessions,
        **stats,
        **injection,
    }, indent=2))


def cmd_decay():
    """Apply time-based confidence decay to all rules."""
    p = _build_pipeline()
    result = p["scorer"].apply_decay_batch(p["storage"])
    print(json.dumps({"status": "ok", **result}))


def main():
    if len(sys.argv) < 2:
        print("Usage: air_cli.py <compile|inject|lookup|stats|decay> [args]")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "compile":
        hours = int(sys.argv[2]) if len(sys.argv) > 2 else 48
        cmd_compile(hours)
    elif cmd == "inject":
        cmd_inject()
    elif cmd == "lookup":
        message = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
        if not message:
            # Read from stdin for hook integration
            message = sys.stdin.read().strip()
        cmd_lookup(message)
    elif cmd == "stats":
        cmd_stats()
    elif cmd == "decay":
        cmd_decay()
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
