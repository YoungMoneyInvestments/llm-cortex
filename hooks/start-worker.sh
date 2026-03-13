#!/bin/bash
# Cortex Worker Startup Hook — ensures the background worker is running
#
# Called on SessionStart. Delegates to the main worker launcher script.
# Uses port 37778 to avoid conflict with claude-mem (37777).

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CORTEX_ROOT="$(dirname "$SCRIPT_DIR")"
WORKER_SCRIPT="${CORTEX_WORKER_SCRIPT:-$CORTEX_ROOT/scripts/start_worker.sh}"

if [ -x "$WORKER_SCRIPT" ]; then
    "$WORKER_SCRIPT" start 2>/dev/null
else
    echo "Warning: Worker launcher not found at $WORKER_SCRIPT" >&2
fi
