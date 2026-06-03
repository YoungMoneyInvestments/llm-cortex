#!/bin/bash
# Cortex UserPromptSubmit Hook — captures user intent
#
# Called by Claude Code when the user submits a prompt.
# Claude Code pipes JSON to stdin with prompt and session_id.
# Registers the session (if new) and logs the user's prompt.
#
# Configure:
#   CORTEX_WORKER_PORT — Worker port (default: 37778)
#   CORTEX_WORKER_API_KEY — Required bearer token for POST endpoints

WORKER_PORT="${CORTEX_WORKER_PORT:-37778}"
WORKER_URL="http://127.0.0.1:$WORKER_PORT"
AUTH_KEY="${CORTEX_WORKER_API_KEY:-}"
AGENT_NAME="${CORTEX_AGENT_NAME:-claude-code}"
CURL="${CORTEX_CURL:-/usr/bin/curl}"
JQ="${CORTEX_JQ:-$(command -v jq || true)}"
if [ -z "$JQ" ]; then
    JQ="/opt/homebrew/bin/jq"
fi
# Fall back to generated key file if env var is absent.
if [ -z "$AUTH_KEY" ] && [ -f "$HOME/.cortex/data/.worker_api_key" ]; then
    AUTH_KEY="$(cat "$HOME/.cortex/data/.worker_api_key" 2>/dev/null)"
fi

# Read stdin (Claude Code sends JSON)
INPUT_JSON=$(cat)

# Skip if worker isn't running
if ! "$CURL" -s --connect-timeout 1 "$WORKER_URL/api/health" > /dev/null 2>&1; then
    echo "Success"
    exit 0
fi

# Extract fields from stdin JSON
PROMPT=$(echo "$INPUT_JSON" | "$JQ" -r '.prompt // .user_prompt // .message // .input // empty' 2>/dev/null)
SESSION_ID=$(echo "$INPUT_JSON" | "$JQ" -r '.session_id // .sessionId // .session.id // empty' 2>/dev/null)

if [ -z "$AUTH_KEY" ]; then
    echo "Warning: CORTEX_WORKER_API_KEY is not set; skipping Cortex session capture." >&2
    echo "Success"
    exit 0
fi

SID="${SESSION_ID:-$(date +%Y%m%d-%H%M%S)}"

# Register/update session with user prompt
"$CURL" -s --max-time 2 \
    -X POST "$WORKER_URL/api/sessions/start" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $AUTH_KEY" \
    -d "$("$JQ" -n \
        --arg sid "$SID" \
        --arg agent "$AGENT_NAME" \
        --arg prompt "$PROMPT" \
        '{
            session_id: $sid,
            agent: $agent,
            user_prompt: $prompt
        }'
    )" > /dev/null 2>&1 &

# Also log as observation for searchability
if [ -n "$PROMPT" ]; then
    "$CURL" -s --max-time 2 \
        -X POST "$WORKER_URL/api/observations" \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer $AUTH_KEY" \
        -d "$("$JQ" -n \
            --arg sid "$SID" \
            --arg agent "$AGENT_NAME" \
            --arg prompt "$PROMPT" \
            '{
                session_id: $sid,
                source: "user_prompt",
                agent: $agent,
                raw_input: $prompt
            }'
        )" > /dev/null 2>&1 &
fi

echo "Success"
exit 0
