#!/bin/bash
# Cortex UserPromptSubmit Hook — captures user intent
#
# Called by Claude Code when the user submits a prompt.
# Claude Code pipes JSON to stdin with prompt and session_id.
# Registers the session (if new) and logs the user's prompt.

WORKER_URL="http://127.0.0.1:37778"
AUTH_KEY="${CORTEX_WORKER_API_KEY:-cortex-local-2026}"

# Read stdin (Claude Code sends JSON)
INPUT_JSON=$(cat)

# Skip if worker isn't running
if ! curl -s --connect-timeout 1 "$WORKER_URL/api/health" > /dev/null 2>&1; then
    echo "Success"
    exit 0
fi

# Extract fields from stdin JSON
PROMPT=$(echo "$INPUT_JSON" | jq -r '.prompt // empty' 2>/dev/null)
SESSION_ID=$(echo "$INPUT_JSON" | jq -r '.session_id // empty' 2>/dev/null)

SID="${SESSION_ID:-$(date +%Y%m%d-%H%M%S)}"

# Register/update session with user prompt
curl -s --max-time 2 \
    -X POST "$WORKER_URL/api/sessions/start" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $AUTH_KEY" \
    -d "$(jq -n \
        --arg sid "$SID" \
        --arg prompt "$PROMPT" \
        '{
            session_id: $sid,
            agent: "main",
            user_prompt: $prompt
        }'
    )" > /dev/null 2>&1 &

# Also log as observation for searchability
if [ -n "$PROMPT" ]; then
    curl -s --max-time 2 \
        -X POST "$WORKER_URL/api/observations" \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer $AUTH_KEY" \
        -d "$(jq -n \
            --arg sid "$SID" \
            --arg prompt "$PROMPT" \
            '{
                session_id: $sid,
                source: "user_prompt",
                agent: "main",
                raw_input: $prompt
            }'
        )" > /dev/null 2>&1 &
fi

echo "Success"
exit 0
