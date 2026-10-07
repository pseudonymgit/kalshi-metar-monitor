# WE Register Reconciliation — 2026-10-07

**Repo:** `prototypes/weather-engine-source/`
**Author:** Ray Donovan · **Sprint:** F close-out
**Scope:** all-inclusive snapshot of every outstanding/closed Weather Engine
workstream after the F cycle. Supersedes session-scoped subsets.

Boundary note: this file is a **reconciliation snapshot** — it states current
state per workstream and the evidence. Where a workstream has a dedicated
receipt, the receipt is the source of record and this file points to it.

---

## CLOSED (this sprint cycle — 38 commits, ~14 workstreams)

| ID | Workstream | State | Evidence |
|---|---|---|---|
| **R2** | single-authority chain | CLOSED | one writer authority for the settlement/signal chain; no dual-supervisor path |
| **R3** | 306 consecutive cycles | CLOSED | 306-cycle streak held; no drift |
| **M1** | core persistence (latched) | CLOSED | `9f830f9ac` — persist CORE evaluator results to `additive_signal_results` |
| **M7** | enrichment (ASOS + NWP) | CLOSED | `37aa380c0` — schema drift + path wiring fixed |
| **M8** | calibration rerun | CLOSED | 80/80 signals nonzero |
| **R6** | Layer 4 phantom watcher | CLOSED | `89733615b` — `phantom_watch.py` observer; `f14727259` salvaged tests |
| **E1** | price integrity fix | CLOSED (proven post-fix) | `4a21c9312` — strike_ladder dedup-first (no stale walk-back) + `price_source` provenance; receipt `docs/plans/E1-PRICE-INTEGRITY-RECEIPT-2026-10-05.md` |
| **E2** | market backfill | CLOSED (67 days) | `47065bc4b` — candlestick snapshot backfill; receipt `docs/plans/E2-MARKET-BACKFILL-RECEIPT-2026-10-05.md` |
| **E4** | gamma scoring enabled | CLOSED | `eb52720e7` — G1–G6 scoring-only bridge; receipt `docs/plans/E4-GAMMA-ENABLE-RECEIPT-2026-10-05.md` |
| **E5** | TIGGE restart | CLOSED (coord fix proven) | `40ed15886` — re-backfill restart receipt; receipt `docs/plans/E5-TIGGE-RESTART-RECEIPT-2026-10-05.md` |
| **E6** | trajectory cell scoring | CLOSED | `ecf3c7518` — score anticipatory CELL only (killed 91% base-rate headline); receipt `docs/plans/E6-TRAJECTORY-CELL-RECEIPT-2026-10-05.md` |
| **F1** | post-fix gate proven | CLOSED | `17a2fd703` — cohort-scoped verifier; post-fix cohort n=2 clean, exit 0 |
| **F2** | side-inversion fix | CLOSED | `17a2fd703` — `record_trade` labels direction/trade_type from EV-selected leg (`yes`→UP/buy_yes, `no`→DOWN/buy_no) |

---

## IN PROGRESS / PENDING

| ID | Workstream | State | Next action |
|---|---|---|---|
| **M4** | fired-once latch | working — 25 fired | awaiting one peak-cycle confirmation |
| **F1/F2 follow-up** | `buy_no` gap + committed test | **DONE 2026-10-07** (this sprint) | `_expected_band` maps `buy_no`→NO band; 5-case test committed (`tests/test_f2_side_inversion.py`) |
| **Historical annotation** | stale-walk + inversion rows | **DONE 2026-10-07** (this sprint) | 28 rows `status='flagged'` + `reconciliation_log`; `scripts/flag_bad_price_rows.py` |

---

## BLOCKED / NEEDS YOU

| ID | Workstream | Blocker | Detail |
|---|---|---|---|
| **B2** | real-price sweep | 67-day window | 144 days (2026-03-09 → 2026-07-30) unreachable from Kalshi **candlesticks**. `/historical/trades` returns **fills**, not mid-prices — a different data axis. See §B2 below. |
| **Settlement integrity** | 19 settled rows where P&L contradicts label | needs a dedicated reconciliation lane | flagged by Gilfoyle; **separate** from the inversion fix; not yet fixed |
| **Trajectory** | zero predictive edge above climatology | decision (Gray Room candidate) | keep as **nowcast**; do not promote as predictor |

---

## PARKED / DEFERRED

- **G1–G6** gamma live enable — scoring only, no edge yet
- **H1–H7** microstructure
- **L7** spread-aware execution
- **D2/D3** rebuilds
- **E7** sigma test conversion (killed)
- **Ω1–Ω9** promotion gates — blocked on everything above

---

## §B2 — Data-source reconciliation (Item 4, probed live 2026-10-07)

**Question:** can `/historical/trades` supply pricing data for the pre-2026-08-01
gap? It is the only lead that might reach pre-Aug-1.

**Probe result — the endpoint WORKS pre-boundary:**

| ticker | result |
|---|---|
| `KXHIGHTBOS-26MAY31-T73` | **200** — 20 prints, span 2026-05-31 → 2026-06-01 |
| `KXHIGHTBOS-26FEB20-T43` | **200** — 36 prints, span 2026-02-19 → 2026-02-20 |
| `KXHIGHTOKC-26FEB20-T62` | **200** — 65 prints |
| `KXHIGHNY-26FEB20-T46` | **200** — 100 prints |
| `KXHIGHCHI-26FEB20-T52` | **200** — 100 prints |
| `KXHIGHLAX-26FEB20-T63` | **200** — 100 prints |
| `KXHIGHTATL-26FEB20-T80` | **200** — 100 prints |

Reach confirmed **≥ 2026-02-19** (the oldest `close_time` returned for
`KXHIGHTBOS` is 2026-02-21). Paginated via a `cursor` field.

**Trade-print shape** (per print): `created_time`, `yes_price_dollars`,
`no_price_dollars`, `count_fp`, `taker_side`, `taker_outcome_side`,
`taker_book_side`, `is_block_trade`, `trade_id`.

**BUT — it is a different axis (confirmed):**

1. Prints are **taker fills**, not bid/ask **mid** snapshots. The live collector
   (`z8_kalshi_market_cron.py`) defines `mid_price = (yes_bid+yes_ask)/2`; the
   prints carry no bid/ask at all.
2. **Sparse** — a print exists only when a trade executes; no continuous series.
3. **Spread unobservable** — you get the executed price, not the book.
4. Historical **candlesticks stay bounded**: `/historical/markets/{tk}/candlesticks`
   → **400**, `/historical/candlesticks` → **404**, `/series/.../candlesticks` → **400**
   (the path exists but rejects out-of-window markets). Confirms E2/F3: the
   `mid_price` series cannot be reconstructed from Kalshi for pre-2026-07-31.
5. The historical **market object** carries terminal-only quotes
   (`yes_bid=0.00`, `yes_ask=1.00` post-settlement) — not a time series.

**Scoping decision (proposed):** build a **separate** store
`data/kalshi_historical_trade_prints.db` with `provenance=trade_print` rows
(ticker, trade_id, created_time, yes_price, no_price, size, taker_side).
Do **not** merge into `market_snapshots.mid_price` — keeps the axis distinction
intact. B2 may then treat "last-trade price" as an *alternate* price source for
the missing 144 days, clearly labeled as not-mid. **Dan/Gilfoyle call** whether
B2's sweep accepts a trade-print axis; until then, **B2 remains scoped to 67 days.**

---

## Files (this sprint, 2026-10-07)

| path | change |
|---|---|
| `scripts/verify_price_provenance.py` | `_expected_band` now maps `buy_no` → NO band (F1/F2 follow-up) |
| `tests/test_f2_side_inversion.py` | new — 5-case F2 functional test + verifier-band coupling |
| `scripts/flag_bad_price_rows.py` | new — historical bad-row annotation (status=flagged + `reconciliation_log`) |
| `data/paper_trading_sbox.db` | 28 rows annotated `status='flagged'`; `reconciliation_log` populated |
| `docs/plans/WE-REGISTER-RECONCILIATION-2026-10-07.md` | this file |
