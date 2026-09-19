# Weather Engine — Assumption Audit (Systematic)

**Generated:** 2026-09-19 21:xx UTC (temp-sprint-branch)
**Scope:** read-only, deterministic, script-only. No DROP, no backfill, no modification of data.
**Method:** Python/sqlite3 read-only inspection of every `data/*.db`, cross-referenced against `core/signals/*.py` consumers.
**Goal (Dan):** "what else is going to bite us? what else do we think is working but never tested? test it."

---

## Summary — "what will bite us"

| # | Assumption | Test result | Severity |
|---|---|---|---|
| A1 | ERA5 climatology signal (`calendar_climatology`) uses only contemporaneous data | **BROKEN — lookahead leak** | 🔴 P0 |
| A2 | KDAL == KDFW (Dallas is one station) | **BROKEN — duplicate stations, divergent settlements** | 🔴 P0 |
| A3 | NWP forecasts are contemporaneous (no reanalysis leak) | **BROKEN — 33.9% lookahead rows** | 🟠 P1 (gated but pervasive) |
| A4 | All data sources are °F / consumers assume correct unit | **MIXED — 3 sources °C, column-name mismatch** | 🟠 P1 |
| A5 | Every signal's DB table exists & has data | **BROKEN — ai_forecasts, daily_temps, signal_state missing** | 🟡 P2 |
| A6 | All stations are actively updating | **BROKEN — 5× 1-min stations stale since 2024** | 🟡 P2 |
| A7 | Cron jobs that error are non-critical | **MIXED — 6 crons erroring, several weather-critical** | 🟡 P2 |
| A8 | No remaining lookahead beyond NWP/ERA5/GHCN | **BROKEN — calendar_climatology reads same-day reanalysis** | 🔴 P0 (dup of A1) |

---

## 1. UNIT CONSISTENCY (°C vs °F)

Every temperature-bearing data source was sampled and unit-verified. Result matrix:

| Source | Table | Column(s) | Actual unit | Consumers assume | Verdict |
|---|---|---|---|---|---|
| METAR | `metar_observations` | `temp_f`, `temp_c`, `dewpoint_f`, `dewpoint_c` | °F / °C (dual) | °F (`temp_f`) | ✅ OK |
| NWP forecasts | `nwp_forecasts` | `value` (variable `temperature_2m_max/min`) | **°C** | °C (cross_model_divergence converts to °F explicitly) | ✅ OK |
| NWP upper-air | `nwp_forecasts` | `temperature_850hPa_daily_mean` | °C | °C (nwp_analog) | ✅ OK |
| NWP geopot | `nwp_forecasts` | `geopotential_height_500hPa_daily_mean` | meters | meters | ✅ OK |
| ASOS (IEM) | `observations` | `tmpf`, `dwpf` | °F | °F | ✅ OK |
| ASOS (IEM) | `asos_observations` | `tmpf`, `dwpf` | °F | °F | ✅ OK |
| GHCN | `ghcn_settlements` | `high_f`, `low_f` | °F | °F | ✅ OK |
| settlement truth | `settlement_truth` | `settlement_temp_f`, `kalshi_temp_f` | °F | °F | ✅ OK |
| ERA5 reanalysis | `era5_archive` | `daily_max_t2m`, `daily_min_t2m`, `daily_mean_t2m` | **°C** | calendar_climatology converts °C→°F (`*9/5+32`) | ✅ conversion correct, but see A1 |
| GEFS | `gefs_archive` | `ensemble_mean/min/max` | **°C** | (no active signal reads GEFS temps directly) | ⚠️ unverified consumer |
| TIGGE | `tigge_archive` | `ensemble_mean/min/max` | **°C** | (no active signal) | ⚠️ unverified |
| HRRR | `hrrr_forecasts` | `temp_f` | °F | °F | ✅ OK |
| ECMWF | `ecmwf_bias` / `ecmwf_forecasts` | `temp_f`, `ecmwf_temp_f`, `actual_temp_f`, `bias_f` | °F | °F | ✅ OK (but tables EMPTY, see A5) |
| WeatherAPI | `daily`/`hourly` | `max_temp_c/_f`, `min_temp_c/_f`, `temp_c/_f` | dual °C/°F | internal C↔F verified consistent (diff <0.1°) | ✅ OK |
| Kalshi settlements | `kalshi_settlements` | `kalshi_temp` (no unit suffix) | °F (80, 79…) | — | ⚠️ ambiguous column name |

**Column-name mismatch (latent bug):** `core/signals/frontal_passage_nowcast_signal.py` fallback queries `observations` table for `temp_c` and `obs_time`, but the actual `observations` table (`iem_asos_1min.db`, `iem_asos_hourly.db`) has **`tmpf`** (°F) and **`valid`** (not `obs_time`), and **no `temp_c` column**. This fallback always throws (caught by bare `except`) and silently returns `None`. The signal is not in the active registry (active variant is `frontal_passage_intraday`), so severity is latent, but it is a real unit + schema mismatch.

**Ambiguous column name:** `kalshi_settlements.kalshi_temp` carries no unit suffix. Value range (79–80 °F) confirms °F, but a future maintainer could reasonably assume °C. Recommend renaming or documenting.

---

## 2. DATA FRESHNESS (MAX timestamp per DB)

| DB | Table | MAX timestamp | Rows | Status |
|---|---|---|---|---|
| `metar_backfill.db` | `metar_observations` | 2026-09-19 | 12.84M | ✅ fresh |
| `metar_backfill.db` | `settlement_epochs` | 2026-09-19 | 117,597 | ✅ fresh |
| `nwp_forecasts.db` | `nwp_forecasts` | 2026-09-25 (target) | 937,071 | ✅ fresh fetch (09-19) |
| `iem_asos_hourly.db` | `observations` | 2026-09-19 20:30 | 5.64M | ✅ fresh |
| `iem_asos_1min.db` | `observations` | 2026-09-18 12:53 | 8.45M | ⚠️ see per-station below |
| `iem_asos.db` | `asos_observations` | 2026-08-31 10:56 | 5.53M | 🟠 **19 days stale** |
| `gefs_archive.db` | `gefs_archive` | 2026-09-16 | 369,305 | ⚠️ 3 days stale |
| `era5_archive.db` | `era5_archive` | 2026-08-31 | 41,380 | 🟠 **19 days stale** |
| `tigge_archive.db` | `tigge_archive` | 2026-08-13 | 182,100 | 🟠 **5 weeks stale** |
| `weatherapi_archive.db` | `daily`/`hourly` | 2026-09-19 | 121k / 2.4M | ✅ fresh |
| `ghcn_settlements.db` | `ghcn_settlements` | 2026-09-12 | 619,428 | ⚠️ 7 days stale |
| `kalshi_settlements.db` | `kalshi_settlements` | 2026-09-17 | 13,794 | ⚠️ 2 days stale |
| `hrrr_forecasts.db` | `hrrr_forecasts` | 2026-09-10 | 216 | 🟠 **9 days stale** |
| `signal_cache.db` | `signal_cache` | 2026-09-07 | 353,735 | 🟠 **12 days stale** |

**Per-station 1-min ASOS (iem_asos_1min.db) — 5 of 15 stations are dead:**

| Station | last_valid | staleness |
|---|---|---|
| KMSY | 2024-10-25 | 🔴 ~11 months |
| KMSP | 2024-11-13 | 🔴 ~10 months |
| KOKC | 2024-12-12 | 🔴 ~9 months |
| KNYC | 2024-12-25 | 🔴 ~9 months |
| KMIA | 2025-06-23 | 🟠 ~3 months |
| KDEN, KDFW, KDCA, KLAS, KLAX, KBOS, KATL, KMDW, KHOU, KAUS | 2026-09-18 | ✅ current |

> Note: the task brief cited "KDEN 1-min stopped at 2024-10". Actual: **KMSY** stopped 2024-10; **KDEN is current** (2026-09-18). The stale set is KMSY/KMSP/KOKC/KNYC/KMIA.

**Empty (0-byte) DB files — dead placeholders:**
`ecmwf_ensembles.db`, `ghcn_daily.db`, `kalshi.db`, `kalshi_settlement.db`, `settlement_truth.db`, `settlements.db`.
Note `settlement_truth` is a **table** inside `metar_backfill.db` (populated), while `data/settlement_truth.db` (0 bytes) is a separate dead file that could be confused with it.

---

## 3. STATION MAPPING (duplicates / aliases)

**KDAL ↔ KDFW duplicate (confirmed, severity P0):**

- `settlement_truth` contains **both** `KDAL` (63,614 rows, back to 1939) and `KDFW` (53,620 rows, back to 1953) as *separate* stations.
- They are **different physical airports**: KDAL = Dallas Love Field; KDFW = Dallas/Fort Worth International.
- Same-date settlement values **diverge** (recent samples):
  - 2026-09-03: KDAL=91.9°F vs KDFW=93.9°F (Δ 2.0°F)
  - 2026-09-07: KDAL=100.9 vs KDFW=102.0 (Δ 1.1°F)
  - 2026-09-10: KDAL=100.0 vs KDFW=100.9 (Δ 0.9°F)
- The canonical Kalshi "Dallas" series is `KXHIGHTDAL` (per `station_mapping.json`, keyed to **KDFW**, station_name "Dallas/Fort Worth International Airport").
- `core/station_registry.py` correctly marks `KDAL` in `_REMOVED_STATIONS = {"KDAL", "TEST"}`.

**BUT the codebase has *conflicting* Dallas canonicalization in live paths:**

| File | Mapping | Direction |
|---|---|---|
| `core/kalshi_monitor.py:1450-1451` | `"DAL"→"KDAL"`, `"DFW"→"KDAL"` | routes both to **KDAL** (wrong) |
| `core/market_monitor.py:643-644` | `"DAL"→"KDAL"`, `"DFW"→"KDAL"` | routes both to **KDAL** (wrong) |
| `core/kalshi_passive_collector.real-v5.py:81` | `"KDFW"→"TDAL"` | routes KDFW to TDAL ticker (right) |
| `core/kalshi_price_fetcher.py:53` | `"KDFW"→"TDAL"` | routes KDFW to TDAL (right) |

So `kalshi_monitor.py` and `market_monitor.py` canonicalize the Kalshi "DAL"/"DFW" token to `KDAL` (Love Field), while `station_registry.py` says canonical is `KDFW` and KDAL is removed. **This means the live market monitor may be fetching/attributing Dallas settlement temperatures from the wrong station (Love Field) while the backtest/registry uses DFW International.** This is a live-vs-backtest inconsistency that will bite.

**No other duplicate pairs found.** Checked KNYC/KJFK/KLGA (only KNYC present), KORD/KMDW (only KMDW present). The only station-pair duplication is KDAL/KDFW.

**Station count discrepancy:** `station_mapping.json` declares 20 stations; `settlement_truth` has 21 (the extra being KDAL). `station_registry.py` `STATIC_MAPPING` lists 20 (includes KJFK, KORD, KBNA, KPDX, KSLC, PHNL, KTPA, KDTW, KCLT — a *different* 20 than mapping.json).

---

## 4. SIGNAL → TABLE EXISTENCE

Cross-referenced every `FROM`/`INTO`/`UPDATE`/`JOIN` table reference in `core/signals/*.py` against the union of all actual DB tables.

**Missing tables (signal reads a table that does not exist anywhere):**

| Signal | Missing table | Status | Severity |
|---|---|---|---|
| `ai_composite_signal.py` | `ai_forecasts` | DISABLED ("ML pipeline not deployed") | 🟡 P2 (latent) |
| `goldilocks_signal.py` | `signal_state` | DISABLED | 🟡 P2 |
| `spike_reversion_signal.py` | `signal_state` | DISABLED (removed for negative EV) | 🟡 P2 |

**Missing tables in non-signal core modules (still active code paths):**

| Module | Missing table | Context |
|---|---|---|
| `core/round_number_anchoring.py` | `daily_temps` | `SELECT ... FROM daily_temps` — would fail at runtime if invoked |
| `core/enso_regime_bias.py` | `daily_temps` | `load_daily_temps()` reads `daily_temps` |

**Column-schema mismatch (table exists but wrong columns):**

| Signal | Table | Queried column | Actual columns | Result |
|---|---|---|---|---|
| `frontal_passage_nowcast_signal.py` | `observations` (ASOS) | `temp_c`, `obs_time` | `tmpf`, `valid` (no `temp_c`) | fallback silently fails |

**Empty tables (table exists but 0 rows — signal would get no data):**

| DB | Table | Rows |
|---|---|---|
| `hrrr_bias.db` | `hrrr_bias` | 0 |
| `ecmwf_bias.db` | `ecmwf_bias` | 0 |

Signals `hrrr_bias_corrected_signal.py` and `ecmwf_bias_corrected_signal.py` read these — they are DISABLED per registry, but the empty tables confirm the "data infrastructure not deployed" note.

**Verified-present tables (active signals read these correctly):** `metar_observations`, `nwp_forecasts`, `asos_observations`, `era5_archive`, `ghcn_settlements`, `station_climatology`, `kalshi_settlements`, `gefs_archive`, `tigge_archive`, `hrrr_forecasts`, `daily_stats`, `six_hour_aggregates`, `settlement_truth`, `settlement_epochs`.

---

## 5. CRON HEALTH

Cron jobs currently in `error` status (from `openclaw cron list`, 2026-09-19):

| Cron | Schedule | Last run | Failure |
|---|---|---|---|
| `PWW Whale Hit Monitor` | every 5m | 39m ago | error (PWW — out of weather-engine scope but live) |
| `backup-openclaw-checkpoint` | 0 0,12 | 9h ago | error |
| `weather-engine-weekly-db-snapshot` | Sun 3am | 5d ago | error |
| `backup-restore-drill-weekly` | Sun 3:45am | 7d ago | error |
| `WE-intraday-bridge-daily` | 11am ET | 6h ago | error (brief said "now fixed" — **still showing error**) |
| `storage-review-reminder` | Mon 8pm ET | 5d ago | error |

**Note on `WE-intraday-bridge-daily`:** the task brief states it was "now fixed", but it is **still in `error` status** in the current cron list (last run 6h ago, status=error). This contradicts the "fixed" claim — either the fix wasn't applied or a new error occurred. Worth re-verifying.

`weather-engine-weekly-db-snapshot` and `backup-restore-drill-weekly` both erroring means the weekly DB snapshot + restore-drill safety net has been failing for ≥5 days.

---

## 6. LOOKAHEAD (data not available at prediction time)

**A1 (P0): `calendar_climatology_signal.py` — same-day ERA5 reanalysis leak.**

The signal (in the ACTIVE registry, labeled "69.00% (best)") computes a z-score of **today's high** vs climatology:

```python
# today's high from ERA5 reanalysis
SELECT daily_max_t2m FROM era5_archive WHERE station=? AND target_date=?
current_high = current_res[0] * 9.0/5.0 + 32.0   # converts °C→°F
z = (current_high - mean) / std                    # "today's" high vs prior days
```

`era5_archive.daily_max_t2m` is ERA5 **reanalysis** — the daily max for a given date is only knowable *after* that date ends *and* ERA5 publishes (~5-day lag). So when this signal evaluates "for station X on date Y" to predict date Y's settlement, it is reading **date Y's actual outcome** (future data). This is the textbook lookahead leak.

**Consequence:** during backtest (date Y is historical, ERA5 has reanalysis for Y), the signal sees the answer and scores 69%. In live operation (date Y = today, ERA5 only populated through 2026-08-31), `current_res` is `None` and the signal returns `(None, 0.0)` — **silently dead in production**. The "best signal" is a backtest artifact, not a live edge.

This is the single most important finding: **a lookahead leak in a production-registered signal that makes it look good in backtest and disappear live.**

**A3 (P1): NWP reanalysis leak — 33.9% of rows.**

`nwp_forecasts.db` has 317,669 of 937,071 rows (33.9%) with `fetch_date > target_date` — reanalysis/backfill rows that postdate their target. This matches the existing `core/nwp_lookahead_gate.py` documentation (which cites 34.1%, dominated by `gefs_ens` 245,029 members + `ecmwf` upper-air 71,440). The gate `contemporaneous_where()` exists and is applied in `nwp_direct_signal` and `nwp_analog_signal`, but **only those two signals use it** — any other NWP consumer that forgets the gate will leak.

**GHCN climatology** is used only for historical settlement truth (not forward prediction), so no lookahead there — it is the ground truth for settlement, not a feature.

**ERA5/GEFS/TIGGE reanalysis** are all potentially leaky if consumed as "forecast" rather than "climatology." Currently only `calendar_climatology` consumes ERA5 (and leaks, per A1). GEFS/TIGGE have no active signal consumer.

---

## Severity-ranked "will bite us" list

### 🔴 P0 — fix before any further validation/backtest trust

1. **`calendar_climatology_signal.py` lookahead** — reads same-day ERA5 reanalysis as the "current" value. 69% accuracy is an artifact; signal is dead live. Fix: gate on `target_date < prediction_date` (use only strictly-prior days for climatology, never the current day), or switch to a contemporaneous forecast source.
2. **KDAL vs KDFW duplicate** — live `kalshi_monitor.py`/`market_monitor.py` canonicalize "DAL"/"DFW" to `KDAL` (Love Field) while registry/backtest use `KDFW` (DFW International). Divergent settlements (±2°F). Fix: unify to KDFW everywhere; retire KDAL from settlement_truth or explicitly mark it non-canonical and exclude from scoring.

### 🟠 P1 — high risk, active data paths

3. **NWP 33.9% lookahead rows** — gate exists but only in 2 signals; enforce the `contemporaneous_where()` gate on *every* NWP consumer.
4. **Unit/schema mismatch in `frontal_passage_nowcast` fallback** — queries `temp_c`/`obs_time` on a table with `tmpf`/`valid` and no `temp_c`. (Latent — signal disabled, but the pattern of °C/°F column-name drift is the same class as the 3 bugs already found this session.)

### 🟡 P2 — stale / latent, will bite eventually

5. **Stale data feeds:** `iem_asos.db` (19d), `era5_archive` (19d), `tigge_archive` (5wk), `hrrr_forecasts` (9d), `signal_cache` (12d), and 5 dead 1-min ASOS stations (KMSY/KMSP/KOKC/KNYC/KMIA).
6. **Missing tables:** `ai_forecasts` (ai_composite), `daily_temps` (round_number_anchoring, enso_regime_bias), `signal_state` (goldilocks, spike_reversion). Disabled signals today, but `daily_temps` is referenced by non-signal active modules.
7. **Cron failures:** `weather-engine-weekly-db-snapshot`, `backup-restore-drill-weekly`, `WE-intraday-bridge-daily` (brief says "fixed" but still erroring), `PWW Whale Hit Monitor`, `backup-openclaw-checkpoint`, `storage-review-reminder`.
8. **Ambiguous column** `kalshi_settlements.kalshi_temp` (no unit suffix) and 6 dead 0-byte DB files that shadow populated tables (`settlement_truth.db` vs `metar_backfill.db:settlement_truth`).

---

## What was verified OK (no action needed)

- METAR `temp_f`/`temp_c` dual columns — consistent.
- NWP `temperature_2m_max/min` (°C) correctly converted to °F by `cross_model_divergence_signal`.
- ASOS `tmpf` (°F) consistently °F.
- GHCN `high_f`/`low_f` (°F) consistently °F.
- WeatherAPI dual °C/°F columns internally consistent (diff <0.1°).
- `station_climatology` °F, consumers check °F range.
- Upper-air NWP (°C / meters) consistent with upper-air consumers.
- `metar.settlement_epochs` schema-qualified references valid (ATTACH DATABASE ... AS metar).
- Active signals all read existing, populated tables except the noted exceptions.

---

*Audit performed read-only. No data modified, no tables dropped, no backfills run. Temporary scratch scripts (`audit_assumptions_2026_09_19.py`, `audit_report_gen.py`) and `audit_assumptions_report.json` are intermediate artifacts; the authoritative deliverable is this document.*
