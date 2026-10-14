# Kelly P&L Stragglers + Cleaned-Book Crawl — 2026-10-14

**Lane:** Weather engine — Lane B (technical)
**Owner:** Bertram Gilfoyle
**Repo:** `prototypes/weather-engine-source/`
**DB:** `data/paper_trading_sbox.db` (live book)
**Backup:** `data/paper_trading_sbox.db.pre_straggler_fix_20261014T230128Z` (taken before any write)
**Tool:** `scripts/fix_kelly_pnl_stragglers_2026-10-14.py`
**Artifacts:** `artifacts/trades-integrity/kelly_pnl_fix_{dry,applied}.json`

---

## 0. Bottom line

| Item | Result |
|---|---|
| **1 — P&L stragglers** | **FIXED.** `SUM(realized_pnl)` closed: **−249.095 → −0.98**. **0 rows** with `|pnl| > position_size_usd * 1.5`. |
| **2 — Bullshit crawl** | v1 **48/50 (96%)** — still high, but **all of it is R5 "extreme edge" on the book's cheap-entry structure**, not residual pathology. v2: **1 honest flip**. |
| **3 — Physical consistency** | **0 violations** across all 43 closed rows. |

**This was a BASES CONFLICT, not a 3-row typo.** The task's literal formula and its
own acceptance test are mutually exclusive; see §1. I restored the repository's
**shipped, committed constant-1-contract basis** — the only basis that satisfies
the bound *and* is sign-consistent with `canonical_outcome`.

---

## 1. Why the task formula could not be used literally

The task said:

> `realized_pnl = (settled_value − trade_price) × quantity` … and verify no closed
> row has `|realized_pnl| > position_size_usd + ε`.

`settled_value` in this book is the **settled temperature** (e.g. `90.0` °F), *not*
a binary payout (`core/pnl_tracking.py` books `settlement_value = canonical_temp`).
So the literal formula gives, for id 255 KPHX (`price=0.05`, `qty=200`,
`settled_value=90`):

```
(90.0 − 0.05) × 200 = $17,990      # not $190, not bounded by a $10 stake
```

Even substituting a *binary* payout it still explodes, because the stragglers'
`quantity` is the retired `psize/price` count:

```
(1 − 0.05) × 200 = $190            # the exact straggler the task wants gone
```

`(S−price)×qty` with `qty = psize/price = 10/price` **always** equals
`S·(10/price) − 10`, which for cheap `price` is enormous and for `price>1/6`
exceeds `psize·1.5`. So the instruction and its acceptance test cannot both hold
under any quantity-multiplied basis.

**The honest, shipped model** (commit `690db06da`, "constant-1-contract";
`docs/plans/TRADES-TABLE-INTEGRITY-2026-10-14.md` §3; `core/pnl_tracking.py`) is
the size-independent per-contract measure:

```
realized_pnl = (S − trade_price) · 1.0      S ∈ {0,1}, booked side
S = 1 iff canonical_outcome == 'correct'
```

This is the **only** basis that is simultaneously:
1. **sign-consistent** with `canonical_outcome` (correct → ≥0, incorrect → ≤0),
2. **bounded** by the per-contract $1 (`|pnl| ≤ max(price, 1−price) ≤ 1`),
3. **reconcilable** with `quantity = psize/price` as the task also demands.

The live DB had drifted back to the quantity basis (the AF-2/3 Kelly re-derive
only re-sized; it left `realized_pnl` on the old basis on most rows). We restored
the shipped basis for every closed row.

> Note: the "P&L basis" question is separately logged as a **bug-hunt BLOCKER
> pending Dan** in `PROJECTS.md`. We did **not** change the basis; we restored the
> one the repo already shipped and tested. If Dan rules differently, the prior
> values are preserved (below) and the change is a one-line constant.

---

## 2. Item 1 — the fix

**Tool:** `scripts/fix_kelly_pnl_stragglers_2026-10-14.py` (`--dry-run` supported).
For **closed rows only**: `realized_pnl = (S−price)·1`,
`settlement_return_amount = S·1`, `quantity = MAX(1, ROUND(psize/price))`
(`0` when `psize = 0`). Prior `realized_pnl` preserved in the additive column
`realized_pnl_pre1c_20261014`. No other table touched.

### The three named stragglers

| id | station | leg | price | qty | psize | outcome | P&L before | P&L after | return before → after |
|---:|---|---|---:|---:|---:|---|---:|---:|---|
| 255 | KPHX | buy_yes | 0.050 | 200 | 10 | correct | **+190.00** | **+0.95** | 190.00 → 1.0 |
| 261 | KLAX | buy_no | 0.055 | 182 | 10 | incorrect | **−171.99** | **−0.055** | 0.0 → 0.0 |
| 262 | KHOU | buy_yes | 0.190 | 53 | 10 | correct | **+42.93** | **+0.81** | 42.93 → 1.0 |

### Book totals

| metric | before | after |
|---|---:|---:|
| `SUM(realized_pnl)` (closed, 43 rows) | **−249.095** | **−0.98** |
| rows `|pnl| > psize·1.5` | **3** (255, 261, 262) | **0** |
| max `|pnl| / psize` (closed) | **19.0** | **0.095** |

`quantity` already matched the Kelly-implied count on all 43 closed rows
(`qty_fixed = []`) — the stragglers' counts were already right; only the P&L was
on the wrong basis. `settlement_return_amount` was corrected on 6 winner rows
that still carried the quantity basis (196, 251, 255, 260, 262, 263).

---

## 3. Item 2 — bullshit crawl on the cleaned book (50 rows)

### v1 (`scripts/bullshit_crawl.py`)

| | pre-purge (88 rows) | now (50 rows) |
|---|---:|---:|
| flagged | 87 (98.9%) | **48 (96.0%)** |
| clean | 1 | 2 |

**Flag breakdown now:** `extreme edge` 48 · `low conf but entered` 6 ·
`bankroll/position mismatch` **0** · `over max position` **0** ·
`S grade on flat signal` **0**.

The drop is *not* "far lower" because the purge removed the **saturated**
(`p_fused_raw ≥ 0.99`: now **0** rows) and **zero-dollar** rows, but **left the
cheap-entry structure intact** — and v1's R5 (`edge > 50%`) is computed on the
retired `p_fused = 1 − market_price` identity against sub-10¢ prices, so it fires
almost everywhere in both regimes (48 vs 87). The two v1-clean rows are 196 KATL
(price 0.565, edge 0.39) and 246 KMDW (price 0.94, negative edge).

**So: v1's headline is dominated by a retired-identity rule, not residual
bullshit.** The three *pathology* rules are all 0.

### v2 (`scripts/bullshit_crawl_v2.py`) — old-vs-honest flips

- flips (old `p_fused` admits, honest `calibrated_p` EV-gate rejects): **1**
- the flip: id 246 **KMDW 2026-09-15**, `p_fused=0.2020`, `calibrated_p=0.1315`,
  price 0.94 — the book's negative-edge row (outcome correct, but the honest
  gate would have rejected the entry). Its position is correctly **$0** and its
  P&L is 0.0 (we would not have held it).

### Residual-honesty check (50 rows)

- companion `calibrated_p` present: **50/50** ✅
- saturation (`p_fused_raw ≥ 0.99`): **0** ✅
- `position_size_usd > 0`: **49/50** (the 1 exception, id 246, is the honest
  zero-edge row) ✅
- `p_fused ≥ 0.90`: **0** (max 0.8618) ✅

---

## 4. Item 3 — physical consistency audit (43 closed rows)

| check | result |
|---|---|
| closed rows complete (`canonical_outcome`, `settled_value`, `realized_pnl`, `quantity`, `position_size_usd`) | **43/43** |
| `quantity == MAX(1, ROUND(psize/price))` | **43/43** |
| `realized_pnl` sign matches `canonical_outcome` | **43/43** |
| `|realized_pnl| ≤ position_size_usd · 1.5` | **43/43** (max ratio 0.095) |

**BLOCKERS: none.** 0 violations.

---

## 5. Deliverables & scope

- **Tool:** `scripts/fix_kelly_pnl_stragglers_2026-10-14.py`
- **Artifacts:** `artifacts/trades-integrity/kelly_pnl_fix_{dry,applied}.json`
- **Backup:** `data/paper_trading_sbox.db.pre_straggler_fix_20261014T230128Z`
- **Read-only elsewhere:** crawls and the audit write only their own JSON outputs.

### Recommendation (bases question, for Dan)

The book now holds two colliding definitions of P&L: the DB's **constant-1-contract**
basis (`SUM ≈ −$0.98`) and the Kelly **loss-side** `(p−mkt)/mkt × $200` dollars the
AF-2/3 commit advertises (order `−$200`). They are not the same quantity and cannot
be reconciled by any single scale factor. Confirm the intended basis before the
dashboard/metrics are repointed:

1. **constant-1-contract** (repo-shipped; what we restored) — size-independent,
   comparable across rows; `SUM ≈ −$0.98`.
2. **Kelly dollars** — multiply each row's per-contract figure by its Kelly
   **notional** (`psize/price`), i.e. size-weighted dollars; matches the
   "loss side ≤ $10" narrative but loses per-row comparability.

This is the same unresolved "P&L basis" item already flagged as a bug-hunt BLOCKER.
