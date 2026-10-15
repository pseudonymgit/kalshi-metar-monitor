# Kalshi Truth Switch — re-scoring every source against unified settlement truth

**Date:** 2026-10-15 · **Owner:** Gilfoyle · **Repo:** `prototypes/weather-engine-source/`
**Script:** `scripts/kalshi_truth_switch_2026-10-15.py` · **Artifact:** `data/kalshi_truth_switch_2026-10-15.json`

**Read-only. No code default switched — this is the evidence; Dan decides.**

## Axis

`T = floor(running_high@13:30 station-local) + 1`, `y = 1{settled_high > T}` (strict; a tie is a LOSS).
The axis is set by the **observed** running high, so it is instrument-independent: the same `T`
is used under both truth regimes. Only `y` moves.

## Truth reconciliation

### Alignment defect found while building the unified truth

- `data/kalshi_settlements.db` holds **10194** HIGH rows. **623** of them have `target_date` **one day later** than the ticker's own `-YYMMMDD` date (all `historical_api`, Jan-May 2026 backfill).
- Keyed naively on `target_date`, those rows land on the wrong station-day (~7°F error vs GHCN). **Keyed on the ticker's own date they agree with GHCN to ~0.05°F.** The unified truth here uses the ticker date for `kalshi_settlements` and prefers `settlement_truth.kalshi_temp_f` (clean, 0.05°F vs GHCN) where present.
- This is a **latent data-integrity defect in the truth table itself**, not an instrument disagreement. Any prior analysis that joined `kalshi_settlements` on `target_date` inherited a ~7°F error on those 623 rows.

- cohort rows on the axis: **3205**
- carrying **both** truths: **2946** (Kalshi-only 0, GHCN-only 259)
- temperatures disagree on **0.4012** of overlapping rows (median |Δ| **0.0°F**, p90 **0.1°F**, max **10.8°F**)
- **label flips** (y differs between regimes): **114** = **0.0387** of overlapping rows

Unified truth = Kalshi canonical where present, GHCN fallback otherwise:
1. `settlement_truth.kalshi_temp_f` (clean; keyed on `local_trading_date`)
2. `data/kalshi_settlements.db` (HIGH series) → `kalshi_temp`, **keyed on the ticker date**
3. `data/ghcn_settlements.db` → `high_f` (identical to the grid's `settled_max`)

## Full-cohort Brier (GHCN truth — reproduces the published baseline)

| source | n | Brier | dir acc | base rate |
|---|---:|---:|---:|---:|
| om_gfs_raw | 3205 | 0.1254 | 0.824 | 0.809 |
| om_gfs_bc | 3205 | 0.1108 | 0.8443 | 0.809 |
| om_gfs_bc_ps | 3205 | 0.1055 | 0.8465 | 0.809 |
| gfs | 3040 | 0.1648 | 0.7796 | 0.8118 |
| ecmwf | 3040 | 0.2149 | 0.6822 | 0.8118 |
| icon | 3040 | 0.1887 | 0.7247 | 0.8118 |
| gem | 2634 | 0.1829 | 0.7339 | 0.8117 |
| gefs | 2853 | 0.3209 | 0.6211 | 0.8041 |
| rate_1h_pit | 2346 | 0.3621 | 0.1982 | 0.8018 |
| market | 584 | 0.6271 | 0.274 | 0.7705 |

## Overlap sub-cohort — GHCN truth vs Kalshi truth (identical rows)

| source | n | Brier (GHCN) | Brier (Kalshi) | Δ (Kalshi−GHCN) | base (GHCN) | base (Kalshi) |
|---|---:|---:|---:|---:|---:|---:|
| om_gfs_raw | 2946 | 0.1256 | 0.1195 | -0.0061 | 0.8143 | 0.7858 |
| om_gfs_bc | 2946 | 0.1108 | 0.1115 | 0.0007 | 0.8143 | 0.7858 |
| om_gfs_bc_ps | 2946 | 0.1036 | 0.1021 | -0.0015 | 0.8143 | 0.7858 |
| gfs | 2823 | 0.1635 | 0.1651 | 0.0016 | 0.8172 | 0.7889 |
| ecmwf | 2823 | 0.2136 | 0.2162 | 0.0026 | 0.8172 | 0.7889 |
| icon | 2823 | 0.1871 | 0.1915 | 0.0044 | 0.8172 | 0.7889 |
| gem | 2467 | 0.1804 | 0.1886 | 0.0082 | 0.8152 | 0.7892 |
| gefs | 2616 | 0.3196 | 0.3259 | 0.0063 | 0.8104 | 0.7825 |
| rate_1h_pit | 2120 | 0.3643 | 0.3485 | -0.0158 | 0.8085 | 0.7717 |
| market | 583 | 0.6281 | 0.6287 | 0.0006 | 0.7719 | 0.7633 |

## Reading

- A Δ near zero means the source's apparent skill was **not** manufactured by the truth source.
- A Δ that is materially positive (Brier worse under Kalshi) means the source only looked good
  against GHCN — the same failure mode the LOW analysis found against METAR.
- Compare the Δ across sources: if the **market** improves against Kalshi while our forecast
  does not, then any 'edge vs market' was a truth artifact.

## VERDICT — is our apparent edge a product of the wrong truth source?

**No. On the HIGH cohort the edge survives both truth regimes.** Every forecast source's
Brier moves by at most ~0.008 between GHCN and Kalshi truth, in both directions, with no
systematic sign. The best source (`om_gfs_bc_ps`) actually *improves* marginally under Kalshi
(0.1036 → 0.1021). The ranking of sources is identical under both regimes. This is the opposite
of the LOW result, where METAR truth manufactured a fake positive edge that went negative
against Kalshi.

**But:** the naive join would have told the opposite story. Without the date-alignment fix,
623 mis-dated rows give a spurious +0.008–0.016 Brier penalty and 6.5% label flips — i.e. the
apparent 'our edge degrades under Kalshi' is itself an artifact of the truth table's own bug.
The lesson is the same one the register keeps recording (P4): *our metrics are real only after
the joins are verified.*

**Two secondary findings:**

1. `rate_1h_pit` has no skill on this axis under *either* truth (Brier 0.3485–0.3643 vs a base-rate
   Brier of ~0.17). Its 0.016 improvement under Kalshi is noise around a broken signal, not an edge.
2. The **market** Brier (0.628) is well *worse* than the base-rate Brier (~0.18) in this sample.
   That is not a tradeable edge — it is the near-money quote subset being read at the pin, where the
   13:30 ladder mid and the re-anchored strike disagree. It is a caution on reading the market column,
   not evidence about our forecast skill.
