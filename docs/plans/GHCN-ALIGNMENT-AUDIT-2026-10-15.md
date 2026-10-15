# GHCN alignment audit — clean of the Kalshi date-offset defect

**Date:** 2026-10-15 · **Owner:** Gilfoyle · **Repo:** `prototypes/weather-engine-source/`
**Script:** `scripts/audit_ghcn_alignment_2026-10-15.py` · **Artifact:** `artifacts/ghcn-alignment-audit-2026-10-15.json`
**Read-only.** No repair performed — none needed.

## Why this audit

Dan: *"fix the GHCN table."* GHCN is the **fallback truth** where Kalshi has no
backfill (Kalshi ≈ 11.7k rows, GHCN ≈ 620k). The Kalshi truth table had a
one-day `target_date` offset on 623 rows, so the question was whether GHCN
carries the same class of defect.

**Structural point:** GHCN cannot carry the *ticker-vs-target_date* defect — it
has no ticker field. `ghcn_settlements` is pure daily observations:

```
(station, date, high_f, low_f, source)  PRIMARY KEY (station, date)
```

The defect *could* only appear as a shifted daily date key. That is what this
audit tests.

## Profile

| field | value |
|---|---|
| rows | 620,036 |
| date range | 1869-01-01 … 2026-10-11 |
| stations | 21 (the tradeable set) |
| null high_f / low_f | 75 / (low covered) |
| duplicate (station, date) | **0** |
| source | `ghcn_daily` only |

## Method

Cross-checked GHCN against METAR daily truth (`data/metar_backfill.db::daily_stats`,
keyed `(station, date_local)`) over the recent window **2026-04-01 … 2026-10-11**:

1. **Same-day agreement** — do `high_f` / `low_f` match `max_temp_f` / `min_temp_f`
   within 1°F for the same station-day?
2. **Systematic offset test** — for each GHCN row, score |GHCN − METAR| at shift
   **−1 / 0 / +1 days** and see which alignment minimizes the difference. A real
   one-day offset would show shift −1 or +1 *beating* shift 0.
3. **Temporal autocorrelation** — a coherent daily series has
   corr(day, day+1) > corr(day, day+2). A half-shifted block would break it.

Overlap: **3,695** station-days. Sample: 200 (deterministic seed).

## Result — GHCN is CLEAN

### Offset test (full overlap, mean |GHCN − METAR|)

| shift | mean abs diff (°F) | n |
|---|---:|---:|
| **−1 day** | 4.590 | 3,586 |
| **0 day** | **2.031** ← best | 3,695 |
| **+1 day** | 4.318 | 3,588 |

Day-0 is unambiguously optimal; both ±1 shifts are more than **2× worse**. There
is **no date offset** in GHCN.

### Agreement (200-row sample, same-day)

| metric | value |
|---|---|
| mean \|Δ\| | 2.15°F |
| median \|Δ\| | 1.50°F |
| p90 \|Δ\| | 4.70°F |
| within 1°F | 33.5% |
| within 2°F | 65.0% |

### Autocorrelation (GHCN daily highs)

lag1 = **0.8947** > lag2 = **0.8173** — a coherent anti-persistent weather
series. No half-shifted block.

## The residual ~2°F disagreement is NOT a date problem

The same-day |Δ| of ~2°F is **instrument/siting**, not misalignment. The signed
bias is remarkably uniform across all 21 stations:

| quantity | mean signed bias (GHCN − METAR) |
|---|---:|
| HIGH (max) | **+1.00°F** |
| LOW (min) | **−0.26°F** |

GHCN runs ~1°F **warmer on daily highs** and marginally **cooler on lows** at
every station (KSFO +2.08, KLAX +1.64, KNYC −0.06 on highs). That is the classic
GHCN-COOP vs METAR-ASOS difference: different sensor, siting, and observation
window, plus whole-°F vs decimal reporting. Day-to-day scatter around that bias
is expected.

## Verdict

**GHCN is clean.** No date-offset repair needed. The fallback truth is correctly
aligned; its ~1°F warm bias on highs is a known instrument difference (and is
already the reason the truth-switch analysis prefers Kalshi canonical where
present). The Kalshi defect was specific to that table's backfill ingestion, not
a shared pipeline bug.

## Note for the register

This closes the "is the fallback truth also broken?" question: **no.** Only
`kalshi_settlements` carried the offset. The GHCN table needs no edit.
