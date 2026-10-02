#!/bin/bash
# Durable watcher: wait for ALL TIGGE re-backfill PIDs, then run the verifier.
# Usage: watch_tigge_rebackfill.sh <before_json> <pid1> <pid2> ...
set -u
BEFORE="${1:?before json required}"
shift
PIDS=("$@")
REPO="/home/node/.openclaw/workspace/prototypes/weather-engine-source"
cd "$REPO" || exit 1
REPORT="data/tigge_repair/after_report_$(date -u +%Y%m%dT%H%M%SZ).txt"
TS="$(date -u +%Y%m%d_%H%M%S)"

# Wait for every shard to exit (poll every 60s, no busy loop).
alive=1
while [ "$alive" -eq 1 ]; do
  alive=0
  for p in "${PIDS[@]}"; do
    if kill -0 "$p" 2>/dev/null; then alive=1; fi
  done
  [ "$alive" -eq 1 ] && sleep 60
done

{
  echo "=== TIGGE re-backfill finished: $(date -u -Iseconds) ==="
  echo "--- shard log tails ---"
  for L in $(ls -t data/logs/tigge_shard*.log 2>/dev/null | head -5); do
    echo "### $L"; tail -6 "$L"; echo
  done
  echo "--- verifier ---"
  .venv-gefs/bin/python scripts/verify_tigge_rebackfill.py --before "$BEFORE"
} > "$REPORT" 2>&1

echo "[$(date -u -Iseconds)] watcher done -> $REPORT" >> data/tigge_repair/watcher.log
