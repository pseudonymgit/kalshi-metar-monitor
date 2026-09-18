# WS9 Artifact Hash Manifest

**Generated:** 2026-08-15
**Purpose:** Immutable record of WS9 experiment artifacts. Used for provenance verification.

---

## v1 — SUPERSEDED

| Field | Value |
|-------|-------|
| **Path** | `data/ws9_results_20260814_182841.json` |
| **Size** | 332,431 bytes (328 KB) |
| **SHA256** | `b518ad5497fbb4d4407ce71dae83e6b0b065c62bcd6bf2855e2d06c742b1750c` |
| **Generated at** | `20260814_182841` |
| **Experiment** | `ws9_trim_trend` |
| **Status** | **SUPERSEDED** — T2/T3 used `metar_trend` registered signal (yesterday vs day-before lookback) instead of `honest_trend_B` (3-day trailing trend per spec B.2). Arms T2/T3 are NOT spec-compliant in this version. Superseded by v2. |
| **Command** | `python3 scripts/ws9_run.py` (v1 script) |

## v2 — AUTHORITATIVE

| Field | Value |
|-------|-------|
| **Path** | `data/ws9_results_20260814_183147_v2.json` |
| **Size** | 356,348 bytes (348 KB) |
| **SHA256** | `47e4f633cc006d630fd3374a1e5f46cc53d322fe1b459b4d2e9713586628023c` |
| **Generated at** | `20260814_183147` |
| **Experiment** | `ws9_trim_trend_v2` |
| **Status** | **AUTHORITATIVE** — All arms spec-compliant. T2/T3 use `honest_trend_B` (sign(yesterday_max − 3ago_max), METAR-only, label-safe, fixed conf 0.55). |
| **Command** | `python3 scripts/ws9_run.py` (v2 corrected script) |

---

## Arm Name Reconciliation

| Spec Arm | v1 Name | v2 Name | Correct Signal | Notes |
|----------|---------|---------|----------------|-------|
| T1 | `T1_pressure_delta_alone` | `T1` | pressure_delta | ✅ Correct in both |
| T2 | `T2_pd_plus_trend` | `T2` | **pd + honest_trend_B** | v1 WRONG: used metar_trend; v2 CORRECT |
| T3 | `T3_trend_B_alone` | `T3` | **honest_trend_B alone** | v1 WRONG: used metar_trend; v2 CORRECT |
| T4 | `T4_full17_fixed` | `T4` | Full-17, cf=0.6, kf=1.0 | ✅ Correct in both |
| T5 | `T5_trimmed_top3`, `T5_trimmed_top2`, `T5_pd_trend` | `T5_top3`, `T5_top2`, `T5_pd_trend` | See T5 table below | See T5 table |
| N1 | `N1_full17_reselected` | `N1` | Full-17, reselected | ✅ Correct in both |
| T6 | Cluster variants | `T6` | pd + honest_trend_B per cluster | ✅ Correct in both, but see caveats |

### T5 Arm Detail

| v2 Key | Renamed To | Signals | Note |
|--------|-----------|---------|------|
| `T5_top3` | **T5_registered_top3** | pressure_delta, metar_trend, corrected_pressure_delta | Registered top-3 from WS3/5 |
| `T5_top2` | **T5_registered_top2** | pressure_delta, metar_trend | Registered top-2 (pd + metar_trend) — **best arm 63.95%** |
| `T5_pd_trend` | **T5_honest_trend** | pressure_delta, honest_trend_B | Honest-trend equivalent of T2 — 49.36% |

### T6 Cluster Caveats

| Cluster | Stations | Eligible | Trades | Note |
|---------|----------|----------|--------|------|
| southwest | KPHX, KLAS | 2 | Small | Highest accuracy (66.72%) but n=2 stations |
| northeast | KNYC, KBOS, KDCA | 3 | Moderate | No KPHL (startup station, 0/4) |
| plains_midwest | KDEN, KMSP, KMDW, KOKC | 2 | Small | No KMSP/KOKC (startup/borderline) |
| gulf_south | KHOU, KMIA, KATL, KDFW, KAUS, KMSY | 2 | Small | Only KAUS (1074 records) + KATL (190) |
| west_coast | KLAX, KSEA, KSFO | 2 | Small | No KSEA (startup) |
| texas | KSAT | 0 | 0 | Single-station cluster, startup — no eligible |

**Caveat:** T6 cluster results have limited sample sizes (2-3 eligible stations per cluster). Per-station accuracy claims are not supported for clusters with <5 eligible stations. Cluster-level accuracy is pooled and diagnostic only. Outer-holdout failure counters are not available per cluster in the current evaluator output (the per-fold result does not include holdout failure counters), which limits decomposition of abstention reasons in the holdout window. This is a diagnostic limitation, not an inferred negative result.

## Protected Artifacts (L1/L2A, unchanged)

| Artifact | SHA256 | Status |
|----------|--------|--------|
| `data/sweep_directional_full_2026-08-14.json` | `571d21342df70f1710656e0831316f549f8196ce4c465a6661ee76cc5d633811` | ✅ Unchanged |
| `data/sweep_directional_full_2026-08-14_074943.json` | `1949cb8c7c9c8212d459f6d8faa4d02a5c2fa8c38e18ce08669109f0b98c383c` | ✅ Unchanged |
| `data/layer2a_signal_isolation_20260814_162320.json` | `5c611b156a2642361c1859fb950f0645797c65fa9af316fc241ba1dd9b1e26e4` | ✅ Unchanged |
| `data/layer2a_full17_plus_trends_20260814_162614.json` | `cee9477d2cc5e85f2876d325400b9ed306b9dcfb4f383c35317fe4d2148256c6` | ✅ Unchanged |

## WS6/WS7 Reconciliation

The earlier “No WS6/WS7 Artifacts” statement is stale and is superseded by the overnight manifests and authoritative artifacts. WS6 is authoritative at `data/ws6_cluster_diagnostics_20260814_211819.json` (SHA256 `d91355001e0fc07b8e442100a4f50485a209fbb141cd8433195182ed22a25aaf`). WS7 inferential/ablation/beam artifacts exist and are authoritative for research selection; their results remain diagnostic and do not authorize deployment. Earlier descriptive WS7 artifacts are preserved as diagnostic, not deleted.

Contents of `data/` filtered to WS9-only:

```
data/ws9_results_20260814_182841.json      (v1 — SUPERSEDED)
data/ws9_results_20260814_183147_v2.json   (v2 — AUTHORITATIVE)
```