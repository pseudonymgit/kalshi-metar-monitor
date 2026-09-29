#!/bin/bash
# METAR 30-min cron verifier (R/T 2026-09-29).
# Evidence for Dan: prove the container cron fires >=3 consecutive cycles at
# ~30-min spacing, with fresh ingestion timestamps in metar_backfill.db.
#
# NOTE: ingestion_timestamp_utc is written per-observation. A cron fire shows up
# as a NEW distinct substring(ingestion_timestamp_utc,1,16) bucket. We sample
# every 60s, record the bucket, and stop after 4 distinct buckets (3 gaps).
set -u
cd /home/node/.openclaw/workspace/prototypes/weather-engine-source
OUT="data/logs/metar_cron_verification.txt"
PY=".venv-gefs/bin/python"
[ -x "$PY" ] || PY="python3"

echo "=== METAR cron verification started $(date -Iseconds) ===" > "$OUT"
prev=""
distinct=0
deadline=$(( $(date +%s) + 10800 ))   # 3h hard cap
while [ "$(date +%s)" -lt "$deadline" ]; do
    cur=$("$PY" - <<'PYEOF'
import sqlite3
c=sqlite3.connect('file:data/metar_backfill.db?mode=ro',uri=True)
r=c.execute("SELECT MAX(substr(ingestion_timestamp_utc,1,16)) FROM metar_observations").fetchone()
print(r[0] if r and r[0] else "")
PYEOF
)
    if [ -n "$cur" ] && [ "$cur" != "$prev" ]; then
        prev="$cur"
        distinct=$((distinct+1))
        n=$("$PY" - <<PYEOF
import sqlite3
c=sqlite3.connect('file:data/metar_backfill.db?mode=ro',uri=True)
print(c.execute("SELECT COUNT(*) FROM metar_observations WHERE substr(ingestion_timestamp_utc,1,16)='$cur'").fetchone()[0])
PYEOF
)
        echo "$(date -Iseconds)  ingestion_bucket=$cur  rows_in_bucket=$n  (cycle $distinct)" >> "$OUT"
        if [ "$distinct" -ge 4 ]; then
            echo "=== PASS: 4 distinct ingestion buckets observed (>=3 consecutive cycles) ===" >> "$OUT"
            exit 0
        fi
    fi
    sleep 60
done
echo "=== INCOMPLETE: only $distinct cycles in 3h window ===" >> "$OUT"
