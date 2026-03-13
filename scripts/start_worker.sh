#!/bin/bash
# Cortex Worker Launcher — starts/stops the background memory worker
#
# Usage:
#   ./start_worker.sh          # Start (if not already running)
#   ./start_worker.sh stop     # Stop gracefully
#   ./start_worker.sh status   # Check status
#   ./start_worker.sh restart  # Stop + start
#
# Configure:
#   CORTEX_DATA_DIR   — Runtime data dir (default: ~/.cortex/data)
#   CORTEX_LOG_DIR    — Runtime log dir (default: ~/.cortex/logs)
#   CORTEX_PID_FILE   — Worker PID file (default: ~/.cortex/worker.pid)
#   CORTEX_WORKER_PORT — HTTP port (default: 37778)
#   CORTEX_PYTHON     — Python interpreter (default: python3)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${CORTEX_PYTHON:-python3}"
RUNTIME_HOME="${CORTEX_RUNTIME_HOME:-$HOME/.cortex}"
DATA_DIR="${CORTEX_DATA_DIR:-$RUNTIME_HOME/data}"
LOG_DIR="${CORTEX_LOG_DIR:-$RUNTIME_HOME/logs}"
WORKER_SCRIPT="${CORTEX_WORKER_SCRIPT:-$SCRIPT_DIR/../src/memory_worker.py}"
PID_FILE="${CORTEX_PID_FILE:-$RUNTIME_HOME/worker.pid}"
LOG_FILE="$LOG_DIR/memory-worker.log"
WORKER_PORT="${CORTEX_WORKER_PORT:-37778}"
WORKER_URL="http://127.0.0.1:$WORKER_PORT"

mkdir -p "$(dirname "$PID_FILE")"
mkdir -p "$(dirname "$LOG_FILE")"
mkdir -p "$DATA_DIR"

is_running() {
    if [ -f "$PID_FILE" ]; then
        pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            return 0
        else
            # Stale PID file
            rm -f "$PID_FILE"
            return 1
        fi
    fi
    return 1
}

start_worker() {
    if is_running; then
        echo "Worker already running (PID $(cat "$PID_FILE"))"
        return 0
    fi

    echo "Starting Cortex Worker..."
    cd "$SCRIPT_DIR"
    CORTEX_DATA_DIR="$DATA_DIR" CORTEX_LOG_DIR="$LOG_DIR" \
        CORTEX_PID_FILE="$PID_FILE" CORTEX_WORKER_PORT="$WORKER_PORT" \
        nohup "$PYTHON" "$WORKER_SCRIPT" >> "$LOG_FILE" 2>&1 &
    local pid=$!

    # Wait briefly for startup
    sleep 1

    if kill -0 "$pid" 2>/dev/null; then
        echo "$pid" > "$PID_FILE"
        echo "Worker started (PID $pid)"

        # Verify health
        for i in 1 2 3; do
            if curl -s --connect-timeout 1 "$WORKER_URL/api/health" > /dev/null 2>&1; then
                echo "Health check passed"
                return 0
            fi
            sleep 1
        done
        echo "Warning: Worker started but health check failed"
    else
        echo "ERROR: Worker failed to start. Check $LOG_FILE"
        return 1
    fi
}

stop_worker() {
    if ! is_running; then
        echo "Worker not running"
        return 0
    fi

    local pid=$(cat "$PID_FILE")
    echo "Stopping worker (PID $pid)..."
    kill "$pid" 2>/dev/null

    # Wait for graceful shutdown (max 5s)
    for i in $(seq 1 10); do
        if ! kill -0 "$pid" 2>/dev/null; then
            rm -f "$PID_FILE"
            echo "Worker stopped"
            return 0
        fi
        sleep 0.5
    done

    # Force kill
    echo "Force killing worker..."
    kill -9 "$pid" 2>/dev/null
    rm -f "$PID_FILE"
    echo "Worker killed"
}

show_status() {
    if is_running; then
        local pid=$(cat "$PID_FILE")
        echo "Worker: RUNNING (PID $pid)"

        # Get health info
        health=$(curl -s --connect-timeout 2 "$WORKER_URL/api/health" 2>/dev/null)
        if [ -n "$health" ]; then
            echo "$health" | python3 -m json.tool 2>/dev/null || echo "$health"
        else
            echo "  (health endpoint unreachable)"
        fi
    else
        echo "Worker: STOPPED"
    fi
}

case "${1:-start}" in
    start)
        start_worker
        ;;
    stop)
        stop_worker
        ;;
    restart)
        stop_worker
        sleep 1
        start_worker
        ;;
    status)
        show_status
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac
