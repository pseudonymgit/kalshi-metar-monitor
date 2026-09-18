# Temperature Prediction Shadow Roadmap
Date: 2026-08-15 UTC
Status: parallel research track; no production/trading enablement

## Objective
Evaluate temperature prediction quality first for ECMWF IFS Direct, GEFS-only, GEFS+ECMWF combined, and HMM diagnostic approaches against the NWS settlement station daily high/low target. Only after forecast accuracy is established may any approach be considered for market research.

## Experiments

### T1 — ECMWF IFS Direct
- Data candidate: `data/tigge_archive.db`.
- Status: **BLOCKED**.
- Reason: retained TIGGE schema has no issue/init timestamp, availability time, revision ID, request/source URI, file hash, valid timestamp, or member IDs; no GRIB files remain. Exact 12Z D-1 vintage reconstruction is impossible. Do not backtest or impute vintage.
- QC receipt: `.meta/continuity/weather-engine/2026-08-15-temperature-vintage-alignment.md`.

### T2 — GEFS + ECMWF combined ensemble
- Data candidates: `data/gefs_archive.db` + `data/tigge_archive.db`.
- Status: **BLOCKED** for the same vintage reason.
- Overlap/member counts are descriptive only; no combined accuracy claim is valid until issue-time provenance is restored.

### T1-G — GEFS-only direct forecast
- Data candidate: `data/gefs_archive.db`.
- Status: **ADVANCE TO FROZEN EVALUATION**.
- GEFS has `init_cycle='12Z'` and a complete step-24 panel, with member counts explicitly reported (6/30/31). Evaluate direct daily high/low forecast accuracy under the common 12Z D-1 contract before any trading use.

### T3 — HMM regime diagnostic
- Data/code candidate: `core/regime_switching_hmm.py` and related feature extraction.
- Question: can regimes explain forecast error or calibrate confidence out of sample?
- Boundary: HMM is ML and remains diagnostic only; it cannot enter the deterministic B-mode signal loop without separate approval.
- Status: design/inventory pending.

## Expert contract incorporated
- Forecast cutoff: proposed 12Z on D-1, with explicit issue vintage and lead-time strata.
- Arms: GEFS-only, ECMWF/TIGGE-only, naive pooled members, and model-balanced pooled members.
- Member availability, blob decoding, inter-model correlation, and effective ensemble size must be reported.
- Recommended accuracy improvement to test in a predeclared later arm: lead/station-specific rolling bias correction that preserves ensemble spread.
- Full expert review: `docs/plans/TEMPERATURE-PREDICTION-SHADOW-EXPERT-REVIEW-2026-08-15.md`.

## Inventory findings incorporated
- TIGGE/ECMWF: 181,400 complete rows; 20 stations; 1,814 dates from 2021-08-19 through 2026-08-06; five 6-hour steps; 50 members/row.
- GEFS: 365,960 rows; 20 stations; 2,050 dates from 2021-01-02 through 2026-08-14; step-24 panel complete but member counts vary 6/30/31.
- GEFS has 12Z issue metadata; TIGGE currently lacks forecast-origin/issue-cycle metadata.
- 28,980 step-24 station/date overlaps exist, but they are not yet issue-time aligned; no combined evaluation may start until this is resolved.
- Inventory receipt: `.meta/continuity/weather-engine/2026-08-15-temperature-shadow-inventory.md`.

## Blocked-item recovery plan
1. Recover exact GEFS/ECMWF issue/init and availability provenance from retained raw artifacts or rerun acquisition with raw object/request/timestamp/member/hash capture.
   - GEFS rebuild QC contract: `docs/plans/GEFS-TEMPERATURE-REBUILD-QC-SPEC-2026-08-15.md`.
   - Required pilot: KAUS, KDEN, KMDW, KNYC, KDFW across fixed standard-time/DST/missing fixtures before full rebuild.
   - Expert rebuild contract: `docs/plans/GEFS-TEMPERATURE-REBUILD-EXPERT-SPEC-2026-08-15.md`.
   - Required evidence: raw object hashes, sidecars, exact cutoff, member identity, local-day/DST trajectory geometry, and deterministic replay.
2. Rebuild forecast geometry as valid-time trajectories covering each station-local calendar day for HIGH/LOW; do not score an arbitrary step-24 scalar as both extrema.
3. Use historical Kalshi/NWS settlements as outcome labels under strict temporal separation; SRRS remains optional audit evidence.
4. Require immutable lineage manifest, member identity/missingness, timezone/DST mapping, no target-day leakage, and deterministic rerun before scoring.

## Common gates
1. Freeze target: exact NWS settlement station daily HIGH/LOW outcome.
2. Freeze forecast issue/cutoff and lead time.
3. Use identical station/date keys across eligible comparisons.
4. No target-day observations or settlements in forecast features.
5. Train/test or calibration separation is temporal and explicit.
6. Report missingness/member availability and do not silently impute.
7. Produce deterministic artifacts, hashes, and per-station/season strata.
8. Accuracy first; no P&L or trading claim from forecast metrics alone.

## Sequence
1. Expert design + data inventory.
2. Independent QC contract.
3. Freeze common evaluator.
4. Run isolated T1/T2/T3 experiments.
5. Review accuracy and robustness.
6. Decide ADVANCE/PARK/KILL for each.
7. Only an ADVANCE result may enter a separate market-impact study.
