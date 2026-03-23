#!/usr/bin/env python3
"""
AIR CLI — Command-line interface for Adaptive Inference Routing.

Called by Cortex hooks to ingest events, compile patterns, inject routes,
and provide per-message routing hints.

Subcommands:
    ingest   — Ingest a tool-call event (JSON on stdin or as argument)
    compile  — Compile patterns from recent sessions
    inject   — Update CLAUDE.md with high-confidence routes
    hint     — Get a per-message routing hint for a user prompt
    stats    — Show AIR statistics
    decay    — Apply confidence decay to all rules

Usage from hooks:
    echo '{"session_id":"...","tool_name":"Bash",...}' | python3 air_cli.py ingest
    python3 air_cli.py compile --hours 48
    python3 air_cli.py inject /path/to/CLAUDE.md
    python3 air_cli.py hint "commit the changes"
    python3 air_cli.py stats
"""

import json
import logging
import sys
from pathlib import Path

# Ensure src/ is importable
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.air.config import AIRConfig
from src.air.storage import RoutingStorage
from src.air.harvester import TelemetryHarvester
from src.air.compiler import PatternCompiler
from src.air.classifier import IntentClassifier
from src.air.scorer import ConfidenceScorer
from src.air.router import RoutingRouter
from src.air.injector import RouteInjector

logger = logging.getLogger("cortex-air")


def _setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,
    )


def _build_stack(config: AIRConfig = None):
    """Construct the full AIR component stack."""
    config = config or AIRConfig.from_env()
    storage = RoutingStorage(db_path=config.db_path)
    scorer = ConfidenceScorer(config)
    classifier = IntentClassifier(config)
    harvester = TelemetryHarvester(storage, config)
    compiler = PatternCompiler(storage, classifier, config, scorer)
    router = RoutingRouter(storage, scorer, config)
    injector = RouteInjector(router, storage, scorer, config)
    return {
        "config": config,
        "storage": storage,
        "scorer": scorer,
        "classifier": classifier,
        "harvester": harvester,
        "compiler": compiler,
        "router": router,
        "injector": injector,
    }


# ── Subcommands ─────────────────────────────────────────────────────────


def cmd_ingest(args: list[str]):
    """Ingest a tool-call event from stdin JSON or argument."""
    stack = _build_stack()

    # Read event JSON from stdin or first argument
    if not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
    elif args:
        raw = args[0]
    else:
        print("Error: provide event JSON on stdin or as argument", file=sys.stderr)
        sys.exit(1)

    if not raw:
        sys.exit(0)

    try:
        event = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Error: invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    event_id = stack["harvester"].ingest_event(event)
    logger.info("Ingested event %d for session %s", event_id, event.get("session_id", "?"))


def cmd_compile(args: list[str]):
    """Compile patterns from recent sessions."""
    hours = 48
    if args and args[0] == "--hours" and len(args) > 1:
        hours = int(args[1])

    stack = _build_stack()
    rules = stack["compiler"].compile_recent(hours=hours)
    count = len(rules)
    if count:
        logger.info("Compiled %d routing rules from recent sessions", count)
    else:
        logger.info("No new patterns found in last %d hours", hours)
    print(json.dumps({"compiled_rules": count}))


def cmd_inject(args: list[str]):
    """Update CLAUDE.md with high-confidence routes."""
    stack = _build_stack()

    # Determine CLAUDE.md path
    if args:
        claude_md_path = Path(args[0]).expanduser()
    else:
        # Default: look for CLAUDE.md in current working directory
        claude_md_path = Path.cwd() / "CLAUDE.md"

    if not claude_md_path.exists():
        logger.warning("CLAUDE.md not found at %s — creating it", claude_md_path)
        claude_md_path.write_text("# Project Instructions\n\n")

    project_id = claude_md_path.parent.name

    modified = stack["injector"].inject_claudemd(claude_md_path, project_id=project_id)
    if modified:
        stats = stack["injector"].get_injection_stats()
        logger.info(
            "Injected %d high-confidence routes into %s",
            stats.get("high_confidence_rules", 0),
            claude_md_path,
        )
    else:
        logger.info("No changes to %s (no new routes or below threshold)", claude_md_path)


def cmd_hint(args: list[str]):
    """Get a per-message routing hint for a user prompt.

    Outputs the hint to stdout if a match is found, or nothing if no match.
    Designed to be captured by user_prompt_submit.sh.
    """
    if not args:
        # Try reading from stdin
        if not sys.stdin.isatty():
            message = sys.stdin.read().strip()
        else:
            sys.exit(0)
    else:
        message = " ".join(args)

    if not message:
        sys.exit(0)

    stack = _build_stack()
    hint = stack["injector"].generate_message_context(message)
    if hint:
        print(hint)


def cmd_stats(args: list[str]):
    """Show AIR statistics."""
    stack = _build_stack()

    storage_stats = stack["storage"].get_stats()
    injection_stats = stack["injector"].get_injection_stats()

    output = {
        **storage_stats,
        **injection_stats,
    }

    print(json.dumps(output, indent=2))


def cmd_decay(args: list[str]):
    """Apply confidence decay to all rules."""
    stack = _build_stack()
    result = stack["scorer"].apply_decay_batch(stack["storage"])
    print(json.dumps(result))
    logger.info("Decay complete: %s", result)


# ── Main ────────────────────────────────────────────────────────────────


COMMANDS = {
    "ingest": cmd_ingest,
    "compile": cmd_compile,
    "inject": cmd_inject,
    "hint": cmd_hint,
    "stats": cmd_stats,
    "decay": cmd_decay,
}


def main():
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    args = [a for a in sys.argv[1:] if a not in ("--verbose", "-v")]

    _setup_logging(verbose)

    if not args:
        print("Usage: air_cli.py <command> [args...]", file=sys.stderr)
        print(f"Commands: {', '.join(COMMANDS.keys())}", file=sys.stderr)
        sys.exit(1)

    cmd = args[0]
    if cmd not in COMMANDS:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print(f"Commands: {', '.join(COMMANDS.keys())}", file=sys.stderr)
        sys.exit(1)

    try:
        COMMANDS[cmd](args[1:])
    except Exception as e:
        logger.error("AIR %s failed: %s", cmd, e, exc_info=verbose)
        sys.exit(1)


if __name__ == "__main__":
    main()
