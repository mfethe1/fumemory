#!/bin/bash
# manage_memu.sh - Start, stop, or check status of local memU instance

ACTION=$1
PID_FILE="memu.pid"

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
    source .env || true
    export UV_PROJECT_ENVIRONMENT=.venv
    nohup uv run uvicorn memu.api:app --host 0.0.0.0 --port 8000 > memu_api.log 2>&1 &
    echo $! > $PID_FILE
    sleep 3
    if check_status; then
        echo "memU started successfully."
    else
        echo "Failed to start memU. Check memu_api.log"
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
