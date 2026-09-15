#!/usr/bin/env python3
'''GEFS daily backfill wrapper — bounded daily top-up of data/gefs_archive.db.'''
import datetime, os, sqlite3, sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(REPO, 'data', 'gefs_archive.db')
POPULATE = os.path.join(REPO, 'scripts', 'populate_gefs_archive.py')

today = datetime.date.today()
yesterday = today - datetime.timedelta(days=1)

if not os.path.exists(DB):
    print(f'[gefs-daily] ERROR: archive missing: {DB}')
    sys.exit(2)

con = sqlite3.connect(DB)
max_date = con.execute('SELECT MAX(target_date) FROM gefs_archive WHERE step=24').fetchone()[0]
n_max = con.execute('SELECT COUNT(*) FROM gefs_archive WHERE target_date = ?', (max_date,)).fetchone()[0]
con.close()

if max_date is None:
    print('[gefs-daily] ERROR: archive empty')
    sys.exit(2)

gap_days = (today - datetime.date.fromisoformat(max_date)).days
print(f'[gefs-daily] archive through {max_date} ({n_max} rows on last date); gap = {gap_days}d')

if gap_days <= 0:
    print('[gefs-daily] up to date — nothing to do')
    sys.exit(0)

rc = os.system(f'{sys.executable} {POPULATE}')

if rc != 0:
    print(f'[gefs-daily] ALERT: backfill exited {rc}')
    sys.exit(1)

print('[gefs-daily] backfill complete')
sys.exit(0)