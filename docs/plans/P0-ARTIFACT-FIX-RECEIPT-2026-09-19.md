# P0 Data-Integrity Artifact Fix — Receipt (2026-09-19)

Three P0 fixes from the assumption-audit (commit `ad982b01`), committed
individually on `temp-sprint-branch`. No storage changes; no DROP/backfill.

## Fix 1 — calendar_climatology ERA5 lookahead leak

**Commit:** `4ec0ec7d8`

Removed the `evaluate_for_station()` override that queried `era5_archive.db`
for the *current* day's `daily_max_t2m` (ERA5 reanalysis, published ~5 days
late) and z-scored it against the prior 60-day ERA5 window. The signal now
inherits `BaseSignal.evaluate_for_station()`, which loads contemporaneous
METAR daily highs and delegates to `evaluate()` (current value =
`days[idx-1]['high']`, same-day METAR obs available at prediction time).

**Verification:** no `era5`/`era5_archive` reference remains in
`calendar_climatology_signal.py`; `CalendarClimatologySignal.__dict__` has no
`evaluate_for_station` override; module imports clean.

**Re-score (true non-lookahead edge):**

| Axis | fired | accuracy | edge vs 50% |
|---|---|---|---|
| day-over-day (signal's design axis) | 9,265 | **69.09%** | +19.1 pts |
| strike-crossing (correct Kalshi axis) | 828 | 19.4% | −30.6 pts |

The 69% figure was produced by the *in-memory* `evaluate()` path over METAR
(already non-lookahead) on the day-over-day axis — NOT by the ERA5 leak. The
ERA5 leak was confined to the production `evaluate_for_station()` path, which
silently returned `(None, 0.0)` live (ERA5 archive stops 2026-08-31). Fix 1
removes that dead path so production now matches backtest.

On the **correct strike-crossing axis**, `calendar_climatology` has *negative*
edge (−30.6 pts) — the signal's up/down semantics predict warming/cooling, not
Kalshi HIGH/LOW strike-crossing. This is a separate signal-validity issue
(tracked under DIRECTION-AXIS-CORRECTION), not a lookahead bug.

## Fix 2 — KDAL vs KDFW duplicate in live monitoring

**Commit:** `e84f202ab`

Live `kalshi_monitor.py` / `market_monitor.py` `city_to_icao` mapped both
`"DAL"` and `"DFW"` → `KDAL` (Love Field). `settlement_truth` holds both
KDAL (63.6k rows) and KDFW (53.6k rows) with divergent settlements (±2°F);
the station registry canonicalizes to **KDFW** (KDAL is in `_REMOVED_STATIONS`).

Changed both `city_to_icao` maps: `"DAL"`/`"DFW"` → `KDFW`.

**Verification:** no `KDAL` remains as a live city→ICAO target in either file;
both parse. Scoring already canonicalized KDAL→KDFW via
`STATION_ALIASES` (kalshi_settlement_upkeep.py) and `_REMOVED_STATIONS`
(station_registry.py), so this closes the live-monitoring gap — production now
resolves the same station the backtest scores.

## Fix 3 — NWP contemporaneous gate to ALL signals

**Commit:** `3efcde780`

The `fetch_date <= target_date` gate was wired into only `nwp_direct`,
`nwp_analog`, and `ai_composite`. Five more signals read `nwp_forecasts`
without the gate, leaking reanalysis/backfill rows (33.9% of rows have
`fetch_date > target_date`).

Gated:
- `cross_model_divergence_signal._compute_model_bias` (bias query was ungated)
- `esdr_signal` (3 ensemble-spread queries)
- `upper_atmosphere_signal._fetch_series` (850hPa advection + 500hPa height —
  the upper-air fields had 34,460 (station,target_date) keys existing *only*
  via lookahead)
- `temperature_advection_signal` (`load_advection_history` + last-known fetch)
- `frontal_passage_detector` (legacy-schema gradient query; gate added for
  correctness — query still no-ops against the real schema)

**Verification:** all 8 NWP-reading signals now carry
`fetch_date <= target_date` on every per-forecast read query; imports/parse
clean. (`nwp_dtdt_fusion` reads NWP only via the already-gated
`NwpDirectSignal`; `store_advection` is a write, not a read.)

## Scope / constraints honored

- Each fix committed separately with a receipt (this doc).
- Deterministic — no ML/AI in any changed path.
- No storage bloat — no new tables, no DROP/backfill.
- Signals NOT wired live (R/T-5 gate untouched).
- Only the three named artifacts changed; pre-existing working-tree edits
  left untouched (not committed by this task).
