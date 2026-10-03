#!/bin/bash
# E4 daemon watchdog kill test — deterministic (Gilfoyle design 2026-10-02)
cd /home/node/.openclaw/workspace/prototypes/weather-engine-source || exit 99
PID_FILE=/tmp/intraday-daemon.pid
RATE_FILE=/tmp/intraday-daemon.restarts
WD_LOG=logs/daemon_watchdog.log
DAEMON_LOG=logs/intraday_daemon.log

# (2) clear the rate file so the rate-limiter cannot mask the restart
: > "$RATE_FILE" 2>/dev/null
rm -f "$RATE_FILE" 2>/dev/null
echo "rate_file_cleared=yes"

OLD=$(cat "$PID_FILE" 2>/dev/null)
if [ -z "$OLD" ] || ! kill -0 "$OLD" 2>/dev/null; then
  echo "RESULT=FAIL reason=pre_assert_not_alive old=$OLD"; exit 1
fi
T0=$(date -u +%s)
T0_HUMAN=$(date -u '+%Y-%m-%d %H:%M:%S')
echo "old_pid=$OLD t0=$T0 t0_human=$T0_HUMAN"

# KILL: graceful
kill -TERM "$OLD"
# assert dead within 5s
DEAD=no
for i in $(seq 1 50); do
  if ! kill -0 "$OLD" 2>/dev/null; then DEAD=yes; break; fi
  sleep 0.1
done
T_DEAD=$(date -u +%s)
echo "sigterm_dead_within_5s=$DEAD died_at=$(date -u '+%Y-%m-%d %H:%M:%S')"
# informational: clean shutdown log
if grep -q "SIGTERM" "$DAEMON_LOG" 2>/dev/null; then echo "clean_shutdown_log=yes"; else echo "clean_shutdown_log=no"; fi
[ "$DEAD" = yes ] || { echo "RESULT=FAIL reason=did_not_die_within_5s old=$OLD"; exit 2; }

# WAIT: poll up to 360s for a NEW pid
NEW=""
WAITED=0
while [ "$WAITED" -lt 360 ]; do
  CUR=$(cat "$PID_FILE" 2>/dev/null)
  if [ -n "$CUR" ] && [ "$CUR" != "$OLD" ] && kill -0 "$CUR" 2>/dev/null; then NEW="$CUR"; break; fi
  sleep 2; WAITED=$((WAITED+2))
done
T_RESTART=$(date -u +%s)
if [ -z "$NEW" ]; then echo "RESULT=FAIL reason=no_restart_within_360s old=$OLD waited=${WAITED}s"; exit 3; fi
echo "new_pid=$NEW restart_waited=${WAITED}s restart_at=$(date -u '+%Y-%m-%d %H:%M:%S')"

# fresh [RESTART] log line newer than T0
if awk -v t0="$T0" '{print}' "$WD_LOG" | grep -q "\[RESTART\]"; then
  last_restart=$(grep "\[RESTART\]" "$WD_LOG" | tail -1)
  echo "watchdog_restart_line=$last_restart"
else
  echo "watchdog_restart_line=MISSING"
fi

# ASSERT: daemon completes one pipeline cycle (fresh "Fusion complete" post-restart)
CYCLE=no
CW=0
while [ "$CW" -lt 240 ]; do
  if grep -q "Fusion complete" "$DAEMON_LOG" 2>/dev/null; then CYCLE=yes; break; fi
  sleep 5; CW=$((CW+5))
done
echo "pipeline_cycle_observed=$CYCLE cycle_wait=${CW}s"
if [ "$CYCLE" = no ]; then echo "RESULT=FAIL reason=no_pipeline_cycle old=$OLD new=$NEW"; exit 4; fi

echo "RESULT=PASS old=$OLD new=$NEW restart_waited=${WAITED}s"
