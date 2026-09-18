# WS6 Cluster Diagnostics Artifact Manifest

**Generated:** 2026-08-15
**Purpose:** Immutable record of WS6 experiment artifact. Used for provenance verification.

---

## WS6 Artifact

| Field | Value |
|-------|-------|
| **Path** | `data/ws6_cluster_diagnostics_20260814_211819.json` |
| **Size** | 139,066 bytes (136 KB) |
| **SHA256** | `d91355001e0fc07b8e442100a4f50485a209fbb141cd8433195182ed22a25aaf` |
| **Generated at** | `20260814_211819` |
| **Experiment** | `ws6_cluster_diagnostics` |
| **Status** | **AUTHORITATIVE** |
| **Command** | `python3 scripts/ws6_run.py` |

### Cluster Fusion vs Baseline Note

The cluster fusion runs use **all stations in the cluster** (including sub-TIER-2 that contribute to calibration). The baseline computations use **only eligible TIER-2 stations** (≥20 trades in both eval folds). For clusters where not all stations are TIER-2 eligible, matched-population comparisons are documented separately.

### Tier Labels

Per-station tier labels are annotated in the JSON artifact (`per_station` entries include station name). Tier-1 (≥50 in both eval folds): KDEN, KLAX, KMDW. Tier-2 (≥20 in both eval folds): 11 stations per canonical list.

### Southwest Holdout

- The Fold4 holdout window (2026-05-15 → 2026-08-13) is the **same window** as the outer holdout. They are not independent.
- Exact holdout: 103 trades, ~76 correct (implied by 73.79% accuracy)
- Clopper-Pearson 95% CI: [0.6524, 0.8196] — lower bound exceeds 58% threshold
- CI computed using `scipy.stats.beta.ppf` with k=76, n=103, α=0.05

### Full-20 Diagnostic vs Paper-Trading

The full-20 diagnostic in this artifact (0.5435/966 = 54.35% on 966 trades, outer holdout) is a **backtest accuracy** — directional prediction accuracy on the sealed holdout window. It is NOT a paper-trading simulation.

A separate `stage6_report.md` / `calc_stage6.py` artifact exists in the repository that reports 0.7463/268 (74.63% on 268 trades). That is a **paper-trading simulation** from a different experiment (phase 6 combinatorial search). These are different experiments, different metrics, and different populations. The full-20 backtest is the authoritative directional accuracy metric for the WS6 experiment.

### Per-Station / Per-Region Calibration Matrix

The next experiment matrix should include:
- **Global calibration** (current default): all stations pooled → one calibrator per signal
- **Cluster pooled** (shrunk): per-cluster calibration from pooled cluster data
- **Station-specific** (independent): per-station calibration from station's own data (requires ≥50 calibration samples per station)
- **Matched populations**: accuracy comparisons on matched (both-have-prediction) dates only
- **Holdout**: each calibration variant validated on the same sealed holdout window (once)

## Protected Artifacts (unchanged)

| Artifact | SHA256 | Status |
|----------|--------|--------|
| `data/sweep_directional_full_2026-08-14.json` | `571d21342df70f1710656e0831316f549f8196ce4c465a6661ee76cc5d633811` | ✅ |
| `data/sweep_directional_full_2026-08-14_074943.json` | `1949cb8c7c9c8212d459f6d8faa4d02a5c2fa8c38e18ce08669109f0b98c383c` | ✅ |
| `data/layer2a_signal_isolation_20260814_162320.json` | `5c611b156a2642361c1859fb950f0645797c65fa9af316fc241ba1dd9b1e26e4` | ✅ |
| `data/layer2a_full17_plus_trends_20260814_162614.json` | `cee9477d2cc5e85f2876d325400b9ed306b9dcfb4f383c35317fe4d2148256c6` | ✅ |
| `data/ws9_results_20260814_182841.json` | `b518ad5497fbb4d4407ce71dae83e6b0b065c62bcd6bf2855e2d06c742b1750c` | SUPERSEDED |
| `data/ws9_results_20260814_183147_v2.json` | `47e4f633cc006d630fd3374a1e5f46cc53d322fe1b459b4d2e9713586628023c` | AUTHORITATIVE |

## WS7 Artifacts Reconciliation

The earlier “No WS7 Artifacts” statement is stale and is superseded by the overnight authoritative WS7 artifacts below. The descriptive 21:33 artifacts remain diagnostic; the 22:19+ corrected/inferential artifacts are authoritative for selection. Hashes are preserved; no artifact was overwritten.

| Artifact | SHA256 | Status / role |
|----------|--------|---------------|
| `data/ws7_auth_pairs_20260814_221944.json` | `f60790b997b4dc18424ced98ac4628095f394647f67380f92e9a72232d65a62e` | AUTHORITATIVE inferential pair screen; fixed 136-pair family and 78 non-degenerate family |
| `data/ws7_auth_ablations_v3_20260814_234417.json` | `356a1cc1bc76f4189e7eebe3800eedcd3c3bc2e0e0247640ddbcddd2886db31f` | AUTHORITATIVE corrected ablations; diagnostic/selection support |
| `data/ws7_auth_beam_v3_20260814_234417.json` | `98772f11d763e5b23807578dc79ac993ac5efe2183a3f2a4b55cbe801aaa09c2` | AUTHORITATIVE corrected beam; diagnostic/selection support |

The pair screen’s inferential gate is three-condition (p vs coin flip < Bonferroni alpha, p vs matched baseline < alpha, estimate > baseline). Degenerate/dead signals are excluded before selecting a pair. WS7 results are diagnostic research artifacts, not deployment authorization.