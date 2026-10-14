# Settlement Axis Truth — 2026-10-14

**Lane:** Sprint — Lane B (technical)
**Owner:** Bertram Gilfoyle
**Repo:** `prototypes/weather-engine-source/` (branch `main`)
**Book:** `data/paper_trading_sbox.db`
**Backups:** `data/paper_trading_sbox.db.pre-axisfix-20261014T164338Z`,
`data/paper_trading_sbox.db.pre-resettle-<ts>`
**Commit (code):** `584de4363`

---

## 0. TL;DR — the task's premise was WRONG, and the real bug is bigger

The fix-sprint task (from the 2026-10-14 external bug-hunt, finding #3) said:

> "Kalshi daily temp markets are offered as … **'or above'/'or below'** levels —
> i.e. **inclusive**. HIGH YES = `settled_temp >= strike`. Our code uses strict `>`.
> Fix `>` → `>=`."

**Both halves of that are false, verified against Kalshi's OWN live API today.**

1. **Kalshi is NOT inclusive.** Ties are **LOSSES**, not wins. See §1.
2. **The real bug is a `strike_type` axis inversion, not an operator.** A Kalshi
   daily-high event publishes **two** tails — "or above" (`strike_type='greater'`)
   and "or below" (`strike_type='less'`) — and the `-T##` ticker suffix is used for
   **both**. The engine assumed every `-T##` tail was a `greater` market under HIGH,
   which **inverts the label on every `less` tail**. That is **76 of 91 rows**, not
   the 14 tie rows the task named. See §2.

**Result:** the strict operator is **correct and kept**; the axis is fixed. The book
re-settles **76 rows** and moves from **+40.34 → −5.59** constant-1-contract dollars.
Every re-settled outcome is cross-checked against Kalshi's own `result` field.

---

## 1. The tie rule is STRICT (`>` / `<`) — `>=` is refuted by Kalshi's own data

Method: resolve every book ticker against the Kalshi trade API
(`api.elections.kalshi.com/trade-api/v2/markets/{ticker}` and the per-event ladder),
which returns the market's authoritative `strike_type`, `floor_strike`, `cap_strike`,
`expiration_value`, and settled `result`.

**Kalshi's own titles are exclusive:**

| ticker | strike_type | floor | cap | title | result | expiration_value |
|---|---|---:|---:|---|---|---:|
| `KXHIGHTPHX-26OCT10-T96` | greater | 96 | – | "…be **>96°**…" | **no** | 96 |
| `KXHIGHTATL-26AUG18-T92` | less | – | 92 | "…be **<92°**…" | **no** | 92 |
| `KXHIGHTMIN-26OCT12-T81` | less | – | 81 | "…be **<81°**…" | **no** | 81 |
| `KXHIGHTBOS-26OCT12-T71` | greater | 71 | – | "…be **>71°**…" | **no** | 71 |
| `KXHIGHTBOS-26OCT11-T60` | less | – | 60 | "…be **<60°**…" | **no** | 60 |
| `KXHIGHNY-26OCT12-T69` | less | – | 69 | "…be **<69°**…" | **no** | 70 (no tie) |

**Every observed tie settles `result='no'`** — on both tails. Confirmed
independently over the authoritative `data/kalshi_strike_ladder.db`: **1,093 tie
rows, 1,093 `result='no'`, zero `result='yes'`.**

The help-page phrase "temperature ranges plus 'or above'/'or below' levels"
describes the **contract set** (an unbounded end tier), not an inclusive operator.
Kalshi's own market titles and rules say "greater than" / "less than".

> **Consequence:** the 12 closed + 2 open tie rows the task expected to flip
> loss→win **stay LOSSES**. Flipping them to wins would have been wrong.

---

## 2. The real bug — operator taken from `market_type`, not `strike_type`

`core/settlement_labels.py` (and every consumer) resolved the operator from
`market_type`:

```
HIGH: settlement > strike     # assumed a "greater"/or-above tail
LOW:  settlement < strike
```

But `market_type='HIGH'` only says the market is keyed to the **daily high**; it does
**not** say the tail is "or above". On the same event Kalshi lists both:

```
KXHIGHCHI-26OCT09-T71   strike_type='less'     cap=71    KMDW high 74 → result='no'
KXHIGHCHI-26OCT09-T78   strike_type='greater'  floor=78  → result='no'
```

A **HIGH buy_yes on `-T71` is an "or below" bet** (won iff high < 71). The engine
booked it as "high > 71" — a **LOSS recorded as a WIN**.

### Blast radius (every resolved book row)

| | count |
|---|---:|
| book tickers resolved to a real `strike_type` | **91 / 91** |
| rows whose book outcome ≠ Kalshi's own `result` | **76** |
| — of those, booked WIN but truly LOSS | **64** |
| — of those, booked LOSS but truly WIN | **7** |
| rows whose book outcome already agreed | 15 |
| `xcheck` — temp + resolved axis agrees with Kalshi `result` | **all resolved rows** |

Rows by axis: **78 pure-API `result`**, **10 authoritative ladder `result`**,
**3 new open rows** (axis from the live API; result pending).

---

## 3. Code fix (commit `584de4363`)

- `core/settlement_labels.py`
  - `settle_yes_from_strike_type(observed, strike_type, floor, cap)` — resolves
    the operator from the market's **own** `strike_type`; strict on both tails
    (`greater`: `observed > floor`; `less`: `observed < cap`;
    `between`: `floor <= observed <= cap`); raises on an unknown type or a missing
    bound (no silent operator).
  - `strike_crossing_yes(...)` gains optional `strike_type`/`floor_strike`/`cap_strike`;
    the legacy `market_type` fallback is retained for pre-axis callers.
- `core/canonical_settlement.py`
  - `canonical_settle_yes` / `canonical_outcome` accept the axis and resolve from
    `strike_type`; new `outcome_from_market_result()` consumes Kalshi's own
    settled `result`.
- **new `core/strike_axis.py`** — resolve a ticker's `strike_type` from the
  authoritative `data/kalshi_strike_ladder.db` (by ticker, then
  station/type/date/strike). Read-only.
- `core/pnl_tracking.py` — forward settle resolves the market's own axis; **fails
  closed** when the axis cannot be resolved.
- `core/additive/outcomes.py` — passes the row's `strike_type` through.
- `tests/test_settlement_axis.py`, `tests/test_strike_crossing_axis.py` — pin the
  strict tie rule **and** the operator-from-`strike_type` matrix.

---

## 4. Re-settle (`scripts/resettle_axis_truth_2026-10-14.py`)

Truth precedence (read-only): Kalshi live-API settled `result` → authoritative
ladder `result`/`settlement_value_dollars` → **fail closed** (row untouched).
91/91 rows resolved; no row fabricated.

Writes (`trades` only): `settlement_outcome`, `canonical_outcome`, `settled_value`
(kept = settlement **temperature**), `realized_pnl` (**constant-1-contract**:
`buy_yes (S−price)·1`, `buy_no (price−S)·1`, `S∈{0,1}`),
`settlement_return_amount`, `settlement_confirmed=1`, `canonical_settled_f`,
`canonical_source`, `status='closed'`, plus provenance `strike_type`,
`strike_axis_source`. Old values preserved in **`canonical_outcome_old_2026`**,
**`settlement_outcome_old_2026`**, **`settled_value_old_2026`**,
**`realized_pnl_old_axis_2026`**, **`status_old_2026`**.

### Book P&L — before / after (constant-1-contract)

| metric | before | after |
|---|---:|---:|
| `SUM(realized_pnl)` (honest field) | **+40.335** | **−5.585** |
| of which rows that changed | (71 rows preserved in `realized_pnl_old_axis_2026`, sum 40.83) |
| original qty-basis P&L (`realized_pnl_old_2026`) | 18,871.65 | 18,871.65 (untouched) |

Net: **56 rows flip WIN→LOSS, 7 flip LOSS→WIN, 13 unchanged, 5 settled from open.**
`PRAGMA integrity_check` = **ok**. Every closed row's `realized_pnl` matches its
`S`/`trade_price`/`trade_type` (0 mismatches).

### Tie rows (the task's named 14) — resolved as LOSSES

| id | ticker | axis | temp | strike_type result | book before | now |
|---:|---|---|---:|---|---|---|
| 195 | `KXHIGHTPHX-26OCT10-T96` | greater | 96 | no | (contradictory) | incorrect |
| 206 | `KXHIGHTATL-26AUG18-T92` | less | 92 | no | correct | incorrect |
| 209 | `KXHIGHAUS-26AUG01-T96` | less | 96 | no | correct | incorrect |
| 224 | `KXHIGHTDC-26AUG11-T89` | less | 89 | no | correct | incorrect |
| 229 | `KXHIGHTDC-26SEP29-T74` | less | 74 | no | correct | incorrect |
| 238 | `KXHIGHDEN-26SEP20-T74` | greater | 74 | no | correct | incorrect |
| 247 | `KXHIGHCHI-26SEP19-T72` | less | 72 | no | correct | incorrect |
| 249 | `KXHIGHMIA-26SEP17-T84` | less | 84 | no | correct | incorrect |
| 252 | `KXHIGHNY-26SEP08-T80` | less | 80 | no | correct | incorrect |
| 257 | `KXHIGHTSEA-26SEP18-T69` | less | 69 | no | correct | incorrect |
| 259 | `KXHIGHTSFO-26SEP16-T66` | less | 66 | no | correct | incorrect |
| 266 | `KXHIGHTBOS-26OCT11-T60` | less | 60 | no | correct | incorrect |
| 268 | `KXHIGHTMIN-26OCT12-T81` | less | 81 | no | open | incorrect |
| 270 | `KXHIGHTBOS-26OCT12-T71` | greater | 71 | no | open | incorrect |

All **14 stay LOSSES** (strict ties). None flips to a win.

### The 3 "no canonical truth" rows — now settled from the API

Rows 272 (KLAX), 273 (KSFO), 276 (KSEA) for 2026-10-13 previously had no
`kalshi_settlements` row and were left open. The Kalshi API now returns their
finalized `result`/`expiration_value`, so they settle from ground truth:
`KXHIGHLAX-26OCT13-T75` (greater 75, exp 73 → no), `KXHIGHTSFO-26OCT13-T75`
(greater 75, exp 74 → no), `KXHIGHTSEA-26OCT13-T60` (less 60, exp 62 → no). All
three are buy_yes LOSSES.

---

## 5. What this changes downstream (flagged for Dan)

- **Every previously-published win-rate / P&L number built on this book is
  invalid** — the book's outcomes were ~86% wrong for the less-tail rows.
- This is a **premise refutation**: the sprint was authorized to make the book
  *inclusive* (`>=`); instead the honest evidence shows the book must be *strict*
  and *axis-resolved*. Both are **more conservative** than the premise (fewer wins).
- **Open question for Dan:** the tie rule was recorded as "PENDING ON DAN." The
  evidence now says **strict**. If Dan still wants `>=` on policy grounds, the
  re-settle is one flag away — but it would disagree with Kalshi's own settlement.

---

## 6. Deliverables

- **Code:** commit `584de4363` (`core/settlement_labels.py`,
  `core/canonical_settlement.py`, `core/strike_axis.py`, `core/pnl_tracking.py`,
  `core/additive/outcomes.py`, the two axis tests).
- **Tool:** `scripts/resettle_axis_truth_2026-10-14.py` (`--dry-run` supported).
- **Truth cache + plan:** `artifacts/settlement-axis-truth/`.
- **Backups:** two `paper_trading_sbox.db.pre-*` copies + `_old_2026` columns.
