# Weather Engine — Deployment Checklist

> **Phase 4.3:** Sync SBOX ↔ PROD ↔ DEV
> **Owner:** Dan (requires Render deploy access)
> **Last updated:** 2026-07-17

## 1. Environment Overview

| Environment | Purpose | URL | Database | Status |
|---|---|---|---|---|
| **DEV** | Daily development, signal testing | Local (`PAPER_TRADING_INSTANCE=DEV`) | `paper_trading_dev.db` | Active |
| **SBOX** | Staging — pre-prod validation | Render preview | `paper_trading_sbox.db` | Active |
| **PROD** | Production — live paper trading | `https://kalshi-metar-monitor.onrender.com` | `paper_trading_prod.db` | Active |

## 2. What Needs to Be Synced

### 2.1 Calibrators
- [ ] `calibrated_pipeline.pkl` — SBOX has latest? PROD? Are they the same?
- [ ] `calibrated_pipeline_v2.pkl` — Same question
- [ ] `best_calibration_params.json` — Should be identical across environments
- [ ] `bss_cache.json` — Per-station BSS matrix; should be fresh

### 2.2 Signal Configuration
- [ ] Active signal list: `calendar_climatology`, `gaussian`, `goldilocks`, `pressure_delta`, `wind_direction_shift`, `regime`, `forecast_disagreement` (7 signals)
- [ ] Any new signals in SBOX that haven't been promoted to PROD?
- [ ] Any dead signals removed from SBOX but still in PROD config?

### 2.3 Config Files
- [ ] `instance_config.py` — Verify all envs have the same config
- [ ] `station_mapping.json` — Should be identical
- [ ] `kalshi_official_mapping.json` — Should be identical
- [ ] `.env.webhooks` — PROD has correct webhook URL
- [ ] `load_env.sh` — Verify env vars are set correctly per env

### 2.4 Database Schema
- [ ] `paper_trading*.db` — All envs have the same schema version
- [ ] `alert_state_machine.db` — Schema consistent
- [ ] `trade_journal.db` — Schema consistent
- [ ] `night_mode_digest.db` — New (Phase 4.1), only PROD initially

### 2.5 Code Changes (Phase 4)
- [ ] `core/night_mode.py` — Night mode digest queue (new)
- [ ] `core/heartbeat.py` — System health heartbeat (new)
- [ ] `core/alert_state_machine.py` — Multi-channel delivery routing (updated)
- [ ] `core/paper_trading_engine.py` — Any changes to main loop?

## 3. Deployment Steps

### 3.1 Pre-Deploy Checks
```bash
# 1. Check current state of all environments
openclaw cron list  # Verify no cron jobs in ERROR state

# 2. Verify database schema consistency
python3 -c "
import sqlite3
for env in ['dev', 'sbox', 'prod']:
    db = f'data/paper_trading_{env}.db'
    conn = sqlite3.connect(db)
    tables = [r[0] for r in conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()]
    print(f'{env.upper()}: {len(tables)} tables — {tables[:5]}...')
    conn.close()
"

# 3. Check that calibrators are present
ls -la data/calibrated_pipeline*.pkl data/best_calibration_params.json data/bss_cache.json

# 4. Verify signal registry
python3 -c "
from core.signals import create_signal_registry
reg = create_signal_registry('data/metar_backfill.db')
print(f'Active signals: {list(reg.get_all_signals().keys())}')
"
```

### 3.2 Deploy to SBOX (Staging)
```bash
# 1. Merge code to staging branch
git checkout staging
git merge main

# 2. Run staging tests
python3 -m pytest tests/ -v

# 3. Start SBOX instance
PAPER_TRADING_INSTANCE=SBOX python3 core/paper_trading_engine.py

# 4. Verify SBOX health
cat data/sbox_health.json

# 5. Check SBOX database
python3 -c "
import sqlite3
conn = sqlite3.connect('data/paper_trading_sbox.db')
# Run a quick sanity query
trades = conn.execute('SELECT COUNT(*) FROM trades').fetchone()[0]
print(f'SBOX: {trades} trades in database')
conn.close()
"
```

### 3.3 Deploy to PROD
```bash
# 1. Merge to main branch
git checkout main
git merge staging

# 2. Trigger Render deploy
# Render auto-deploys from main branch
# Or manually: https://dashboard.render.com/select-repo

# 3. Verify PROD is running
curl -s https://kalshi-metar-monitor.onrender.com/health | python3 -m json.tool

# 4. Check PROD health
python3 -c "
import json
with open('data/prod_health.json') as f:
    health = json.load(f)
print(json.dumps(health, indent=2))
"
```

## 4. Verification Steps After Deploy

### 4.1 Immediate Checks
- [ ] PROD instance starts without errors
- [ ] All 7 active signals are computed
- [ ] Paper trading loop completes without errors
- [ ] Risk controls are active (no kill switch triggered)
- [ ] Alert state machine is processing transitions

### 4.2 Night Mode Verification
- [ ] `data/night_mode_digest.db` exists
- [ ] During night mode, signals are queued (not sent real-time)
- [ ] At 8am ET, morning digest is delivered
- [ ] Critical override (Sure Thing > 0.80) bypasses night mode

### 4.3 Heartbeat Verification
- [ ] Hourly heartbeat sent to Discord
- [ ] 🟢 OK when system healthy
- [ ] 🟡 WARNING when METAR data stale
- [ ] 🔴 ERROR when errors detected

### 4.4 Cron Job Verification
- [ ] `openclaw cron list` shows no jobs in ERROR state
- [ ] NWP Forecast Collection runs at 06:00 UTC
- [ ] Backup status check runs at 08:00 UTC
- [ ] Weekly DB snapshot runs Sundays at 03:00 UTC

### 4.5 Data Integrity
- [ ] Trade journal entries are being written
- [ ] Alert state machine transitions are recorded
- [ ] Daily reconciliation produces correct P&L
- [ ] Settlement processing works end-to-end

## 5. Rollback Plan

### 5.1 If PROD Deploy Fails
```bash
# 1. Revert to previous commit
git revert HEAD

# 2. Force Render redeploy from main
# (Render auto-deploys on push to main)

# 3. Verify rollback
curl -s https://kalshi-metar-monitor.onrender.com/health
```

### 5.2 If Database Migration Issues
```bash
# 1. Restore from snapshot
python3 scripts/snapshot_db.py --restore --instance prod --snapshot data/snapshots/<latest>

# 2. Verify data integrity
python3 -c "
import sqlite3
conn = sqlite3.connect('data/paper_trading_prod.db')
count = conn.execute('SELECT COUNT(*) FROM trades').fetchone()[0]
print(f'Restored: {count} trades')
conn.close()
"
```

### 5.3 If Cron Jobs Malfunction
```bash
# 1. Disable cron job
openclaw cron disable <job-id>

# 2. Fix the issue

# 3. Re-enable
openclaw cron enable <job-id>

# 4. Test manually
openclaw cron trigger <job-id>
```

## 6. Current State (2026-07-17)

### DEV
- ✅ All Phase 4 code changes applied
- ✅ Night mode module created and tested
- ✅ Heartbeat module created and tested
- ✅ Multi-channel delivery routing added

### SBOX
- Needs sync from DEV (code changes not yet deployed)
- `paper_trading_sbox.db` has 5 trades (as of last check)
- `.sbox.pid` file present — instance was running

### PROD
- Needs sync from DEV (code changes not yet deployed)
- `paper_trading_prod.db` has 0 trades (as of last check)
- `prod_health.json` shows healthy status

## 7. Known Issues
1. **ClawHub update check** — Discord "Invalid Form Body" error; needs Dan to configure Discord channel
2. **Forecast Disagreement cron** — Model `openai/gpt-5-mini` needs to be updated to `openrouter/qwen/qwen3-coder-plus`
3. **NWP + Backup crons** — Model was updated from `openai/gpt-5-mini` to `openrouter/qwen/qwen3-coder-plus`; next run should succeed
4. **Weekly DB snapshot** — Delivery mode set to `none`; next run should succeed