#!/bin/bash
# manage_memu.sh - Start, stop, or check status of local memU instance

cd "$(dirname "$0")"

export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

ACTION=$1
RUNTIME_DIR=".state/runtime"
PID_FILE="$RUNTIME_DIR/memu.pid"
LOG_FILE="$RUNTIME_DIR/memu_api.log"
STATE_EVENTS_RUNTIME_DIR="$RUNTIME_DIR/state-events"
MEMU_LOG_RUNTIME_DIR="$RUNTIME_DIR/logs"
SECRET_MEMU_KEY_FILE="$HOME/.openclaw/secrets/memu_api_key"

mkdir -p "$RUNTIME_DIR" "$STATE_EVENTS_RUNTIME_DIR" "$MEMU_LOG_RUNTIME_DIR"

load_runtime_env() {
    if [ -f .env ]; then
        source .env
    fi
    if [ -f "$SECRET_MEMU_KEY_FILE" ] && { [ -z "${MEMU_API_KEY:-}" ] || [ "${MEMU_API_KEY}" = "memu-dev-key" ]; }; then
        MEMU_API_KEY="$(tr -d '\r\n' < "$SECRET_MEMU_KEY_FILE")"
        export MEMU_API_KEY
    fi
}

function check_status() {
    if curl -s -m 2 http://localhost:8000/health > /dev/null; then
        return 0
    else
        return 1
    fi
}

function start_memu() {
    if check_status; then
        echo "memU is already running on port 8000."
        exit 0
    fi
    echo "Starting memU..."
    load_runtime_env
    export UV_PROJECT_ENVIRONMENT=.venv
    export MEMU_LOG_DIR="${MEMU_LOG_DIR:-$MEMU_LOG_RUNTIME_DIR}"
    export STATE_EVENTS_EVIDENCE_PATH="${STATE_EVENTS_EVIDENCE_PATH:-$STATE_EVENTS_RUNTIME_DIR/live-emits.jsonl}"
    nohup uv run uvicorn memu.api:app --host 0.0.0.0 --port 8000 > "$LOG_FILE" 2>&1 &
    echo $! > $PID_FILE
    sleep 3
    if check_status; then
        echo "memU started successfully."
    else
        echo "Failed to start memU. Check $LOG_FILE"
        exit 1
    fi
}

function stop_memu() {
    if [ -f $PID_FILE ]; then
        kill $(cat $PID_FILE) 2>/dev/null || true
        rm -f $PID_FILE
    fi
    # fallback to killing by port
    lsof -ti:8000 | xargs kill -9 2>/dev/null || true
    echo "memU stopped."
}

case $ACTION in
    start)
        start_memu
        ;;
    stop)
        stop_memu
        ;;
    restart)
        stop_memu
        sleep 1
        start_memu
        ;;
    status)
        if check_status; then
            echo "memU is RUNNING."
            exit 0
        else
            echo "memU is DOWN."
            exit 1
        fi
        ;;
    watchdog)
        if ! check_status; then
            echo "memU is down. Restarting..."
            start_memu
        fi
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|watchdog}"
        exit 1
esac
