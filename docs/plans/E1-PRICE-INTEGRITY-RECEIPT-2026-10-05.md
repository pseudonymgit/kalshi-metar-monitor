# E1 — Price Integrity Trace: RECEIPT (2026-10-05)

**Sprint:** E — Price Integrity + Gamma Enable + TIGGE Restart
**Item:** E1 (P0) — trade-DB `market_price` must match the decision-time snapshot
**Owner:** Ray Donovan · **Status:** fixed & committed, pending Gilfoyle/independent review
**Repo:** `prototypes/weather-engine-source/`

---

## 1. What was asked

The KOKC trade-DB recorded `market_price = 0.025`, but the market snapshot at that
time showed `mid_price = 0.005` — a 5× discrepancy. Trace the price source in the
SBOX bridge, fix the path, and make every trade carry its snapshot provenance.

## 2. Root cause (proven, not asserted)

**File:** `scripts/sbox_intraday_ev_bridge.py` → `strike_ladder()` (~line 156)

The query is `ORDER BY snapshot_time DESC` (newest first). The loop then did:

```
for tk, mid, ask, vol in rows:
    if strike in seen: continue          # dedup
    px = mid if mid is not None else ask
    if px <= 0.02 or px >= 0.98: continue   # <-- WALK-BACK
    if (vol or 0) < min_volume: continue    # <-- WALK-BACK
    seen[strike] = {...}
```

The dedup check ran **before** the price/volume filters but the strike was only
written to `seen` **after** they passed. So when a strike's *newest* snapshot was
filtered out (penny price ≤ 0.02, or thin volume), the loop `continue`d **without
marking the strike decided** and kept scanning *older* snapshots — pricing the
strike from a stale row, potentially many hours/days old.

### The KOKC case, reproduced exactly

| field | value |
|---|---|
| trade_uuid | `9b50b640-2a26-445a-bbc1-1f006ed6a619` |
| ticker | `KXHIGHTOKC-26OCT05-T78` |
| trade created | `2026-10-05T14:15:46Z` |
| decision-time snapshot (13:30) | mid **0.005** (bid 0.0 / ask 0.01) |
| booked `market_price` | **0.025** |
| walk-back source found | `2026-10-04T14:30:00Z` mid **0.025** |

Reproduction command (pre-fix) printed:
```
T78 RESOLVED: {'yes_price': 0.025, ..., 'snapshot_time': '2026-10-04T14:30:00.842012+00:00'}
```

The trade was priced off a snapshot ~**24 hours stale**. At decision time the true
price was 0.005 (≤ 0.02), so the honest ladder was **empty** — the bug did not just
mispricing, it *manufactured a trade that honest pricing would have declined*.

### Frequency (whole SBOX trade-DB, 47 trades)

`scripts/verify_price_provenance.py`:

| verdict | count |
|---|---|
| MATCH | 19 |
| **STALE_WALK** | **26** |
| MISMATCH (side/label inversion, separate defect) | 2 |
| NO_TICKER / NO_SNAPSHOT | 0 |

**26 of 47 booked trades (55%) were priced off a non-decision-time snapshot.**
26/26 STALE_WALK rows resolve to an exact older snapshot whose mid equals the booked
price. 19 older trades MATCH (bug is recent; behavior drifted as more strikes went
sub-penny).

## 3. The fix

`scripts/sbox_intraday_ev_bridge.py`:

1. **Dedup-first (core fix).** A `decided` set marks a strike the moment its
   *newest* snapshot is read — before any filter. If that newest price is filtered,
   the strike is simply absent from the ladder; the code **never walks back in time**.
   Net effect: latest snapshot is authoritative.
2. **Provenance in the ladder.** Each ladder entry now returns `snapshot_time`.
3. **Provenance on the trade.** `record_trade(..., price_source=...)` writes a new
   additive `price_source` column on `trades`:
   `kalshi_market_snapshots.db:market_snapshots.mid_price@<snapshot_iso>|mid=<px>`
   (idempotent `ALTER TABLE`, applied via `_ensure_price_source_column`).

## 4. Verification (5-minute)

```bash
cd prototypes/weather-engine-source
.venv-gefs/bin/python scripts/verify_price_provenance.py
```

Pre-fix data → prints `STALE_WALK: 26` and exits **1** (gate trips).
Post-fix, every newly recorded trade → MATCH (its `price_source` names the exact
snapshot it was priced from). Exit **0**.

Reproduce the specific KOKC resolution:
```bash
.venv-gefs/bin/python -c "
import sys; sys.path.insert(0,'.'); sys.path.insert(0,'scripts')
import scripts.sbox_intraday_ev_bridge as b
lad = b.strike_ladder('KOKC','26OCT05', before_iso='2026-10-05T14:15:46.829937+00:00')
print('T78 present:', any(s['strike']==78.0 for s in lad))  # -> False (honest)
"
```

## 5. Known residuals (flagged for review, not silently fixed)

- **2 MISMATCH trades are a side/label inversion**, not walk-back:
  `KXHIGHTATL-26OCT05-T77` booked `0.105 = 1 − mid(0.895)`; `KXHIGHTBOS-26OCT01-T75`
  booked `0.335 ≈ 1 − mid(0.665)`. Market mid was ~0.895/0.665 and the row is
  `buy_yes` with `signal_direction=UP`. Either the side label or the price axis is
  inverted. **Needs a decision from Gilfoyle/Dan** — separate from E1's walk-back.
- **Historical rows keep `price_source = NULL`** until a reconciliation backfill
  runs (deliberate: additive-only commit; no retroactive mutation of settled data).
- `min_volume=50` and `px<=0.02` thresholds are unchanged — E1 only changed the
  walk-back behavior, not the liquidity policy.

## 6. Files

| path | change |
|---|---|
| `scripts/sbox_intraday_ev_bridge.py` | dedup-first ladder + `snapshot_time` + `price_source` write |
| `scripts/verify_price_provenance.py` | NEW — reconciliation verifier (gating) |
| `docs/plans/E1-PRICE-INTEGRITY-RECEIPT-2026-10-05.md` | this receipt |
