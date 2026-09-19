#!/usr/bin/env bash
# Parallel backfill of missing 1-min ASOS stations.
# Deterministic, script-only. Run 5 stations concurrently.
set -u

cd "$(dirname "$0")/.."

MISSING="KDFW KHOU KLAS KLAX KMDW KMIA KMSP KMSY KNYC KOKC KPHL KPHX KSAT KSEA KSFO"
START="${1:-2024-01-01}"
END="${2:-2026-09-18}"
PARALLEL="${3:-5}"

echo "Parallel 1-min backfill: ${MISSING}"
echo "Range ${START} .. ${END}, parallelism ${PARALLEL}"

# Emit one line per station; xargs -P runs up to PARALLEL concurrently.
# Each process logs to its own per-station file (the collect script itself
# also appends to data/iem_asos_1min_collect.log via its FileHandler).
export START END
printf '%s\n' $MISSING | xargs -P "$PARALLEL" -I {} sh -c \
  'python3 scripts/iem_asos_1min_collect.py \
     --mode backfill --start "$START" --end "$END" --stations "$1" \
     > "data/parallel_backfill_$1.log" 2>&1' _ {}

echo "All parallel backfill processes done."
