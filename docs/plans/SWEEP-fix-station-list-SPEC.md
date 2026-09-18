# SWEEP Task 1 — Fix Station List (Sweep Missing KAUS) — SPEC

**Date:** 2026-08-10
**Engineer:** Donna Task 1 execution
**Difficulty:** Low (standalone data/config fix)

## Objective
Restore the per-parameter sweep to the full 20-station universe. The sweep
engine (`scripts/per_parameter_sweep.py`) has a hardcoded `STATIONS` list of 19,
missing KAUS (Austin-Bergstrom). The unified backtest already has all 20.

## First-Principles Analysis
A per-parameter sweep is only valid if it covers the same station universe as the
production backtest. If the sweep omits a station, the returned optimal
parameters are optimized on a strict subset and may not generalize to KAUS.
Root cause is a drift between two hardcoded enumerations (sweep STATIONS vs
backtest stations). The fix is to make the sweep STATIONS match the canonical
20-station universe.

## Data Verification (done)
| Source | Table | KAUS rows |
|---|---|---|
| metar_backfill.db | metar_observations | 49,125 |
| kalshi_settlements.db | kalshi_settlements | 1,068 |
| gefs_archive.db | gefs_archive | 18,298 |
| station_metadata.json | — | present (lat 30.1945, lon -97.6699) |

All sources have KAUS data → KAUS is fully sweep-able.

## Changes
1. In `scripts/per_parameter_sweep.py`, add `"KAUS"` to the `STATIONS` list
   (alphabetical/region position). 19 → 20 stations.
2. `STATION_REGIONS["KAUS"]` already exists (`"subtropical"`) — no change needed.

## QC Criteria (must all pass)
- [ ] `STATIONS` length == 20
- [ ] Fast-mode sweep (`python3 scripts/per_parameter_sweep.py --fast`) completes without errors
- [ ] Fast-mode sweep output reports `n_stations: 20`
- [ ] No other files modified; no git commit; no daemon/dashboard/frontend touched

## Out of Scope
- Signal list changes (Task 2)
- Param consumption (Task 4)