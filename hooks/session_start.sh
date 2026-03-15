#!/bin/bash
# Cortex SessionStart Hook — starts worker + runs context loader
#
# Called by Claude Code at the start of every session.
# Ensures the memory worker is running, then injects session context.
#
# Configure:
#   CORTEX_WORKSPACE   — Project root (default: ~/cortex)
#   CORTEX_WORKER_PORT — Worker port (default: 37778)
#   CORTEX_PYTHON      — Python interpreter (default: python3)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CORTEX_ROOT="$(dirname "$SCRIPT_DIR")"
WORKSPACE="${CORTEX_WORKSPACE:-$HOME/cortex}"
PYTHON="${CORTEX_PYTHON:-python3}"

# Start the worker if not running
"$CORTEX_ROOT/scripts/start_worker.sh" start 2>/dev/null

# Run context loader to inject session bootstrap
"$PYTHON" "$CORTEX_ROOT/scripts/context_loader.py" --hours 48

exit 0
