# B-Mode Loop Log — P1 Execution (2026-08-03)

## Overview
**Operator:** Donna Paulsen → Gilfoyle (subagent)
**Branch:** bmode-p1-execution
**Repo:** `/home/node/.openclaw/workspace/prototypes/weather-engine-source`
**Duration:** 2026-08-03 04:39 UTC → completion

Authority documents read:
- `docs/plans/P1-FIRST-PRINCIPLES-2026-08-03.md` — P1 priority ordering
- `docs/plans/GRAY-ROOM-ROUND14-SYNTHESIS.md` — GLM 5.2 Kelly config
- `docs/plans/GRAY-ROOM-CONSOLIDATED-R13-R14.md` — 26 findings
- `docs/plans/WEATHER-ENGINE-MASTER-ROADMAP.md` — current state

---

## Step 1: Diagnose — Baseline State

**Working tree:** Clean (Gray Room R13-R14 fixes committed at `5034ea5`). Only DB runtime artifacts (wal/shm, log) unstaged.

**GEFS archive:** 363,440 rows, 2,036 dates (2021-01-02 → 2026-07-30), step=24
**Settlements:** 6,151 rows, 1,750 dates (2021-08-19 → 2026-08-01), 21 stations (incl. TEST)
**Station coverage:** Highly uneven — only KMDW (1,708) and KAUS (1,061) have deep history; most stations start 2026.

**Cron:** `scripts/gefs_paper_trading_cron.py` — 7-day live window, produces 67-68% accuracy on 85 trades.

**Bug found in edge computation:** The NO-prediction edge formula was `market_price - confidence` (wrong sign). Should be `confidence - (1.0 - market_price)`. With backtest market_price=0.50, this suppressed ALL down-prediction trades. The live cron avoids this because real Kalshi prices are rarely exactly 0.50, but the edge magnitude was still wrong. Fixed in both the cron and the backtest runner.

**StopLossMonitor bug:** `PRIMARY_DAY_LIMIT = 30` uses `datetime.utcnow()` vs first trade date. For historical backtests, this triggers immediately and halts all trading. The StopLossMonitor is live-forward only; backtest runner should not use it.

---

## Step 2: Build — Full Backtest Runner

**Script:** `scripts/bmode_p1_backtest.py`
- Config-driven (BacktestConfig dataclass with all knobs)
- Walk-forward corrections (UHI bias, per-station calibration) — no lookahead
- Per-station stats output (P&L, accuracy, Sharpe, PF, trade count)
- Separate output DB (`data/bmode_p1_backtest.db`) — does not pollute live `paper_trading_dev.db`
- JSON output to `docs/weather-engine/backtests/`
- CLI: `--days`, `--start`, `--tag`, `--uhi`, `--epoch`, `--calib`, `--risk`, `--edge`, `--max-contracts`, `--kelly`, `--market-price`

**Cycle tracking:** Added `init_cycle` column to `gefs_archive` via `scripts/migrate_gefs_cycle.py`. Default '12Z' for existing rows. Cron updated to read init_cycle and apply epoch multipliers + confidence thresholds + edge tiers + entry bounds.

---

## Step 3: Fix — Edge Bug

**Bug:** `edge = market_price - confidence` for NO predictions. Correct: `edge = confidence - (1.0 - market_price)`.

**Fix applied to:**
- `scripts/gefs_paper_trading_cron.py` (line 350)
- `scripts/bmode_p1_backtest.py` (line 350)

**Impact:** Before fix, the backtest produced 1,072 trades (NO trades suppressed). After fix, 2,820 trades (all 20 stations trade). Accuracy dropped from 69.4% to 60.2% (the up-only bias was inflating accuracy).

---

## Step 4: Run — Baseline (P1.1)

| Config | Trades | Acc% | P&L | Sharpe | PF | Max DD |
|--------|--------|------|-----|--------|----|--------|
| **Baseline** | 2,820 | 60.2% | +$41,220 | 7.42 | 1.41 | 7.51% |
| **UHI** | 2,805 | 60.7% | +$43,788 | 7.79 | 1.45 | 8.10% |
| **Epoch (12Z)** | 2,806 | 60.2% | +$41,226 | 7.41 | 1.41 | 7.48% |
| **Calibration** | 2,666 | 60.5% | +$40,151 | 7.57 | 1.44 | 7.51% |

**Stop condition check:** All runs pass (accuracy >55%, Sharpe >0.15).

---

## Step 5: Analyze — Per-Station Audit (P1.3)

### Losers (flagged for size reduction)
| Station | Trades | Acc% | P&L | Sharpe | Recommendation |
|---------|--------|------|-----|--------|---------------|
| KLAS | 175 | 43.4% | -$2,625 | -2.74 | REDUCE 50% — consistently negative |
| KPHL | 70 | 45.7% | -$749 | -1.99 | REDUCE 50% — negative P&L |
| KMDW | 237 | 50.6% | -$442 | -0.43 | REDUCE 50% — borderline, UHI helps |
| KMIA | 58 | 48.3% | -$378 | -1.17 | REDUCE 50% — small sample but negative |

### Top performers
| Station | Trades | Acc% | P&L | Sharpe |
|---------|--------|------|-----|--------|
| KNYC | 169 | 76.3% | +$7,246 | 9.06 |
| KLAX | 226 | 69.9% | +$7,084 | 6.19 |
| KDFW | 131 | 80.9% | +$6,435 | 11.64 |
| KDCA | 170 | 72.9% | +$6,301 | 7.46 |

---

## Step 6: UHI Correction (P1.2)

**Walk-forward 120-day trailing bias** computed from raw GEFS mean vs settlement actual temp. Applied as correction to GEFS forecast before signal computation.

**UHI factors learned (mean bias in °F, negative = GEFS cooler than station):**
| Station | Bias | Impact |
|---------|------|--------|
| KLAS | -6.70°F | Acc +3.0pp, P&L +$994 |
| KPHX | -5.19°F | Acc +3.1pp, P&L +$733 |
| KSFO | -4.51°F | Acc +3.6pp, P&L +$966 |
| KNYC | -4.39°F | Acc -0.9pp, P&L -$146 (neutral) |
| KATL | -4.29°F | Acc +0.6pp, P&L +$267 |
| KBOS | -4.11°F | Acc -1.8pp, P&L -$393 (overcorrected) |
| KMDW | -2.24°F | Acc +1.9pp, P&L +$445 (back to breakeven) |

**Overall:** UHI adds +0.5pp accuracy, +$2,568 P&L (+6.2%), +0.37 Sharpe. Worth deploying.

**Accuracy improvement suggestion:** Use seasonal (summer-only) bias instead of trailing-120d. The UHI bias is strongest in summer (JJA); a winter bias correction may be counterproductive. Compute per-season bias and apply only when the forecast date falls in the summer season.

---

## Step 7: Epoch Kelly (P1.4)

**GLM 5.2 config wired:**
- `scripts/gefs_paper_trading_cron.py` — reads init_cycle from gefs_archive, applies epoch multipliers, confidence thresholds, entry bounds [0.25, 0.75], edge tiers
- `scripts/migrate_gefs_cycle.py` — adds init_cycle column, defaults to '12Z' for historical rows
- Backtest runner supports `--epoch` flag

**Backtest results (12Z assumption):** Epoch config is essentially identical to baseline in backtest mode because:
- 12Z multiplier = 1.00 (no change to Kelly fraction)
- 12Z confidence threshold = 0.55 (edge threshold 0.02 is the real gate)
- Entry bounds [0.25, 0.75] with market_price=0.50 → no change
- Edge tiers reduce sizing for weak edges (<0.03 → no trade, 0.03-0.06 → 50% size)

**The epoch config will matter when:** (a) init_cycle is populated with actual cycles (00Z/06Z/18Z data), (b) live Kalshi prices fluctuate (so entry bounds and confidence thresholds interact with real market prices).

**Accuracy improvement suggestion:** Deploy cycle tracking in the live NWP collection path. The cron fetches every 4h; stamp the GEFS cycle based on the fetch hour (e.g., fetch at 04:00Z → 00Z cycle, 10:00Z → 06Z, 16:00Z → 12Z, 22:00Z → 18Z). Then the epoch multipliers will modulate position size by cycle quality.

---

## Step 8: Calibration Curves (P1.5)

**Walk-forward 180-day trailing calibration** per station: bin ensemble-fraction confidence by 0.05, compute empirical win rate per bin, interpolate to replace confidence.

**Results:** Calibration shows +0.3pp accuracy, slightly lower P&L (-$1,069, -2.6%) due to fewer trades (2,666 vs 2,820). The pruning effect (rejecting trades where empirical calibration differs from ensemble fraction) is healthy but the sample size (180-day window per station) is small for most stations.

**Accuracy improvement suggestion:** Increase calibration window to 365 days once more data accumulates. The current 180-day trailing window only has enough data for stations with ≥40 samples — many stations lack sufficient history. A pooled calibration (across all stations, with station-specific offsets) would be more robust at current data levels.

---

## Step 9: P1.6 and Phase B Legacy Items

**P1.6 (Wire Edge 20 — multi_model_ensemble):** **PARK'D.** GEFS-only baseline produces 60.2% accuracy at 2,820 trades. NWP fusion is a secondary optimization. Revisit after 30 days of live paper trading.

**Phase B Legacy Items (13 items):** **KILL'D.** All 6 Structural Edge Preservation + 7 Epoch Backfill Bootstrap items belong to the dead alert-path system. The GEFS pipeline replaces it. Effort recovered: ~40h.

---

## Step 10: Commit

All changes committed to `bmode-p1-execution` branch:
- `scripts/bmode_p1_backtest.py` — new: full backtest runner
- `scripts/migrate_gefs_cycle.py` — new: cycle tracking migration
- `scripts/gefs_paper_trading_cron.py` — fix: NO edge formula, wire: epoch config
- `data/bmode_p1_execution_log.md` — this log
- `docs/weather-engine/backtests/bmode_p1_*.json` — 4 backtest results
- `.meta/continuity/weather-engine/2026-08-03-bmode-p1-execution.md` — handoff

Pushed to branch; NOT merged to main (all merges through Dan).

---

## Files Changed

| File | Change | Type |
|------|--------|------|
| `scripts/bmode_p1_backtest.py` | New: config-driven full backtest runner | Add |
| `scripts/migrate_gefs_cycle.py` | New: init_cycle migration | Add |
| `scripts/gefs_paper_trading_cron.py` | Fix: NO edge formula; Add: epoch config, cycle tracking | Modify |
| `data/bmode_p1_execution_log.md` | This log | Add |
| `docs/weather-engine/backtests/` | 4 JSON result files | Add |
| `.meta/continuity/weather-engine/2026-08-03-bmode-p1-execution.md` | Continuity handoff | Add |

## Bugs Found and Fixed
1. **NO edge formula sign error** (HIGH severity) — both cron and backtest runner. Fixed.
2. **StopLossMonitor PRIMARY_DAY_LIMIT** (MED severity) — uses real utcnow(), breaks historical backtests. Not fixed (live-forward design; documented as known limitation).
3. **UHI bias/calibration feedback loop** — walk-forward was recording corrected values, creating self-referential bias. Fixed by storing raw GEFS temp and raw confidence.

## Blocked on Dan
1. **Merge to main** — review and merge `bmode-p1-execution` branch
2. **Deploy cycle tracking in NWP collection path** — the cron is wired but the data collection path needs to stamp init_cycle. The gefs_archive writer is external to this repo (not found in scripts/). May need a separate cron job or wrapper.
3. **Station sizing for losers** — KLAS, KPHL, KMDW, KMIA need 50% size reduction in the production config. Requires Dan's approval before changing.
4. **ECMWF backfill status** — PID 958921 at ~85%. Not stalled but not actioned here (P1.6 PARK'd).