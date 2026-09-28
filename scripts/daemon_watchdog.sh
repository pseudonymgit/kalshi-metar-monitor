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
# Use the project venv: bare `python3` lacks numpy/scipy, which the intraday
# pipeline imports (core/intraday/fusion.py) — that was the silent restart
# failure. .venv-gefs has numpy+scipy; .venv's interpreter is broken.
PYTHON_BIN="${WORK_DIR}/.venv-gefs/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [ERROR] venv python not found: $PYTHON_BIN" >> "$LOG_FILE"
    exit 1
fi
nohup "$PYTHON_BIN" "$DAEMON_SCRIPT" --interval-min 5 > logs/intraday_daemon.log 2>&1 &
NEW_PID=$!
# Do NOT write PID_FILE here: the daemon writes its own PID on startup. Having
# the watchdog pre-write $! caused the daemon's _check_pid() to see its own
# (alive) PID and exit "already running". (WS-N fix 2026-09-27)
echo "$(date '+%Y-%m-%d %H:%M:%S') [RESTART] Daemon restarted: PID=$NEW_PID" >> "$LOG_FILE"
exit 0