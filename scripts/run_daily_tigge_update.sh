#!/bin/bash
# Daily TIGGE ECMWF ensemble backfill — runs in background, logs to file
set -e
cd /home/node/.openclaw/workspace/prototypes/weather-engine-source
LOG="data/logs/tigge_daily_$(date +%Y%m%d_%H%M%S).log"
mkdir -p data/logs
echo "[$(date -Iseconds)] Starting TIGGE daily backfill..." >> "$LOG"
PYTHONUNBUFFERED=1 python3 scripts/ecds_sequential_backfill.py \
  --start "$(date -d '2 months ago' +%Y-%m-%d)" \
  --end "$(date +%Y-%m-%d)" \
  --batch-days 3 2>&1 >> "$LOG"
echo "[$(date -Iseconds)] Finished. Exit code: $?" >> "$LOG"
# Report summary
echo "=== SUMMARY ===" >> "$LOG"
python3 -c "
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