# Bankroll Compounding Simulation + Time-Stop q Distribution

**Date:** 2026-10-12
**Script:** `scripts/sbox_bankroll_timestop_qdist.py`
**Output:** `data/bankroll_timestop_combined_2026-10-12.json`
**Inputs (read-only):** `data/paper_trading_sbox.db` (live book), `data/trackA_spread_filter.json` (209 Track A trades), `data/kalshi_market_snapshots.db` (quotes).
**Discipline:** live book untouched; no temp DBs; all computation in memory; total output 68 KB.

---

## Query 1 — Bankroll-compounding simulation (the $16,769 fix)

### What was wrong

`rebuild_sbox_book_resized.py` did `new_pnl = position_size * old_pnl / 50`. That is a **static rescale**, not a bankroll simulation — it never lets the stake grow or shrink with the account, and it hard-codes `50` as the denominator. The question that actually matters: **start at $200 and run the book's resolved trades in chronological order, sizing each off the current bankroll** via the grading ladder.

### Method

- Start bankroll **$200.00**.
- Trades in `created_at_utc` order. Grade from `p_fused`:

| p_fused | grade | position |
|---|---|---|
| ≥ 0.9 | **S** | `min($25, bankroll × 0.125)` |
| 0.8 ≤ p < 0.9 | **A** | `min($15, bankroll × 0.075)` |
| 0.7 ≤ p < 0.8 | **B** | `min($10, bankroll × 0.05)` |
| 0.6 ≤ p < 0.7 | **C** | `min($5, bankroll × 0.025)` |
| < 0.6 | **F** | no entry |

- Net = `realized_pnl / old_position_size × new_position_size`; `bankroll += net`; stop if `bankroll ≤ 0`.
- The 14 `open` trades have no `realized_pnl` and are excluded (no invented outcome).

### Result

| Metric | Value |
|---|---:|
| Start bankroll | $200.00 |
| **Final bankroll** | **$18,484.30** |
| Total P&L | **+$18,284.30** |
| Return | **+9,142.15%** |
| Resolved trades | 68 |
| **Trades entered** | **63** |
| Skipped (grade F, p<0.6) | 5 |
| Open trades excluded | 14 |
| Bankrupt at any point? | **No** |
| Peak bankroll | $18,493.23 |
| Trough bankroll | $322.04 |
| **Max drawdown** | **$65.36 (0.36%)** |

**P&L curve (peak → trough → end):** **$18,493.23 → $322.04 → $18,484.30**. The trough is the low point after trade #209 (−$25.02); the account never retraces below $322 after the first day.

### Grade breakdown

| Grade | Entered | Net P&L |
|---|---:|---:|
| S (≥0.9) | 47 | +$15,572.33 |
| A (0.8–0.9) | 11 | +$2,282.03 |
| B (0.7–0.8) | 5 | +$429.94 |
| C (0.6–0.7) | 0 | $0.00 |
| F (<0.6) | 0 (5 skipped) | $0.00 |
| **Total** | **63** | **+$18,284.30** |

No C-grade trades exist in the book. Two book trades sit in the `[0.79, 0.80)` gap in the task's prose (0.7928, 0.7978); bands were applied contiguously (A ≥ 0.80).

### ⚠️ The number is real but the simulation degenerates

**The stake never compounds.** All 63 entries priced at `min(cap, bankroll × frac)` hit the **cap**, never the fraction:

- 47 × $25 (S), 11 × $15 (A), 5 × $10 (B) — **63/63 at cap**.

So this is **not** a compounding run. It is the *fixed-ladder* book (the resized book itself) simply replayed, summed, and added to $200. The "×0.125 of bankroll" rule never binds because the account only ever grows: $200 → $1,131 by the third trade, and the 12.5% trigger ($141) dwarfs the $25 cap forever after. Final = $200 + Σ(resized per-trade P&L).

**What this actually shows:** the book's *resolved* trades are a 74%-win-rate sample (47 S-grade wins vs a handful of −$25 losers) that happened to run hot. A $18.5k result from $200 is a **fixed-$25-stake sum**, not the product of a compounding strategy. Treat it as "what the resized book's realized P&L adds to a $200 account," **not** as a forward return.

---

## Query 2 — Time-stop q distribution (Track A-3 decisive test)

### The question

Track A-3 (`docs/weather-engine/trackA-3-timestop-2026-10-12.md`) showed the time-stop flips positive only if settlement losers can be exited at the next window at **`q ≥ 0.639 × entry_mid`**. The Track A artifact stores no post-entry quotes, so that verdict was "indeterminate." This query goes to `kalshi_market_snapshots.db` and measures the actual `q / entry_mid` for the 105 settlement losers.

### Data-depth caveat (this is the whole story)

The snapshot DB cadence is **not uniform**:

| Period | Snapshots/day | Timing |
|---|---|---|
| Aug 2026 | **1** | 04:00Z (before the 14/17/20Z entry) |
| Sep 1–7 | 1–84 | sparse |
| Sep 14 onward | ~24 | hourly |

A 04:00Z snapshot is **before** the entry, so an August loser's only "next snapshot" is the **following day — after settlement**, when the mid is pinned at ~0. Those are post-settlement prices, not time-stop quotes. Only September losers can carry a true pre-settlement next-window quote.

### Matching outcome (105 losers)

| Match kind | n | What it is |
|---|---:|---|
| **next_window** | **33** | first same-day snapshot ≥ entry+3h — a **real time-stop quote** |
| same_day_late | 1 | same-day snapshot after entry but < entry+3h |
| cross_day | 71 | only a following-day, post-settlement snapshot exists |
| no_quote | 0 | — |

### q distribution

| Subset | n | median q | mean q | p25 | p75 | min | max | q=0 | no quote | zero bid | ≥ 0.639 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **next_window (real quotes)** | **33** | **0.5556** | 0.5695 | 0.2105 | 0.7913 | 0.0164 | 1.6667 | 0 | 0 | 11 | 13 |
| cross_day (post-settlement) | 71 | 0.0496 | — | — | — | — | — | 0 | 0 | 72 | — |
| all 105 | 105 | 0.1429 | 2.2194 | 0.0435 | 0.8627 | 0.0105 | 33.33 | 0 | 0 | 83 | 34 |

*(`all 105` mean is dominated by a 33× outlier — a Sept loser whose entry mid was a penny and whose next-window mid rose; ignore the mean, use the median.)*

### Verdict: **NOT adoptable on this evidence**

- **Median q (next_window) = 0.5556 < 0.64.** The central estimate sits **below** the break-even line.
- Only **13 of 33** (39%) real next-window quotes clear 0.639. The rule needs the *median* above break-even, and it is not.
- **11 of 33** real next-window observations have **no bid** (q>0 mid but bid = 0) — i.e. you cannot actually sell there. Counting those as unexecutable drags the effective median lower still.
- The 71 cross-day August observations are post-settlement (median q ≈ 0.05) and are **not** valid time-stop quotes; they are reported separately precisely so they do not masquerade as evidence.

**Bottom line:** the decisive test Track A-3 asked for comes back **negative**. The time-stop is a mirage at this data depth — the losers' next-window mid sits at ~0.56× entry, below the 0.639× break-even, and a third of those quotes have no bid at all. The lever is **avoiding the settlement ride at entry** (signal-quality gate), not exiting it.

---

## Stitch — is the time-stop worth bolting onto the bankroll sim?

**No.** The bankroll simulation (Query 1) is the current book; the time-stop (Query 2) was the proposed fix for the Track A settlement leg. The q distribution fails its own break-even test (median 0.5556 < 0.639), so the "bankroll simulation *with* time-stop exits" final test is **not warranted** — it would be simulating an exit rule the market data says does not clear break-even.

If the time-stop were ever re-tested, it must be on a **September-only** cohort with hourly snapshots; the August data cannot answer it.

---

## Reproduce

```bash
python3 scripts/sbox_bankroll_timestop_qdist.py            # → data/bankroll_timestop_combined_2026-10-12.json
python3 scripts/sbox_bankroll_timestop_qdist.py --stdout   # print only, no write
```
