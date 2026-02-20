#!/bin/bash
# Cortex SessionEnd Hook — triggers session consolidation
#
# Called by Claude Code when a session ends.
# Tells the worker to finalize the session and queue summarization.
#
# Environment variables (set by Claude Code):
#   SESSION_ID   — current session identifier
#
# Configure:
#   CORTEX_WORKER_PORT — Worker port (default: 7778)

WORKER_PORT="${CORTEX_WORKER_PORT:-7778}"
WORKER_URL="http://127.0.0.1:$WORKER_PORT"

# Skip if worker isn't running
if ! curl -s --connect-timeout 1 "$WORKER_URL/api/health" > /dev/null 2>&1; then
    exit 0
fi

SID="${SESSION_ID:-$(date +%Y%m%d-%H%M%S)}"

# End session
curl -s --max-time 2 \
    -X POST "$WORKER_URL/api/sessions/end" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg sid "$SID" '{session_id: $sid}')" \
    > /dev/null 2>&1 &

exit 0
