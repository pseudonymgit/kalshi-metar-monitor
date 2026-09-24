#!/usr/bin/env python3
"""GEFS daily wrapper — bounded daily top-up of data/gefs_archive.db.

Two stages:
  1. FETCH: scripts/gefs_ensemble_collect.py pulls the 31-member GEFS ensemble
     forecast from Open-Meteo and writes flat rows to nwp_forecasts.db
     (model='gefs_ens'). This is the upstream producer that was missing — the
     wrapper previously only transformed already-fetched rows, so GEFS went
     stale as soon as manual backfills stopped.
  2. POPULATE: scripts/populate_gefs_archive.py converts those rows into the
     gefs_archive blob format.

Exit codes: 0 ok, 1 fetch or populate failed, 2 archive missing.
"""
import datetime
import os
import sqlite3
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(REPO, 'data', 'gefs_archive.db')
COLLECT = os.path.join(REPO, 'scripts', 'gefs_ensemble_collect.py')
POPULATE = os.path.join(REPO, 'scripts', 'populate_gefs_archive.py')

if not os.path.exists(DB):
    print(f'[gefs-daily] ERROR: archive missing: {DB}')
    sys.exit(2)

# ── Stage 1: FETCH fresh ensemble data (the missing producer) ────────────────
print('[gefs-daily] stage 1: fetching GEFS ensemble from Open-Meteo...')
fetch = subprocess.run([sys.executable, COLLECT], cwd=REPO)
if fetch.returncode != 0:
    print(f'[gefs-daily] ALERT: ensemble fetch exited {fetch.returncode}')
    sys.exit(1)
print('[gefs-daily] stage 1 complete')

# ── Stage 2: POPULATE the archive from nwp_forecasts.db ──────────────────────
today = datetime.date.today()
con = sqlite3.connect(DB)
max_date = con.execute('SELECT MAX(target_date) FROM gefs_archive WHERE step=24').fetchone()[0]
n_max = con.execute('SELECT COUNT(*) FROM gefs_archive WHERE target_date = ?', (max_date,)).fetchone()[0]
con.close()

if max_date is None:
    print('[gefs-daily] ERROR: archive empty')
    sys.exit(2)

gap_days = (today - datetime.date.fromisoformat(max_date)).days
print(f'[gefs-daily] archive through {max_date} ({n_max} rows on last date); gap = {gap_days}d')

print('[gefs-daily] stage 2: populating archive...')
pop = subprocess.run([sys.executable, POPULATE], cwd=REPO)
if pop.returncode != 0:
    print(f'[gefs-daily] ALERT: populate exited {pop.returncode}')
    sys.exit(1)

print('[gefs-daily] complete')
sys.exit(0)
