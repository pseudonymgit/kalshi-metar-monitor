#!/bin/bash
# Daemon Auto-Restart Watchdog
# Checks if intraday daemon is running. If not, restarts it.
# Run every 5 minutes via cron.

PID_FILE="/tmp/intraday-daemon.pid"
DAEMON_SCRIPT="scripts/intraday_daemon.py"
WORK_DIR="/home/node/.openclaw/workspace/prototypes/weather-engine-source"
LOG_FILE="${WORK_DIR}/logs/daemon_watchdog.log"

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') [OK] Daemon running (pid=$PID)" >> "$LOG_FILE"
        exit 0  # Daemon running
    fi
fi

# Daemon not running — restart
cd "$WORK_DIR" || {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [ERROR] Cannot change to WORK_DIR" >> "$LOG_FILE"
    exit 1
}

if [ ! -f "$DAEMON_SCRIPT" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [ERROR] Daemon script not found: $DAEMON_SCRIPT" >> "$LOG_FILE"
    exit 1
fi

mkdir -p logs
nohup python3 "$DAEMON_SCRIPT" --interval-min 5 > logs/intraday_daemon.log 2>&1 &
NEW_PID=$!
echo "$(date '+%Y-%m-%d %H:%M:%S') [RESTART] Daemon restarted: PID=$NEW_PID" >> "$LOG_FILE"
echo "$NEW_PID" > "$PID_FILE"
exit 0