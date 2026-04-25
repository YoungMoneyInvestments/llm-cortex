#!/usr/bin/env bash
# watchdog.sh — Auto-restart cortex memory worker when health endpoint stops responding.
#
# Detects both crash (connection refused) and deadlock (empty reply / timeout).
# Called every 5 minutes by com.cortex.watchdog LaunchAgent.
# Delegates to restart_worker.sh which guards against active DB cleanup jobs.

set -euo pipefail

WORKER_URL="http://127.0.0.1:37778"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$HOME/.openclaw/logs/watchdog.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 --connect-timeout 3 \
    "$WORKER_URL/api/health" 2>/dev/null || true)

if [[ "$HTTP_CODE" == "200" ]]; then
    exit 0
fi

log "Health check failed (HTTP=${HTTP_CODE:-empty}). Triggering restart."

# restart_worker.sh guards against active CC cleanup jobs and polls until healthy
if bash "$SCRIPT_DIR/restart_worker.sh" >> "$LOG" 2>&1; then
    log "Restart successful."
else
    log "ERROR: Restart failed. Check memory-worker-stderr.log."
fi
