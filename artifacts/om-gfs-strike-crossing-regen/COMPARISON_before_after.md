# A6 — om_gfs strike-crossing re-run on the CORRECT ASOS loader (2026-10-13)

The published study used `load_asos_fixed` (`ldate = ds`, no cross-day carry). The
ASOS-loader review (commits 9212a631f / 6a6832d3a) established that the diversity
study's `PDS.load_asos` is the correct loader and the bespoke `ldate = ds` variant
mis-dates ~22.6% of ASOS rows. This re-run swaps ONLY the loader
(`OM.load_asos_fixed = PDS.load_asos`) — nothing else in the study changes.

Original: `artifacts/om-gfs-strike-crossing/om_gfs_strike_crossing_2026-10-13.json`  
Re-run:   `artifacts/om-gfs-strike-crossing-regen/om_gfs_strike_crossing_regen_correct_loader_2026-10-13.json`

## Before → after

| quantity | original | re-run (correct loader) | Δ |
|---|---:|---:|---:|
| verdict | DO_NOT_WIRE | DO_NOT_WIRE |  |
| criterion_a (Brier<=rate_1h) | False | False | 0 |
| criterion_b (fcerr<0.85) | False | False | 0 |
| grid_rows | 3440 | 3440 | 0 |
| scored_rows_omgfs | 3160 | 3160 | 0 |
| common_rows | 2464 | 2464 | 0 |
| global_bias_F | -0.6506 | -0.6506 | 0.0 |
| global_sigma_F | 1.5964 | 1.5964 | 0.0 |
| Brier rate_1h | 0.0922 | 0.0921 | -0.0001 |
| Brier om_gfs_raw | 0.1392 | 0.1392 | 0.0 |
| Brier om_gfs_bc | 0.1182 | 0.1182 | 0.0 |
| Brier om_gfs_bc_per_station | 0.1128 | 0.1128 | 0.0 |
| Brier gfs_raw_sigma4 | 0.1597 | 0.1597 | 0.0 |
| Brier om_gfs_bc_all_rows | 0.1079 | 0.1079 | 0.0 |
| fcerr level_pooled | 0.9924 | 0.9924 | 0.0 |
| fcerr within_station | 0.988 | 0.9881 | 0.0001 |
| fcerr pcross | 0.6865 | 0.6874 | 0.0009 |
| fcerr max_abs | 0.9924 | 0.9924 | 0.0 |
| level_mae rate_1h_mu | 0.765 | 0.7646 | -0.0004 |
| blend best_blend_brier | 0.0895 | 0.0894 | -0.0001 |
| blend paired_t | -2.773 | -2.761 | 0.012 |
| blend best_weight | 0.25 | 0.25 | 0.0 |

## Result

**No conclusion change.** Verdict stays `DO_NOT_WIRE`; both decision criteria unchanged
(a=False, b=False); every Brier identical to 4 dp except `rate_1h` which drifts
0.0922 → 0.0921 (noise-level, and in the safe direction). The 13:30-pin metrics are
provably identical between loaders (hsf_at_pin uses buckets with mod <= 810, and the
loader difference only re-dates rows with station-local minute-of-day > 810), which is
why om_gfs Brier/fcerr/MAE are byte-identical. Only rate_1h (which scans the intraday
13:00-16:59 window, partially beyond the pin) and the within-station/pcross fcerr
scalars move, at <= 0.0009.

Status: **re-generated on corrected loader — no conclusion change.**


## Independent confirmation that hsf_at_pin is loader-invariant

`hsf_at_pin` reads only buckets with local minute-of-day `mod <= 810` (13:30). The
two loaders differ ONLY in the cross-day carry on the local-date key, which can only
change the mapping of rows whose station-local minute-of-day is beyond 810 (local
evening). Therefore the pin-set of buckets is identical, so the strike `T` is identical.

Empirical cross-check: both runs independently produced the SAME grid (3440 rows),
the SAME scored set (3160) and the SAME common set (2464), and om_gfs Brier/fcerr/MAE
are byte-identical. If `hsf_at_pin` had differed on any scored station-day, the strike
`T` (and hence the `y` label) would change and the om_gfs Brier could not be identical.
Hence 0 hsf_at_pin diffs on all scored station-days — consistent with the loader
review's 168,000-combo verification.

## What moved, and why it is safe

- `rate_1h` Brier 0.0922 → 0.0921 (−0.0001): `rate_1h` scans the 13:00–16:59 local
  window, i.e. buckets up to `mod=1015` — partially BEYOND the pin — so the loader can
  re-date a minority of its buckets. The move is noise-level and in the safe direction
  (makes the om_gfs `≤ rate_1h` bar very slightly harder to clear, not easier).
- `fcerr` within-station 0.9880 → 0.9881, pcross 0.6865 → 0.6874, level_mae
  rate_1h_mu 0.765 → 0.7646: all downstream of rate_1h's intraday mu, all ≤ 0.0009.
- om_gfs columns and all decision inputs: unchanged.

The published DO_NOT_WIRE verdict was safe on the wrong loader; it remains
DO_NOT_WIRE on the correct loader, now cited from a correct-loader artifact.
