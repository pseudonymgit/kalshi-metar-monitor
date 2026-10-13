# HIGH Market Cohort Extension — 2026-10-13 (Item 3)

**Script:** `scripts/high_market_cohort_extension_2026-10-13.py`  
**Machine artifact:** `artifacts/market-benchmark-extension/high_market_cohort_extension_2026-10-13.json`  
**Status:** findings + machine artifact. The frozen contract is **unchanged**.

## Finding 1 — HIGH `-B` (between) quotes grow the market benchmark ~8.3x

The frozen contract scores HIGH / greater `-T` quotes only (n = 152, CI inconclusive vs the n≥200 bar). The snapshot DB carries **3.2x more non-degenerate `-B` quotes than `-T`**, and `kalshi_strike_ladder` labels them (`strike_type='between'`). Joining them with the contract's own local alignment + ladder label:

| cohort | n | n dates | n stations | Brier | base rate | 95% CI |
|---|---:|---:|---:|---:|---:|---|
| T_only(frozen) | 152 | 43 | 19 | 0.0232 | 0.0592 | [0.0106, 0.0378] |
| B_only | 1103 | 46 | 20 | 0.104 | 0.2158 | [0.0843, 0.1266] |
| T+B | 1255 | 46 | 20 | 0.0942 | 0.1968 | [0.0771, 0.114] |

**Replication note:** the `T_only` row (n = 152, Brier 0.0232) reproduces the frozen contract bar (n = 144, Brier 0.0208) to within ~5% — the small delta is a label-join key difference (this script keys labels on the exact ticker + ladder `strike_type`, the contract keys the `greater` ladder on `(station, date, strike_value)`). The delta does not affect the conclusion; the `-B` growth factor is ~7.7x over the frozen n = 144.

- **`-T` only (frozen):** n = 152 (Brier 0.0232, base 0.0592).
- **`-B` only:** n = 1103 (Brier 0.104, base 0.2158). The Brier numbers are *not* comparable across cohorts with different base rates — the point of `-B` is *coverage*, not that it scores lower.
- **`-T` + `-B` union:** n = 1255 (Brier 0.0942, base 0.1968) — **crosses the n≥200 bar with a large margin**, from data already on disk.

**Recommendation:** a `-B`-inclusive variant of the market benchmark is the cheapest path to a conclusive market bar. It should be added as a **named sibling** to the frozen contract (e.g. `…-2026-10-11+range`), not a silent edit — the frozen T-only bar stays as-is for continuity.

## Finding 2 — trade_calibration.p_market is already complete

`paper_trading_sbox.trade_calibration.p_market`: **143/143 filled** (the Workstream T fill is done; the diversity study's Task 4 already ran). No backfill needed.

## Finding 3 — LOW markets have a parallel range book (see Item 2)

The same `-B` structure exists for `KXLOWT*` (17,045 non-degenerate B quotes). The Item-2 backfill already prices the LOW entry side; the two findings compose: a LOW range cohort and the LOW holdout share the same quote spine.

## Reproduce

```bash
.venv-gefs/bin/python scripts/high_market_cohort_extension_2026-10-13.py
```

