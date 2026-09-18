# GEFS option / Graphify retention / weather-cron decision

**Date:** 2026-08-16 UTC  
**Scope:** read-only decision artifact; no cleanup, scheduler mutation, scoring, trading, or production integration.

## Decision

**Recommend acquiring the exact additional official native `f057` GRIB2 + `.idx` pairs first, then recapturing the frozen request set with proven serialized timing. Do not change product/lead geometry yet.**

The current geometry recovery bundle has all declared 3-hour TMP point leads through `f054`, but native TMAX/TMIN interval coverage is blocked at the local-day tail. Run3 reports **40 station/member rows: 18 pass, 22 blocked**; every blocked row has `TMAX_INTERVAL_GAP` and `TMIN_INTERVAL_GAP`, with the missing boundary source lead identified as **`f057`**. The official source route and member/date scope are already frozen, so `f057` is the smallest principled repair.

This does **not** make the pilot score-eligible by itself: the earlier provenance receipt also remains `PILOT_BLOCKED` because the captured request manifest contains 2–3 second gaps against the frozen >=10-second request-spacing gate. A clean timing recapture of the frozen request set is still required.

## GEFS alternatives

| Option | Difficulty | Elapsed time (estimate) | Invasiveness | Main risks | Disposition |
|---|---|---:|---|---|---|
| **A. Acquire exact official native `f057`** for the four included D-1 cycles × `{gec00, gep01}`; retain full GRIB2, matching `.idx`, headers, hashes, and timing receipt | Moderate; bounded retrieval + replay/QC | ~3–5 min for 16 objects at >=10s spacing; **~50–60 min** if the full 288-request frozen set must be recaptured to repair timing evidence | Low; new versioned raw/QC artifacts only; no archive/evaluator changes | Official object availability/404, hash or parser failure, timing proof still incomplete; two-member pilot remains non-score-eligible | **Recommended first** |
| **B. Change product/lead geometry** (e.g. drop native interval-coverage requirement and use point-trajectory extrema, or adopt a different native extrema product/lead contract) | High; new contract, source semantics, DST/window QC, and deterministic replay | ~1–3 engineering days plus a fresh bounded QC run | Medium/high; changes the estimand and comparability to settlement HIGH/LOW | Can hide tail coverage error, alter event semantics, introduce product/version mismatch, and make prior evidence non-comparable; does not automatically repair issue/availability timing | Defer unless `f057` route is unavailable or explicitly choosing a new estimand |

**Why A wins:** it addresses the observed blocker directly (the exact missing official lead), preserves the frozen 2m TMP trajectory and native interval semantics, and limits scope. Geometry B is not a harmless implementation tweak; it changes what forecast quantity is being evaluated.

## Accuracy improvement suggestion

After a full-member, timing-qualified geometry PASS, compare trajectory-derived HIGH/LOW against native TMAX/TMIN extrema on a locked holdout stratified by station, event type, DST class, lead/boundary completeness, and member count. Report boundary-complete versus boundary-incomplete error separately. This quantifies representation error and prevents a geometry change from being selected merely because it removes difficult tail cases.

## Graphify retention (retain all; delete nothing)

- **Current useful evidence:** `graphify-out/` (mtime 2026-08-16 00:03Z): `graph.json` has **290 nodes / 767 edges**; `manifest.json` (mtime 12:04Z) is the current source manifest. This is the active code-only AST graph and is the primary current map.
- **Current immutable comparison:** `graphify-out-snapshot-20260816T120200Z/`: same **290 / 767** graph and matching 2026-08-16 manifest. Retain as the latest reproducibility snapshot.
- **Stale but useful historical evidence:** `graphify-out-snapshot-20260815T180200Z/` (**193 / 607**) and older dated snapshots. They preserve prior graph states for change comparison and are regenerable cache, not current truth. Do not delete them in this task.
- **Retention observation:** the Graphify cron is enabled every 6h and says “keep 10 historical,” but the filesystem has more than ten dated/snapshot directories when legacy snapshot trees are counted. This is a retention-policy observation only; no deletion or sweep was run.
- Graphify outputs are structural code evidence, not forecast-value or QC evidence. Forecast conclusions must continue to cite the raw official GEFS/QC artifacts above.

## Weather-engine cron inventory (read-only)

All weather-engine candidates below are **enabled and last-run `ok`** on 2026-08-16. They are classified active/needed or active/supporting. **No unused weather-engine cron is identified, so there is no authorized deletion candidate.** Do not delete similarly named non-weather jobs.

| ID | Name | Schedule | Classification | Evidence / reason |
|---|---|---|---|---|
| `060eb0e3-61e4-45fb-89f1-08a6d383a114` | `graphify-weather-engine-map` | `0 */6 * * *` UTC | **Active / needed** | Last ok 12:02Z; regenerates current 290/767 code map and archives snapshot. |
| `b19907d4-df43-4d16-82dc-667a9b848848` | `NWP Forecast Collection` | `0 6 * * *` UTC | **Active / needed** | Last ok 06:00Z; runs `scripts/nwp_collect.py`, the live NWP ingestion path. |
| `b7046aca-3cb7-4bb2-ab48-7e0fcda7689c` | `ecmwf-tigge-daily-update` | `0 7 * * *` UTC | **Active / needed** | Last ok 07:00Z; keeps TIGGE data current; current forecast-comparison lane depends on it. |
| `488608f8-ba6e-4005-852f-265b1594ae99` | `Forecast Disagreement Daily Collection` | `0 7 * * *` America/New_York | **Active / needed** | Last ok 11:00Z; runs collector and actual backfill for disagreement signal. |
| `0028e6cd-fe3c-4e3d-b391-eb07f82c74ff` | `Weather Engine Status Check` | `0 9 * * *` UTC | **Active / supporting** | Last ok 09:00Z; guarded handoff/P0-P1 status check, lightweight and silent when clear. |
| `f3ec98c3-4dd0-4971-85dc-f6e75fe9d742` | `weather-engine-weekly-db-snapshot` | `0 3 * * 0` UTC | **Active / needed** | Last ok 03:00Z; snapshots DBs and runs freshness gate. |

**Excluded from weather-engine deletion scope:** `f58809f5-2d16-448c-a2c9-8da294609914` is a WhaleWatch collector watchdog even though its command path mentions `weather-engine-source`; it is not a weather-engine cron by name/function. Other non-weather jobs were not considered.

## Evidence paths

- `data/raw/gefs_temperature_pilot/2026-08-15/qc/final_receipt.md`
- `data/raw/gefs_temperature_pilot/2026-08-15-f057-recovery-v1/qc/geometry_fix_run3/summary.md`
- `.meta/continuity/weather-engine/2026-08-15-gefs-temperature-geometry-fix.md`
- `graphify-out/graph.json`, `graphify-out/manifest.json`
- `graphify-out-snapshot-20260816T120200Z/`
- `openclaw cron list --json` read-only inventory at 2026-08-16 UTC
