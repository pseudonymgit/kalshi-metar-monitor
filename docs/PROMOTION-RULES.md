# Promotion Rules — Weather Engine

**Status:** Active  
**Created:** 2026-07-05  
**Owner:** Gilfoyle  

---

## Overview

This document defines the rules for promoting code, config, and data from DEV → PROD in the weather engine. No code, signal, or configuration is promoted to PROD without satisfying these rules.

---

## 1. Data Reconciliation Rules

### 1.1 DEV → PROD Promotion Checklist

Before promoting any change from DEV to PROD:

| Check | Requirement | Verification |
|-------|-------------|--------------|
| DB Schema | No destructive schema changes | `sqlite3 .schema` diff between DEV and PROD DBs |
| Signal Accuracy | DEV signal accuracy ≥ 65% over 7-day window | Run `scripts/split_backtest.py --instance DEV --days 7` |
| Sharpe Ratio | DEV Sharpe ≥ 1.0 over 7-day window | Extract from `daily_balances` table in DEV DB |
| No Alert Spam | Alert rate < 50/day in DEV | Count alerts in `transition_events` table |
| Position Sizing | No trade exceeds `max_size_usd` for instance | Query `trades` table for max `position_size_usd` |
| Settlement Integrity | All settled trades have `realized_pnl` != NULL | Query `trades WHERE status='closed' AND realized_pnl IS NULL` returns 0 rows |
| Calibration | Brier score < 0.25 (if ≥ 20 resolved trades) | Check `calibration_metrics` table |

### 1.2 Code Promotion Checklist

| Check | Requirement |
|-------|-------------|
| Syntax | `py_compile` passes on all modified files |
| Tests | All existing tests pass (`pytest tests/ -q`) |
| No AI in Loop | No LLM/AI calls in execution path |
| Version Tag | `trade_version` field updated on any signal logic change |
| Backward Compat | Alert schema v1.0 compatibility maintained |

### 1.3 Config Promotion

| Config | DEV | PROD | SBOX |
|--------|-----|------|------|
| base_size_usd | $50 | $100 | $10 |
| max_size_usd | $200 | $500 | $50 |
| min_size_usd | $10 | $25 | $5 |
| initial_balance | $5,000 | $10,000 | $1,000 |
| discord_enabled | false (default) | true (when promoted) | false |
| fee_rate | 0.001 | 0.001 | 0.002 |

---

## 2. Promotion Process

### Step 1: DEV Validation
- Run DEV instance for ≥ 7 days
- Verify all checklist items pass
- Generate promotion report: `scripts/generate_promotion_report.py --instance DEV`

### Step 2: Code Review
- Review all file changes (diff against last promoted tag)
- Verify no unreviewed AI/ML code added
- Confirm signal version bumped if logic changed

### Step 3: SBOX Smoke Test
- Run SBOX instance for 1 day with promoted config
- Verify no crashes, no alert spam, settlements process correctly

### Step 4: PROD Deployment
- Tag release: `git tag deploy-YYYYMMDD-HHMM`
- Deploy to Render (auto-deploy from `main` branch)
- Monitor first 24 hours of PROD alerts
- Rollback if any critical failure

### Step 5: Post-Promotion Audit
- After 7 days in PROD, audit:
  - Alert accuracy vs DEV
  - P&L vs expectations
  - Any new failure modes

---

## 3. Rollback Rules

- If PROD alert accuracy < 55% over 3-day window → rollback
- If PROD produces > 100 alerts/day → rollback
- If any trade with `position_size_usd` > `max_size_usd` → rollback immediately
- If settlement processing fails for > 10% of trades → rollback
- Rollback = revert to previous deploy tag + redeploy

---

## 4. DB Snapshot Policy

### Weekly Local DB Snapshot
- **Schedule:** Every Sunday at 03:00 UTC
- **Retention:** 3 rotating copies (snapshots rotate)
- **Storage:** Minimal — compressed SQLite dumps
- **Location:** `data/snapshots/`
- **Script:** `scripts/snapshot_db.py`

### Snapshot Rotation
```
data/snapshots/
  metar_backfill_001.db.gz  (newest)
  metar_backfill_002.db.gz  (middle)
  metar_backfill_003.db.gz  (oldest)
```

When a new snapshot is taken:
1. Delete `_003.db.gz` (oldest)
2. Rename `_002.db.gz` → `_003.db.gz`
3. Rename `_001.db.gz` → `_002.db.gz`
4. Write new `_001.db.gz`

### Snapshot Contents
- `metar_backfill.db` — full dump (compressed)
- `paper_trading_dev.db` — full dump (compressed)
- `paper_trading_prod.db` — full dump (compressed, if exists)

### Restoration
```bash
# Restore metar DB from snapshot
gunzip -c data/snapshots/metar_backfill_001.db.gz > data/metar_backfill.db

# Restore paper trading DEV DB
gunzip -c data/snapshots/paper_trading_dev_001.db.gz > data/paper_trading_dev.db
```

---

## 5. Cron Integration

The weekly DB snapshot is managed via OpenClaw cron:

```json
{
  "name": "weather-engine-db-snapshot",
  "schedule": "0 3 * * 0",
  "sessionTarget": "isolated",
  "payload": {
    "kind": "agentTurn",
    "prompt": "Run the weather engine DB snapshot script: cd /home/node/.openclaw/workspace/prototypes/weather-engine-source && python3 scripts/snapshot_db.py"
  },
  "model": "openai/gpt-5-mini"
}
```

---

**Last Updated:** 2026-07-05  
**Approved by:** Dan (via Donna dispatch)
