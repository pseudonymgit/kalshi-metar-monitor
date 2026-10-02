#!/bin/bash
# Durable watcher: wait for the TIGGE re-backfill PID, then run the verifier.
# Usage: watch_tigge_rebackfill.sh <pid> <backfill_log> <before_json>
set -u
PID="${1:?pid required}"
LOG="${2:?log required}"
BEFORE="${3:?before json required}"
REPO="/home/node/.openclaw/workspace/prototypes/weather-engine-source"
cd "$REPO" || exit 1
REPORT="data/tigge_repair/after_report_$(date -u +%Y%m%dT%H%M%SZ).txt"

# Wait for the backfill process to exit (poll every 60s, no busy loop).
while kill -0 "$PID" 2>/dev/null; do sleep 60; done

{
  echo "=== TIGGE re-backfill finished: $(date -u -Iseconds) ==="
  echo "--- last 20 lines of backfill log ---"
  tail -20 "$LOG"
  echo
  echo "--- verifier ---"
  .venv-gefs/bin/python scripts/verify_tigge_rebackfill.py --before "$BEFORE"
} > "$REPORT" 2>&1

echo "[$(date -u -Iseconds)] watcher done -> $REPORT" >> data/tigge_repair/watcher.log
