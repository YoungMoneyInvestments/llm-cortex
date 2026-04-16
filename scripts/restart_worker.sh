#!/usr/bin/env bash
# restart_worker.sh — Canonical post-deploy step for llm-cortex code changes.
#
# The Cortex Memory Worker is managed by launchd (com.cortex.memory-worker).
# After any code change in llm-cortex, run this script to reload the live worker.
#
# Usage:
#   ./scripts/restart_worker.sh
#
# What it does:
#   1. Checks for concurrent CC cleanup jobs (safe-restart guard).
#   2. Captures before-PID from launchctl.
#   3. Issues `launchctl kickstart -k` (sends SIGTERM, waits for exit, then starts
#      fresh). The worker's KeepAlive=SuccessfulExit=false ensures launchd respects
#      the kickstart rather than immediately relaunching on SIGTERM.
#   4. Polls /api/health every 0.5 s, up to 10 s.
#   5. Reports before/after PID and health status.
#
# Idempotent: safe to run while a request is in-flight.
# Hard limit: never leaves the worker down > 10 seconds.

set -euo pipefail

LAUNCHD_LABEL="com.cortex.memory-worker"
WORKER_URL="http://127.0.0.1:37778"
HEALTH_TIMEOUT=10          # max seconds to wait for health after restart
POLL_INTERVAL="0.5"        # seconds between health polls

# ── Safety guard: abort if a CC cleanup job is running ───────────────────────
if pgrep -fl "cleanup_ner_false_positives|cleanup_memcell_orphans" >/dev/null 2>&1; then
    echo "ERROR: CC cleanup job is running. Do not restart during an active DB transaction."
    echo "       Wait for cleanup to finish, then re-run this script."
    exit 1
fi

# ── Capture before-PID ───────────────────────────────────────────────────────
BEFORE_PID=$(launchctl list | awk -v label="$LAUNCHD_LABEL" '$3 == label {print $1}')
if [[ -z "$BEFORE_PID" || "$BEFORE_PID" == "-" ]]; then
    echo "WARNING: Worker was not running before restart (PID = ${BEFORE_PID:-none})."
else
    echo "Before: worker PID = $BEFORE_PID"
fi

# ── Kickstart (graceful restart via launchd) ─────────────────────────────────
echo "Issuing: launchctl kickstart -k gui/$(id -u)/$LAUNCHD_LABEL"
launchctl kickstart -k "gui/$(id -u)/$LAUNCHD_LABEL"

# ── Wait for health ──────────────────────────────────────────────────────────
ELAPSED=0
STEP=0.5
echo "Waiting for /api/health (timeout: ${HEALTH_TIMEOUT}s)..."
while (( $(echo "$ELAPSED < $HEALTH_TIMEOUT" | bc -l) )); do
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 1 "$WORKER_URL/api/health" 2>/dev/null || true)
    if [[ "$HTTP_CODE" == "200" ]]; then
        break
    fi
    sleep "$STEP"
    ELAPSED=$(echo "$ELAPSED + $STEP" | bc -l)
done

# ── Capture after-PID ────────────────────────────────────────────────────────
AFTER_PID=$(launchctl list | awk -v label="$LAUNCHD_LABEL" '$3 == label {print $1}')

# ── Final report ─────────────────────────────────────────────────────────────
echo ""
echo "=== Restart Report ==="
echo "  Before PID : ${BEFORE_PID:-none}"
echo "  After PID  : ${AFTER_PID:-none}"

if [[ "$HTTP_CODE" == "200" ]]; then
    HEALTH=$(curl -s --connect-timeout 2 "$WORKER_URL/api/health" 2>/dev/null)
    echo "  Health     : OK (HTTP 200)"
    echo "  Response   : $HEALTH"
    echo ""
    echo "Restart complete. Worker is healthy."
else
    echo "  Health     : FAILED (HTTP ${HTTP_CODE:-timeout} after ${HEALTH_TIMEOUT}s)"
    echo ""
    echo "ERROR: Worker did not become healthy within ${HEALTH_TIMEOUT}s."
    echo "       Check logs: ~/.openclaw/logs/memory-worker-stderr.log"
    exit 1
fi
