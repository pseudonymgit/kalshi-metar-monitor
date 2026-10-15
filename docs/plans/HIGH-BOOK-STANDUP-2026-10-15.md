# HIGH book stand-up — arm state, cron, dry-run

**Date:** 2026-10-15 · **Owner:** Gilfoyle · **Repo:** `prototypes/weather-engine-source/`
**Verify script:** `scripts/verify_high_bridge_arm_2026-10-15.py` · **Artifact:** `artifacts/high-bridge-arm-2026-10-15.json`
**Book:** `data/paper_trading_sbox.db` (currently **0 rows** — the fresh lane)
**No live trades placed.** The dry-run is the verification.

## 1. Arm state — ARMED ✅

| check | required | observed |
|---|---|---|
| `USE_OM_GFS_NWP` in `.env` | `1` (om_gfs sole forecast) | **1** |
| `sole_forecast_enabled()` | True | **True** |
| sizing path | Kelly `_position_size_usd` | **True** (delegates to `kelly_position_size`; sample inputs match; no-edge → $0) |
| settle writer contract-notional | `(payout − price) × qty` | **True** (`core/pnl_tracking.py`; commit `c8b6ab2a7` is an ancestor of HEAD) |
| `LOW_BRIDGE_ENABLED` | OFF/unset | **0** (gate in `low_entry_bridge.run` refuses to book when unset) |
| booking gate | enabled | **True** (`WE_BOOKING_GATE` default 1) |
| book write path | reachable | **True** (`trades` table has `quantity`, `position_size_usd`, `lane`) |

`ARMED=True`.

## 2. Cron — exists and is correct; no new entry needed

`9dcbee27-353a-406e-9566-88e25a1d5482` — **WE-SBOX-paper-trading-intraday**

- schedule `15 14-21 * * *` UTC (hourly at :15, in the trading window)
- enabled, `lastRunStatus=ok`, 0 consecutive errors
- runner: `sh -lc "bash scripts/we_cron_preflight.sh .venv-gefs/bin/python scripts/sbox_intraday_ev_bridge.py"`
  — **points at the current script** (`scripts/sbox_intraday_ev_bridge.py`), cwd = repo root
- writes the HIGH lane into `data/paper_trading_sbox.db`

The book write path was proven end-to-end on a **temp copy** (see §4): 8 HIGH trades
written with correct `× quantity` notional and custody hashes; the live DBs were
untouched (md5s unchanged).

**No cron change was required.**

## 3. Dry-run today — 0 would-book (expected; we are out of window)

`--dry-run --ignore-window` at 01:35 UTC 2026-10-15:

```
date_utc 2026-10-15   n_executed 0   n_skipped 20
skip_reasons {no_strike_ladder: 20}
```

**This is the correct / expected result at this hour, not a defect.** It is 01:35 UTC —
outside the 14–21 UTC entry window. `local_day()` resolves most stations to the
near-settled local date (2026-10-14), whose `-T##` strikes are pinned to
0.005 / 0.995 and therefore filtered by the ladder's `px<=0.02 / px>=0.98` rule. An
empty ladder at the settled edge is the designed behaviour.

## 4. In-window verification — the pipeline computes and books

Two complementary checks confirm the pipeline works when it is actually supposed to run:

**(a) Historical dry-run** at in-window instants (clock simulated to the cron's own timestamps):

| instant (UTC) | would-book | top skip reasons |
|---|---:|---|
| 2026-10-14T14:15 | 6 | no_strike_ladder 6, outside_station_window 6 |
| 2026-10-14T16:15 | 9 | no_strike_ladder 9, no_positive_ev_strike 2 |
| 2026-10-14T18:15 | 8 | no_strike_ladder 10, no_positive_ev_strike 2 |
| 2026-10-14T20:15 | 8 | no_strike_ladder 12 |
| 2026-10-13T18:15 | 9 | no_strike_ladder 9, entry_engine_pass 2 |
| 2026-10-14T21:15 (last cron run) | 0 | no_strike_ladder 13, entry_engine_pass 7 |

At a representative in-window instant the bridge computes forecasts (`om_gfs` sole),
finds strikes, sizes by Kelly, and **would book ~6–9** HIGH trades. (Dan's "0–2"
expectation understates the live pipeline: it books one per station that clears the
EV / entry-timing / grade gates, capped at 3 open/station and 10 open/day.)

**(b) Full non-dry booking path** on a temp DB copy, 2026-10-14T18:15:

```
8 rows written to /tmp copy, lane=HIGH, status=open, position_size_usd=10.0
buy_yes/buy_no, quantity 16..400, custody_hash populated
live data/paper_trading_sbox.db unchanged (0 rows)
live data/gate_ledger.db, data/trade_custody.db unchanged
```

This is the strongest evidence: the booking gate admits, `record_trade` writes, and
`trade_custody` gets a hash — the "empty book stays empty" risk is **not** a code path.

## 5. Why the book is empty (and why it will fill)

- **Worktree** book: **0 rows**.
- **HEAD-committed** book: **50 rows** (7 open for 2026-10-14, rest closed), ids 187–283.
- The worktree book was emptied at **2026-10-14T23:37** — *after* the last cron run
  (21:15). That is the deliberate reset to stand up the fresh lane, not a data loss.

The next cron run (**14:15 UTC today**) runs inside the window against the fresh
2026-10-15 station-days, so the new book begins accruing its honest win/loss record
automatically. **No action needed to make it fill.**

## Verdict

- Arm state: **ARMED** (om_gfs sole, Kelly, contract-notional settle, LOW off).
- Cron: **present and pointed at the current script**; no new entry needed.
- Dry-run today: **0 would-book** (out of window — expected).
- In-window: **computes and books ~6–9**; full write path proven on a temp copy.
- Live trades: **none placed** (per instruction).
