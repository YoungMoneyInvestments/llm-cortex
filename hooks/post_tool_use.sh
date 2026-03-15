#!/bin/bash
# Cortex PostToolUse Hook — captures tool usage observations
#
# Called by Claude Code after every tool execution.
# Claude Code pipes JSON to stdin with tool_name, tool_input, tool_output, session_id.
# Sends observation to the background worker via HTTP (fire-and-forget).
#
# Configure:
#   CORTEX_WORKER_PORT — Worker port (default: 37778)
#   CORTEX_WORKER_API_KEY — Required bearer token for POST endpoints

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${CORTEX_PYTHON:-python3}"
WORKER_PORT="${CORTEX_WORKER_PORT:-37778}"
WORKER_URL="http://127.0.0.1:$WORKER_PORT"
AUTH_KEY="${CORTEX_WORKER_API_KEY:-}"

# Read stdin (Claude Code sends JSON)
INPUT_JSON=$(cat)

# Skip if worker isn't running
if ! curl -s --connect-timeout 1 "$WORKER_URL/api/health" > /dev/null 2>&1; then
    exit 0
fi

# Extract fields from stdin JSON
TOOL_NAME=$(echo "$INPUT_JSON" | jq -r '.tool_name // empty' 2>/dev/null)
SESSION_ID=$(echo "$INPUT_JSON" | jq -r '.session_id // empty' 2>/dev/null)

# Skip if no tool name (nothing useful to capture)
if [ -z "$TOOL_NAME" ]; then
    exit 0
fi

if [ -z "$AUTH_KEY" ]; then
    echo "Warning: CORTEX_WORKER_API_KEY is not set; skipping Cortex observation capture." >&2
    exit 0
fi

SID="${SESSION_ID:-$(date +%Y%m%d-%H%M%S)}"

# Extract and truncate input/output
TOOL_INPUT=$(echo "$INPUT_JSON" | jq -r '.tool_input // empty' 2>/dev/null | head -c 4000)
TOOL_OUTPUT=$(echo "$INPUT_JSON" | jq -r '.tool_output // empty' 2>/dev/null | head -c 8000)

# Send to worker (fire-and-forget, max 2s timeout)
curl -s --max-time 2 \
    -X POST "$WORKER_URL/api/observations" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $AUTH_KEY" \
    -d "$(jq -n \
        --arg sid "$SID" \
        --arg tool "$TOOL_NAME" \
        --arg input "$TOOL_INPUT" \
        --arg output "$TOOL_OUTPUT" \
        '{
            session_id: $sid,
            source: "post_tool_use",
            tool_name: $tool,
            agent: "main",
            raw_input: $input,
            raw_output: $output
        }'
    )" > /dev/null 2>&1 &

exit 0
