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

# AIR: Inject high-confidence routes into CLAUDE.md and compile recent patterns
AIR_CLI="$CORTEX_ROOT/scripts/air_cli.py"
if [ -f "$AIR_CLI" ]; then
    # Compile patterns from recent sessions (fire-and-forget)
    "$PYTHON" "$AIR_CLI" compile --hours 48 > /dev/null 2>&1
    # Apply confidence decay
    "$PYTHON" "$AIR_CLI" decay > /dev/null 2>&1
    # Inject routes into CLAUDE.md if it exists in the workspace
    if [ -f "$WORKSPACE/CLAUDE.md" ]; then
        "$PYTHON" "$AIR_CLI" inject "$WORKSPACE/CLAUDE.md" > /dev/null 2>&1
    fi
fi

exit 0
