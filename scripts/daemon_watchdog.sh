#!/bin/bash
# Daemon Auto-Restart Watchdog
# Default:     checks if the intraday daemon is running; restarts it if not.
#              Run every 5 minutes via cron.
# --check-only: liveness check with NO restart authority. Exit 0 if alive,
#              exit 1 if dead. Used for the D2 demotion path where a host-level
#              supervisor owns restart (see
#              docs/plans/DUAL-SUPERVISOR-CONSOLIDATION-2026-10-03.md §3.3).

PID_FILE="/tmp/intraday-daemon.pid"
DAEMON_SCRIPT="scripts/intraday_daemon.py"
WORK_DIR="/home/node/.openclaw/workspace/prototypes/weather-engine-source"
LOG_FILE="${WORK_DIR}/logs/daemon_watchdog.log"

# P2-D (2026-09-28): restart rate-limit. Without this, a daemon that dies on
# startup was respawned every 5 min = up to 288 restarts/day, masking the real
# failure and spamming logs. Allow at most MAX_RESTARTS within WINDOW seconds;
# beyond that, back off and alert loudly instead of hot-looping.
MAX_RESTARTS=6
WINDOW=3600
RATE_FILE="/tmp/intraday-daemon.restarts"

CHECK_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --check-only) CHECK_ONLY=1 ;;
        *)
            echo "[watchdog] unknown argument: $arg" >&2
            exit 2
            ;;
    esac
done

# Print the PID from the PID file (empty/fail if absent).
daemon_pid() {
    [ -f "$PID_FILE" ] || return 1
    local pid
    pid=$(cat "$PID_FILE" 2>/dev/null)
    [ -n "$pid" ] || return 1
    printf '%s' "$pid"
}

# Alive iff the PID file names a live process.
daemon_alive() {
    local pid
    pid=$(daemon_pid) || return 1
    kill -0 "$pid" 2>/dev/null
}

# ── --check-only: pure liveness/observability, NO restart authority ──────────
# Emits a clear DEAD line to stdout so the cron announce / failureAlert surfaces
# it. This is what the container WE-daemon-watchdog cron becomes after demotion.
if [ "$CHECK_ONLY" -eq 1 ]; then
    if daemon_alive; then
        PID=$(daemon_pid)
        echo "$(date '+%Y-%m-%d %H:%M:%S') [OK] Daemon running (pid=$PID)" >> "$LOG_FILE"
        echo "[watchdog] OK: daemon alive (pid=$PID)"
        exit 0
    fi
    echo "$(date '+%Y-%m-%d %H:%M:%S') [DEAD] daemon not running (check-only; no restart authority)" >> "$LOG_FILE"
    echo "[watchdog] DEAD: daemon not running — host supervisor must restart it (check-only job has no restart authority)."
    exit 1
fi

alert_stuck() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [STUCK] $1 — restart rate limited ($MAX_RESTARTS/${WINDOW}s). MANUAL INTERVENTION NEEDED." >> "$LOG_FILE"
    # Make it visible in the watchdog's own stdout so cron announce surfaces it.
    echo "[watchdog] STUCK: $1 — rate limited; daemon not restarting. Manual intervention needed."
}

now=$(date +%s)
if [ -f "$RATE_FILE" ]; then
    recent=$(awk -v now="$now" -v win="$WINDOW" '$1 > now-win' "$RATE_FILE")
else
    recent=""
fi
n_recent=$(printf '%s\n' "$recent" | grep -c '[0-9]' || true)
if [ "$n_recent" -ge "$MAX_RESTARTS" ]; then
    alert_stuck "exceeded restart budget"
    exit 1
fi

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
# Record this restart for the rate limiter.
echo "$now" >> "$RATE_FILE"
exit 0
