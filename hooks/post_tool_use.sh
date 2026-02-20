#!/bin/bash
# Cortex PostToolUse Hook — captures tool usage observations
#
# Called by Claude Code after every tool execution.
# Sends observation to the background worker via HTTP (fire-and-forget).
#
# Environment variables (set by Claude Code):
#   TOOL_NAME    — name of the tool that was used
#   TOOL_INPUT   — JSON input to the tool
#   TOOL_OUTPUT  — output from the tool (may be large)
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

# Use provided session ID or generate one
SID="${SESSION_ID:-$(date +%Y%m%d-%H%M%S)}"

# Truncate large outputs to avoid overwhelming the worker
MAX_INPUT=4000
MAX_OUTPUT=8000

INPUT_TRUNC=$(echo "$TOOL_INPUT" | head -c "$MAX_INPUT" 2>/dev/null || echo "")
OUTPUT_TRUNC=$(echo "$TOOL_OUTPUT" | head -c "$MAX_OUTPUT" 2>/dev/null || echo "")

# Send to worker (fire-and-forget, max 2s timeout)
curl -s --max-time 2 \
    -X POST "$WORKER_URL/api/observations" \
    -H "Content-Type: application/json" \
    -d "$(jq -n \
        --arg sid "$SID" \
        --arg tool "$TOOL_NAME" \
        --arg input "$INPUT_TRUNC" \
        --arg output "$OUTPUT_TRUNC" \
        '{
            session_id: $sid,
            source: "post_tool_use",
            tool_name: $tool,
            agent: "main",
            raw_input: $input,
            raw_output: $output
        }'
    )" > /dev/null 2>&1 &

# Don't wait for curl — return immediately
exit 0
