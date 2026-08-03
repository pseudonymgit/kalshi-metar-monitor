# Weather Engine — Comprehensive Master Roadmap

**Updated:** 2026-08-03 01:52 UTC
**Canonical file:** This one. Read this before asking "what's next."
**Reference docs:** `docs/plans/GRAY-ROOM-CONSOLIDATED-R13-R14.md` | `docs/plans/DEAD-CODE-REVIEW-2026-08-03.md` | `docs/ROLLING_TODO.md`

---

## 1. ACTIVE PIPELINE — GEFS Cron (Producing Results)

**Script:** `scripts/gefs_paper_trading_cron.py` (245 lines, standalone)
**Latest run:** 35 trades, 68.57% accuracy, +$970.36 P&L, max_contracts=175
**Schedule:** Every 4 hours (cron `0 */4 * * *`)
**DB:** `data/paper_trading_dev.db` (31 total DBs in the project)
**Accuracy CI:** 68.57% on 35 trades — 95% CI [51.3%, 82.5%]
**Available data:** GEFS archive has 2,036 dates (2021-01-02 to 2026-07-30). Kalshi settlements has 1,750 dates (2021-08-19 to 2026-08-01). **Cron runs on 7 days (0.4% of available data).** Running `--days 365 --start 2025-08-03` would give ~4,745 trades across 365 days — enough for meaningful per-station statistics.

### What's wired:
- ✅ GEFS 31-member ensemble (mean-threshold direction)
- ✅ Ensemble fraction confidence (replaced heuristic 0.5 + temp_diff/20.0)
- ✅ Kalshi published fee formula (per-contract ceil, not total ceil)
- ✅ Risk controls (RiskManager: 5% daily loss, 15% drawdown, 10 consecutive losses)
- ✅ Stop-loss (StopLossMonitor: 20 loss limit, 30-day limit, win-rate stop)
- ✅ Kalshi API price feed (falls back to 0.50 when unavailable)
- ✅ Corrected Kelly formula (edge/(1-c) with market price reference)
- ✅ GLM 5.2 config (max_contracts=175, bankroll=5%, drawdown=15%)

### Fixes applied this session (2026-08-02/03):
| Item | Before | After | 
|------|--------|-------|
| Kelly formula | `(p-c)/(1-c)` | `edge/(1-c)` with market price |
| Market price | `0.5 + 0.4×confidence` (circular) | Kalshi API mid-price |
| Entry price | Hardcoded 0.85 | Kalshi API price |
| 3 dead sizers | Existed in core/ | Deleted |
| Old dashboard.py | Existed in core/ | Deleted |
| 8 hardcoded fees | Spread across files | Centralized ROUND_TRIP_FEE |
| 85 print() calls | In paper_trading_engine.py | Structured logging |
| Circuit breaker | Not wired | Kalshi/NWS/Open-Meteo |
| GRIB cleanup | GRIB files accumulated | Purge on parse |
| Backup script | Left orphaned temp dirs | Trap-clean on exit |
| 126GB disk space | Orphaned backup dirs | Cleaned |
| Fee formula | `ceil(0.07 × C × P × (1-P))` | `ceil(0.07 × P × (1-P) × 100) / 100 × C` |
| Fee path | `/trade-api/v2/markets?` | `/markets?` (Kalshi API compatible) |
| KALSHI_BASE_URL | Not set | `https://api.elections.kalshi.com/trade-api/v2` |
| Phase1 cron entry price | Hardcoded 0.85 | Kalshi API price feed |
| Confidence formula | `0.5 + temp_diff/20.0` heuristic | Ensemble fraction `max(f_up, 1-f_up)` |
| Risk controls | None in pipeline | RiskManager + StopLossMonitor wired |

---

## 2. PHASE B PRODUCTION SYSTEM (ROLLING_TODO.md)

The older alert-path infrastructure. Separate from the GEFS cron. Built around metar_monitor, kalshi_monitor, and the alert pipeline. Many P0 items still outstanding.

### P0 — Production Reliability (10 items, 7 undone)

| # | Item | Status | Notes |
|---|------|--------|-------|
| R1 | Market discovery mismatch investigation | ❌ Not done | Phase B system |
| R2 | Audit missing alert emissions across stations | ❌ Not done | Phase B system |
| R3 | Verify ingestion parity across all active cities | ❌ Not done | Phase B system |
| R4 | Detect stalled station ingestion automatically | ❌ Not done | Phase B system |
| R5 | Confirm integer-cross detection consistency | ❌ Not done | Phase B system |
| R6 | Validate scheduler execution per station | ❌ Not done | Phase B system |
| R7 | Milestone tagging requirement | ❌ Not done | Phase B system |
| R8 | Transition emission verification logging | ⚠️ Partial | Phase B system |
| R9 | Ingestion health visibility endpoint | ✅ Done | `/observability/ingestion-health` |
| R10 | Silent execution-domain guard rejection | ✅ Done | Domain guards present |

### P0 — Regression Alert Governance (6 items, all undone)
| # | Item | Status |
|---|------|--------|
| R11 | Define regression-class taxonomy | ❌ |
| R12 | Define invariant violation triggers | ❌ |
| R13 | Separate regression notifications from trading alerts | ❌ |
| R14 | Add replay-based regression verification hook | ❌ |
| R15 | Define suppression transparency for regression-class events | ❌ |
| R16 | Replace pipeline-truth with runtime-authority-snapshot | ❌ |

### P0 — Alert Governance Hardening (4 items, all undone)
| # | Item | Status |
|---|------|--------|
| R17 | Alert Schema v1.0 design freeze | ❌ |
| R18 | Event log canonicalization format | ❌ |
| R19 | Schema versioning policy | ❌ |
| R20 | Alert distribution topology decision (Discord per-city vs unified) | ❌ |

### P1 — Structural Edge Preservation (6 items, all undone)
Goldilocks alert surfacing, replay verification, detection auditability, station-day visibility, missed-event detection.

### P1 — Epoch Backfill Bootstrap (7 items, all undone)
Historical METAR acquisition, deterministic replay, backfill ≥90 days, persistence, provenance, validation.

### P2 — Signal Engine (Phases 2-8, not implemented)
Proximity regime engine, price suppression logic, time weighting, forced rehydration, signal classification, Goldilocks priority, alert payload upgrade.

---

## 3. ECMWF BACKFILL

| Metric | Value |
|--------|-------|
| Status | Running |
| PID | 958921 |
| Progress | ~85% (589 dates in DB) |
| Remaining | ~1,000 dates, ~6 days |
| DB | `data/nwp_forecasts.db` (146MB) |
| GRIB cleanup | Active (purge on parse) |
| What it enables | ECMWF shadow test, potential +1-3pp accuracy |

**Next action:** Wait for completion. Then run ECMWF shadow test (30 days parallel, wire only if adds >2pp over GEFS-alone).

---

## 4. NWP FORECAST DATA

| Metric | Value |
|--------|-------|
| Status | ✅ **Already backfilled** |
| Dates | 2,045 (2021-01-02 to 2026-08-08) — all available |
| DB | `data/nwp_forecasts.db` (146MB) |
| Models | GFS, ECMWF, ICON, GEM |
| Daily cron | Continues collecting latest forecasts |

**Edge 20 (multi_model_ensemble) can be wired now.** The NWP data is complete. No 30-day wait needed.

---

## 5. GRAY ROOM FINDINGS — ALL UNRESOLVED

### Errors (need fix)

| # | Error | Severity | Fix | Effort | Status |
|---|-------|----------|-----|--------|--------|
| E1 | Circular market price (was 0.5 + 0.4×confidence) | 🔴 HIGH | Kalshi API call | 4h | ✅ DONE |
| E2 | No ensemble fraction (only mean_c used) | 🔴 HIGH | Member-level voting | 4h | ✅ DONE |
| E3 | Entry price hardcoded 0.85 | 🔴 HIGH | Kalshi API price feed | 4h | ✅ DONE |
| E4 | Kelly formula ignored market price | 🔴 HIGH | edge/(1-c) | 2h | ✅ DONE |
| E5 | Confidence formula uncalibrated | 🟡 HIGH | Per-station calibration curve | 4h | 🟡 ADVANCE |
| E6 | Fee docstring/code mismatch | 🟡 MED | Test live Kalshi trade | 0.5h | 🟡 ADVANCE |
| E7 | Fee stair-step discontinuity | 🟢 LOW | Per-contract formula (✅ FIXED) | — | ✅ DONE |
| E8 | Phase1 cron entry price hardcoded 0.85 | 🟡 MED | Kalshi API price feed | 1h | ✅ DONE |
| E9 | No live Kalshi price feed anywhere | 🔴 HIGH | Wire price feed | 4h | ✅ DONE |

### Ideas (unproven, may add value)

| # | Idea | Impact | Effort | Status |
|---|------|--------|--------|--------|
| I1 | Ensemble fraction as actual signal | +2-5pp | 4h | ✅ DONE (confidence from fraction) |
| I2 | Epoch-based Kelly schedule | +5-15% Sharpe | 2h | ADVANCE |
| I3 | Portfolio correlation reduction | Prevents 3-5× over-bet | 12h | ADVANCE |
| I4 | Per-station bias correction | +2-5pp on biased stations | 4h | ADVANCE |
| I5 | Urban heat island correction | +1-3pp on urban stations | 2h | ADVANCE |
| I6 | ECMWF shadow test | +1-3pp | 4h | PARK (waits backfill) |
| I7 | Regime-based confidence modulation | +1-2pp | 4h | PARK |
| I8 | Spatial coherence as confidence modulator | +0.5-1pp | 2h | PARK |
| I9 | Dewpoint ceiling cap | +1-2pp humid summer | 1h | PARK |

### Improvements (ready to build)

| # | Spec | Effort | Status |
|---|------|--------|--------|
| S1 | GLM 5.2 Kelly config (epoch multipliers, portfolio factor) | 2h | ADVANCE |
| S2 | Wire risk controls into GEFS cron | 2h | ✅ DONE |
| S3 | Station tradability audit | 1h | ADVANCE |
| S4 | Fee verification (1 live Kalshi trade) | 0.5h | ADVANCE |
| S5 | Per-station calibration curve | 4h | ADVANCE (post-backfill) |
| S6 | Accuracy CI reporting | 0.5h | ADVANCE |
| S7 | Go/No-Go criteria (5 gates) | — | PARK |
| S8 | Merge sequence (PR1-PR4) | 12h | PR1 DONE, rest pending |

---

## 6. INFRASTRUCTURE STATUS

### Databases (31 total, notable ones)

| DB | Size | Purpose |
|----|------|---------|
| `metar_backfill.db` | 385MB | METAR observations |
| `weatherapi_archive.db` | 315MB | WeatherAPI.com data |
| `nwp_forecasts.db` | 146MB | NWP forecasts + ECMWF backfill |
| `isd_lite_raw.db` | 135MB | ISD lite raw data |
| `gefs_archive.db` | 54MB | GEFS 31-member ensemble |
| `kalshi_settlements.db` | 1.4MB | Kalshi settlement data |
| `paper_trading_dev.db` | 356KB | GEFS cron trades |

### Cron Jobs (13 total)

| Job | Status | Notes |
|-----|--------|-------|
| gefs-paper-trading-cron | ✅ OK | Every 4h — active pipeline |
| NWP Forecast Collection | ✅ OK | Daily 06:00 UTC |
| kalshi-settlement-upkeep | ✅ OK | Daily |
| Backup status check | ✅ OK | Daily |
| Weather Engine Status Check | ✅ OK | Daily |
| System health check | ✅ OK | Hourly |
| Active Work Status Check | ✅ OK | Hourly |
| Memory Dreaming Promotion | ✅ OK | Daily 3am |
| WhaleWatch hourly snapshot | ✅ OK | :15 past hour |
| backup-openclaw-checkpoint-12h | ✅ OK | 12h |
| **luna-price-monitor** | ❌ **ERROR** | Needs investigation |
| **weather-engine-weekly-db-snapshot** | ❌ **ERROR** | Needs investigation |
| **backup-restore-drill-weekly** | ❌ **ERROR** | Needs investigation |

### Configuration

| Parameter | Current | Recommended | Blocked On |
|-----------|---------|-------------|------------|
| Edge threshold | 0.02 | 0.03 (GLM 5.2) | 7 days real edge data |
| Entry price min | 0.15 | 0.25 | Liquidity verification |
| Entry price max | 0.70 | 0.75 | Edge distribution analysis |
| Temp diff min | 0.5°F | 1.0°F | Edge threshold is real gate |
| Kelly fraction | 0.50 | 0.25-0.50 | GLM 5.2 range |
| Epoch multipliers | None | 0.70/0.85/1.00/0.55 | Needs implementation |
| Portfolio factor | None | 0.20× | Needs correlation matrix |

### Other Infrastructure
| Item | Status | Notes |
|------|--------|-------|
| KALSHI_BASE_URL | ✅ Set | `https://api.elections.kalshi.com/trade-api/v2` |
| KALSHI_KEY_ID | ✅ In .env | Configured |
| KALSHI_PRIVATE_KEY_PEM | ✅ In .env | Configured |
| KALSHI_PUBLIC_BASE_URL | ❌ Not set | Used for public endpoints |
| Headroom plugin | ⛔ Disabled | Causes looping — re-enable with isolated testing |
| OpenAI quota | ⛔ Blocked | GPT 5.4/5.5 unavailable |
| Memory index | ✅ Fixed | `nomic-embed-text` on Ollama |
| Disk space | ✅ Clean | 126GB orphaned backup dirs cleaned |

---

## 7. DEAD CODE STATUS

| Category | Count | Lines | Action |
|----------|-------|-------|--------|
| A: Deleted (this session) | 4 | ~1,591 | 3 Kelly sizers + old dashboard.py |
| B: Needs preservation | ~23 | ~15,000 | Risk controls, signals, reference modules |
| C: Currently wired | ~20 | ~13,000 | GEFS/phase1 import chains |
| D: Other modules | ~100+ | ~30,000+ | Preserved — P3, WhaleWatch, alert infra, etc. |

**All non-deleted files preserved.** Full analysis: `docs/plans/DEAD-CODE-REVIEW-2026-08-03.md`

---

## 8. ROADMAP — NEXT ACTIONS (RANKED)

### P1 — Now

| # | What | Effort | Status |
|---|------|--------|--------|
| 1 | **Run full backtest** — increase cron from 7 days to 90-365 days. `--days 365 --start 2025-08-03`. Gives ~4,745 trades across 365 days. | 0.5h | 🟡 ADVANCE |
| 2 | **Station tradability audit** — after full backtest, compute per-station P&L, accuracy, Sharpe. Flag stations under 40% accuracy or negative P&L. | 1h | 🟡 ADVANCE |
| 3 | **Wire Edge 20 (multi_model_ensemble)** — NWP data is already backfilled (2,045 dates). No wait needed. | 4h | 🟡 ADVANCE |
| 4 | **Epoch-based Kelly schedule** — needs cycle tracking added to archive. Re-estimated at 6h. | 6h | 🟡 ADVANCE |
| 5 | **Urban heat island correction** (KNYC, KLAX, KPHX, KDFW) | 2h | 🟡 ADVANCE |
| 6 | **Per-station calibration curves** | 4h | 🟡 ADVANCE |

### P2 — After P1

| # | What | Effort | Gate |
|---|------|--------|------|
| 7 | Portfolio correlation matrix | 12h | After P1.6 calibration |
| 8 | ECMWF shadow test | 4h | ECMWF backfill at 85% |
| 9 | Per-station bias correction (merge with UHI) | 4h | After P1.6 |
| 10 | Spatial coherence wiring | 2h | After P1.6 |
| 11 | Station skill gate wiring | 2h | After P1.6 |
| 12 | Low liquidity traps wiring | 2h | Before real money |

### P3 — Phase B Production Items (Separate System)

| # | What | Status |
|---|------|--------|
| 13 | Market discovery mismatch investigation | ❌ P0 undone |
| 14 | Audit missing alert emissions | ❌ P0 undone |
| 15 | Verify ingestion parity | ❌ P0 undone |
| 16 | Stalled station detection | ❌ P0 undone |
| 17 | Integer-cross detection consistency | ❌ P0 undone |
| 18 | Scheduler execution validation | ❌ P0 undone |
| 19 | Regression alert governance (6 items) | ❌ All undone |
| 20 | Alert governance hardening (4 items) | ❌ All undone |
| 21 | Signal engine phases 2-8 | ❌ Not implemented |

### PARK

| # | What | Gate |
|---|------|------|
| 22 | Fee verification (1 live Kalshi trade) | When actually trading live |
| 23 | Accuracy CI reporting | Formatting change |
| 24 | Fix 3 ERROR'd crons (luna-price, db-snapshot, restore-drill) | Not weather engine items |
| 25 | 30-day paper test | All P1-P2 deployed |
| 26 | Real-money go decision | All 5 gates met |
| 27 | Phase 6.2 search scripts | When relevant |
| 28 | Dead code tagging | When convenient |

---

## 9. GO/NO-GO GATES (before real money)

| Gate | Status | Detail |
|------|--------|--------|
| 30 days paper testing | ❌ Not started | Requires all fixes deployed |
| ≥60% directional accuracy | ❓ 68.57% on 35 trades | Unverified with corrected Kelly |
| ≥0.30 Sharpe | ❌ Unknown | Not computed with corrected sizing |
| No >10% single-day drawdown | ❌ Unknown | Untested at $175 max position |
| Settlement-confirmed accuracy | ❌ 35 trades | 35 < 200 needed for significance |

**Verdict: Not close to real money. All 5 gates fail.**

---

## 10. COMPLETE ITEM INVENTORY

| Category | Total | DONE | ADVANCE | PARK | UNDONE |
|----------|-------|------|---------|------|--------|
| Errors (Gray Room) | 9 | 5 | 2 | 1 | 1 |
| Ideas (Gray Room) | 9 | 1 | 4 | 4 | 0 |
| Improvements (Gray Room) | 8 | 2 | 5 | 1 | 0 |
| Phase B P0 items | 20 | 3 | 0 | 0 | 17 |
| Phase B P1 items | 13 | 0 | 0 | 0 | 13 |
| Phase B P2-P8 items | ~10 | 0 | 0 | 0 | ~10 |
| Category A deletions | 4 | 4 | 0 | 0 | 0 |
| Category B modules | ~23 | 0 | 3 | 20 | 0 |
| Infrastructure items | ~10 | 3 | 3 | 0 | 4 |
| Config pending | 6 | 0 | 6 | 0 | 0 |
| ERROR'd crons | 3 | 0 | 3 | 0 | 0 |
| Go/No-Go gates | 5 | 0 | 0 | 5 | 0 |
| **Grand Total** | **~120+** | **18** | **26** | **30** | **~45+** |

---

*End of Comprehensive Roadmap. Read this file before asking "what's next."*