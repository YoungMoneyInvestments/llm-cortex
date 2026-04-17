#!/bin/bash
# Cortex SessionEnd Hook — triggers session consolidation
#
# Called by Claude Code when a session ends (Stop hook).
# Claude Code pipes JSON to stdin with session_id.
# Tells the worker to finalize the session and queue summarization.
#
# Configure:
#   CORTEX_WORKER_PORT — Worker port (default: 37778)
#   CORTEX_WORKER_API_KEY — Required bearer token for POST endpoints

WORKER_PORT="${CORTEX_WORKER_PORT:-37778}"
WORKER_URL="http://127.0.0.1:$WORKER_PORT"
AUTH_KEY="${CORTEX_WORKER_API_KEY:-}"
# Fall back to generated key file if env var is absent (DEF-6 compatibility)
if [ -z "$AUTH_KEY" ] && [ -f "$HOME/.cortex/data/.worker_api_key" ]; then
    AUTH_KEY="$(cat "$HOME/.cortex/data/.worker_api_key" 2>/dev/null)"
fi

# Read stdin (Claude Code sends JSON)
INPUT_JSON=$(cat)

# Skip if worker isn't running
if ! curl -s --connect-timeout 1 "$WORKER_URL/api/health" > /dev/null 2>&1; then
    exit 0
fi

SESSION_ID=$(echo "$INPUT_JSON" | jq -r '.session_id // empty' 2>/dev/null)

if [ -z "$AUTH_KEY" ]; then
    echo "Warning: CORTEX_WORKER_API_KEY is not set; skipping Cortex session finalization." >&2
    exit 0
fi

SID="${SESSION_ID:-$(date +%Y%m%d-%H%M%S)}"

# End session
curl -s --max-time 2 \
    -X POST "$WORKER_URL/api/sessions/end" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $AUTH_KEY" \
    -d "$(jq -n --arg sid "$SID" '{session_id: $sid}')" \
    > /dev/null 2>&1 &

exit 0
