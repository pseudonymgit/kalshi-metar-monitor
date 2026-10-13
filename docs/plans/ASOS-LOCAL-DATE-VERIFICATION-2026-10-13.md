# ASOS local-date attribution — verification of the alleged `load_asos` cross-day bug (2026-10-13)

**Task:** "Fix the load_asos cross-day bug + re-verify the diversity study."
**Disposition: PREMISE REFUTED — no code fix is warranted.**
`predictor_diversity_study_2026-10-13.py::load_asos` is *already correct*. The
`om_gfs` validation script's `load_asos_fixed` (which "drops the cross-day
carry") is the divergent, incorrect loader. The diversity study's headline is
reproduced byte-for-byte and therefore does not change.

Read-only on the live book. No writes to any live DB or committed artifact.

## 1. The claim under test

- Alleged bug (`scripts/predictor_diversity_study_2026-10-13.py` ~L306):
  ```python
  tot  = hh*60 + mm + off//60            # station-local minute-of-day
  ldate = (utc_date + timedelta(days=tot // 1440)).isoformat()
  ```
  Claim: for US stations before ~08:00 UTC, `tot` is negative, Python floor-divides
  negatives toward −inf, so `tot//1440 == -1` and pre-dawn rows are attributed to
  the **previous** local date, shifting the candidate strike set.

- The `om_gfs` "fix" (`scripts/om_gfs_strike_crossing_test_2026-10-13.py::load_asos_fixed`):
  `ldate = ds` (UTC date, **no cross-day carry**).

## 2. Why the claim is wrong

`tot = hh*60 + mm + off//60` is the true station-local minute-of-day **relative to
00:00 of the UTC calendar day**. When the local calendar day is *earlier* than the
UTC day (US stations, evening local), `tot` is negative — e.g. KNYC
`2026-08-01 00:00 UTC = 2026-07-31 20:00 local` → `tot = -240`.

Python `//` and `%` are **floor/Euclidean**, so `(tot // 1440, tot % 1440)` is the
*exact* Euclidean split of `tot`:

| tot  | tot//1440 | tot%1440 | local | true local (astimezone) |
|------|-----------|----------|-------|--------------------------|
| -240 |    -1     |   1200   | 20:00 prev day | 20:00 prev day ✔ |
|    0 |     0     |      0   | 00:00      | 00:00 ✔ |
|  840 |     0     |    840   | 14:00      | 14:00 ✔ |

The formula *is* ``utc_dt.astimezone(station_tz)`` computed arithmetically — the
exact opposite of the described failure. The negative `tot` is the *normal* case
for the local evening, and floor-division carries it to the correct previous local
day.

## 3. Evidence (all read-only)

### 3.1 Full-window structural diff (2026-04-21..2026-10-09, 20 Kalshi stations)
One scan, 3,346,030 rows; pre-dawn rows (`tot < 0`): **687,942**.

| comparison | bucket keys | key-set symdiff | bucket value diffs | dmax keys | dmax value diffs |
|---|---|---|---|---|---|
| **A (study, floor-carry) vs C (true astimezone)** | 236,417 vs 236,417 | **0** | **0** | 3,278 vs 3,278 | **0** |
| A (study) vs B (om_gfs `ldate=ds`) | 236,417 vs 236,417 | **17,974** | — | — | **547** |

**A is byte-identical to the true local-time attribution.** B (om_gfs) diverges
on 17,974 buckets / 547 daily maxima. The divergence is *entirely* local-evening
rows (local ≥ ~17:00 = the local daily MAX), which `ldate=ds` folds into the next
UTC day's bucket; e.g. KLAS `2026-05-05` mod 1020/1035 exist under A but are `nan`
under B.

### 3.2 Scored-axis diff (the only axis that can move a headline)
`hsf_at_pin` (buckets with local mod ≤ 810 = 13:30 equation pin) across 3,290 union
station-days:

| quantity | A-vs-B differences |
|---|---|
| **`hsf_at_pin` (drives strike T)** | **0** |
| rate_1h μ | 87 |
| rate_1h p | 76 |
| raw daily max (`dmax`) | 547 |

**The strike set is provably unchanged.** The rate_1h μ/p differences arise only
because A and B bucket *different wall-clock rows into the same key*: A's
(Euclidean) buckets align 1:1 with station-local wall-clock time; B's carry-free
buckets mix rows offset by the UTC offset (so e.g. the `mod=1005` bucket is 13:45
local for one convention and a different wall-clock instant for the other). A
matches the signal's own local-time convention (`sweep_subhourly_strike_crossing.py`
uses `astimezone(...).date()` + local minute-of-day); B does not.

### 3.3 Independent truth check (2026-07-25..2026-08-10, KNYC-class window)
Per-(station, local_date) ASOS max vs canonical `settlement_truth` HIGH:

| loader | MAE vs truth | bias |
|---|---|---|
| **A (study, floor-carry)** | **1.097 °F** | **−0.050 °F** |
| C (true astimezone) | 1.097 °F | −0.050 °F |
| B (om_gfs `ldate=ds`) | 1.105 °F | **+0.521 °F** |

A ≡ C; B is biased +0.52 °F by the wrong-date folding.

### 3.4 Re-run of the diversity study (current loader, no artifact write)
`build_grid` + Tasks 1–3 re-executed against the live book: `grid_rows = 3440`
(committed: 3440); **every** per-source `n`, Brier, ECE and dir_acc is identical to
the committed artifact; Task 2 flag sets identical; Task 3 identical
(`best_single = rate_1h`, `blend_brier 0.2068` vs `best_single_brier 0.1241`).

## 4. Answers to the requested before/after

| ask | answer |
|---|---|
| affected station-day count | **0 on the scored axis** (`hsf_at_pin`/strike set unchanged; full-window A≡C, 236,417 buckets, 0 diffs). The only divergence is the 17,974 buckets / 547 daily maxima that the *om_gfs* loader (B) produces by dropping the carry — all local-evening rows; the study loader itself has zero affected rows. |
| Brier table delta | **none** (byte-identical) |
| correlation-matrix delta | **none** (byte-identical) |
| Does "one forecast sampled five ways; rate_1h best-single" survive? | **YES** — rate_1h best single (Brier 0.0921; 0.1241 overlap), blend/bestNWP/gefsBrier identical; the p-cross matrix collinearity flags are unchanged. |

## 5. `om_gfs` validation loader (Q5)
**Not correct — a different bug, as originally suspected, but it is *this* loader
that is wrong.** `ldate = ds` attributes rows by UTC date, so all local-evening
rows (local ~17:00–24:00, i.e. the local daily max) land on the next UTC day's
bucket; measured divergence vs the correct loader is 17,974 buckets / 547 daily
maxima. It does **not** affect the published `om_gfs` verdict because every scored
metric there is taken at the 13:30-local pin (`hsf_at_pin`, mod ≤ 810) and
`hsf_at_pin` is byte-identical either way (0 diffs over all window station-days).
So the study's DO_NOT_WIRE verdict stands, but the loader's docstring/comment
blaming the study is backwards.

## 6. Recommended disposition (no code fix)
1. **Keep** `predictor_diversity_study_2026-10-13.py::load_asos` as-is (do not
   "simplify" to UTC date). Its floor-carry form is correct.
2. **Keep** the corrected notes already present in the working tree for
   `om_gfs_strike_crossing_test_2026-10-13.py` (which now states `ldate=ds` is
   *not* equivalent) — commit those, do not revert.
3. No re-run artifact change is needed; the committed
   `artifacts/predictor-diversity/predictor_diversity_2026-10-13.json` is accurate.

*Verification scripts (untracked scratch): `scripts/_verify_asos_dateattr.py`,
`scripts/_verify_asos_fullwindow.py`, `scripts/_verify_asos_axis.py`,
`scripts/_rerun_diversity_core.py`.*
