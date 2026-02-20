#!/bin/bash
# Cortex UserPromptSubmit Hook — captures user intent
#
# Called by Claude Code when the user submits a prompt.
# Registers the session (if new) and logs the user's prompt.
#
# Environment variables (set by Claude Code):
#   USER_PROMPT  — the user's message text
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
PROMPT="${USER_PROMPT:-}"

# Register/update session with user prompt
curl -s --max-time 2 \
    -X POST "$WORKER_URL/api/sessions/start" \
    -H "Content-Type: application/json" \
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

exit 0
