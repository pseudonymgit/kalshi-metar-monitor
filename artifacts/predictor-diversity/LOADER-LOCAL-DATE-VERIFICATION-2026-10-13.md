# ASOS loader local-date verification — 2026-10-13

**Task:** fix the reported "pre-dawn cross-day bug" in
`scripts/predictor_diversity_study_2026-10-13.py:306` (`load_asos`), re-run the
diversity study, and re-verify the headline numbers and the om_gfs validation.

## Verdict (short)

- **The reported bug does not exist.** `tot // 1440` (Python floor-division)
  plus `tot % 1440` (Euclidean modulo) is the exact local-minute-of-day split
  and is **identical** to `utc_dt.astimezone(station_tz)` for a negative `tot`.
- **The "fix" that was applied was itself the bug.** The om_gfs loader's
  `ldate = ds` (drop the cross-day carry) is *not* equivalent to the true local
  date and diverges on real data.
- **No headline number changes.** The diversity study is unaffected (its
  consumed signal never sees the divergent rows); the om_gfs verdict is
  unaffected (rate_1h Brier moves by +0.00006).
- **Core conclusion survives: YES.** One forecast sampled five ways; `rate_1h`
  is the best single source; no equal-weight blend beats it.

## Why the original pattern is correct

Python's `//` and `%` are floor / Euclidean, not C truncation:

| tot  | tot//1440 | tot%1440 | reconstruct | astimezone local date |
|-----:|----------:|---------:|------------:|:----------------------|
| -1   | -1        | 1439     | -1          | previous day, 23:59   |
| -300 | -1        | 1140     | -300        | previous day, 19:00   |
| -720 | -1        | 720      | -720        | previous day, 12:00   |
| 0    | 0         | 0        | 0           | same day, 00:00       |

So a pre-dawn UTC observation correctly carries to the **previous** local date
*and* keeps the true local clock minute — exactly what the trading date at the
13:30 station-local pin requires.

### Evidence

- **168,000** (station, offset, minute) combinations, incl. both DST offsets and
  a year rollover: **0 mismatches** vs `astimezone`.
- **2,265** raw pre-dawn ASOS rows (KNYC/KBOS/KLAX/KSEA/KPHX/KATL, 00:00–09:59
  UTC on 2026-08-01): **0 mismatches**.
- `tot == tot//1440*1440 + tot%1440` holds for every negative `tot` tested
  (Euclidean split; C-style truncation would *not*).

## The real defect (the om_gfs `ldate = ds` shortcut)

`scripts/om_gfs_strike_crossing_test_2026-10-13.py::load_asos_fixed` used
`ldate = ds` (UTC date, no carry). Since `utc_date != station_local_date`
whenever local midnight falls inside the UTC day, local-evening rows (the local
daily MAX, ~17:00–24:00 local) are folded into the next UTC date.

Measured divergence (whole archive, vs the correct loader):

| metric | value |
|---|---:|
| ASOS rows with a different (local_date, bucket) | 4,590,478 / 20,346,025 (**22.56%**) |
| shared station-days whose daily max differs | 3,546 / 17,031 |
| station-days under one key only | 164 |
| consumed-range (mod 720–1020) bucket cells differing | 588 (all at mod=1020, the roll term) |
| window station-days where `rate_1h` p differs | 76 / 2,498 |

## Impact on each study

### predictor_diversity_study (the re-run)

`hsf_at_pin` reads only buckets with `mod <= 810` (the 13:30 pin). The loaders
first diverge at `mod = 825` (13:45 local), so the pin input is unaffected:

- **`hsf_at_pin` identical on 3,205 / 3,205 window station-days (0 diffs).**
- Re-run vs the published baseline JSON: **byte-identical** (only
  `generated_at_utc` differs).

Brier table (baseline == re-run):

| source | n | Brier | dir_acc |
|---|---:|---:|---:|
| ecmwf | 2960 | 0.2080 | 0.6916 |
| gefs | 2740 | 0.3290 | 0.6131 |
| gfs | 2960 | 0.1597 | 0.7882 |
| icon | 2960 | 0.1840 | 0.7331 |
| gem | 2520 | 0.1793 | 0.7369 |
| **rate_1h** | 2464 | **0.0921** | 0.8916 |
| market | 391 | 0.6247 | 0.2788 |

Correlation matrix: unchanged (no |r| > 0.85 among the five NWP; correctly
flagged as effectively one family). Blend test: `blend_minus_best_single =
+0.0827` (best single = rate_1h, 0.1241), and every targeted subset is positive
— **no blend beats rate_1h.**

### om_gfs_index validation

The break pins its strike and `rate_1h` baseline to the *same* pin; the
divergence touches only `rate_1h`'s 1h-rate (the roll term), not the strike.

- rate_1h Brier correct = 0.0924 vs om_gfs-loader = 0.0925 (**+0.00006**).
- Verdict holds: rate_1h 0.0922 < om_gfs(bc) 0.1182 (criterion a FAILS);
  fcerr 0.9924 ≥ 0.85 (criterion b FAILS) → **DO_NOT_WIRE** unchanged.
- The om_gfs loader's own numbers should still be re-run on the corrected
  loader before any future publication, since the roll term is genuinely
  mis-dated there.

## Changes made

- `scripts/predictor_diversity_study_2026-10-13.py` — `load_asos` docstring
  documents the correct Euclidean semantics; inline comment added. **Behavior
  unchanged** (no code-path change).
- `scripts/ai_nwp_independence_rate1h_sweep_2026-10-13.py` — sibling comment.
  Behavior unchanged.
- `scripts/om_gfs_strike_crossing_test_2026-10-13.py` — corrected the false
  "pre-dawn bug fixed" docstring/inline comments to state that `ldate = ds` is
  the divergent path and the study's pattern is correct. Behavior unchanged
  (keeps the published numbers).
- `tests/test_asos_local_date_semantics.py` — new hermetic regression test
  (22 cases) locking local-date == `astimezone` and documenting the no-carry
  counterexample.

No live-book writes, no temp DBs. Constraints honored: SBOX bridge / fusion
untouched.
