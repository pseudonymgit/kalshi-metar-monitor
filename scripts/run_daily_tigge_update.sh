#!/bin/bash
# Daily TIGGE ECMWF ensemble backfill — runs in background, logs to file
set -e
cd /home/node/.openclaw/workspace/prototypes/weather-engine-source
LOG="data/logs/tigge_daily_$(date +%Y%m%d_%H%M%S).log"
mkdir -p data/logs

# ITEM-3 FIX (2026-09-28):
#  (1) bare `python3` lacks numpy/xarray/cdsapi -> the job silently failed.
#  (2) `--end $(date +%Y-%m-%d)` asked for TODAY, but TIGGE publishes with a ~2-day
#      embargo; requesting embargoed days makes ECDS return partial/empty and the
#      run appears to succeed while doing nothing. Cap the end at today-2 to match
#      the embargo (same rule as commit 083764f3f).
#  (3) route through the preflight wrapper so a broken env fails LOUDLY.
PY=".venv-gefs/bin/python"
[ -x "$PY" ] || PY="python3"
END="$(date -d '2 days ago' +%Y-%m-%d)"

echo "[$(date -Iseconds)] Starting TIGGE daily backfill (end=$END, embargo-respecting)..." >> "$LOG"
bash scripts/we_cron_preflight.sh "$PY" scripts/ecds_sequential_backfill.py \
  --start "$(date -d '2 months ago' +%Y-%m-%d)" \
  --end "$END" \
  --batch-days 3 2>&1 >> "$LOG"
echo "[$(date -Iseconds)] Finished. Exit code: $?" >> "$LOG"
# Report summary
echo "=== SUMMARY ===" >> "$LOG"
"$PY" -c "
import sqlite3
conn = sqlite3.connect('data/tigge_archive.db')
conn.row_factory = sqlite3.Row
total = conn.execute('SELECT COUNT(*) FROM tigge_archive').fetchone()[0]
dates = conn.execute('SELECT COUNT(DISTINCT target_date) FROM tigge_archive').fetchone()[0]
stations = conn.execute('SELECT COUNT(DISTINCT station) FROM tigge_archive').fetchone()[0]
print(f'Total rows: {total}, Dates: {dates}, Stations: {stations}')
rows = conn.execute('''SELECT station, MAX(target_date) as latest FROM tigge_archive GROUP BY station ORDER BY station''').fetchall()
for r in rows:
    print(f'  {r[\"station\"]}: latest={r[\"latest\"]}')
" >> "$LOG"
echo "Log: $LOG"