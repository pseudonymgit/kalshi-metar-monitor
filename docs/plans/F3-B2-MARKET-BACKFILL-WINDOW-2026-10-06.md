# F3 — B2 Market Backfill Window: FORMAL GAP ACCEPTANCE (2026-10-06)

**Sprint:** F · **Item:** F3 (P1) — B2 market backfill
**Owner:** Ray Donovan · **Repo:** `prototypes/weather-engine-source/`
**Status:** candlestick window empirically bounded; gap formally accepted.

---

## 1. What was asked

Per Dan's order: try the Kalshi **candlesticks** API for pre-Aug-1 dates. If it
serves them, backfill. If it 404s, **formally accept the ~65-day window** as
B2's scope and document the gap.

## 2. Probe result — candlesticks 404 before the boundary

All probes run live against `https://api.elections.kalshi.com/trade-api/v2`
(2026-10-06), through the repo's own `market_snapshot_backfill.py` helpers.

| market close | ticker | candles |
|---|---|---|
| 2026-06-01 | `KXHIGHTBOS-26MAY31-T73` | **404** |
| 2026-07-20 | `KXHIGHTBOS-26JUL19-T84` | **404** |
| 2026-07-25 | `KXHIGHTBOS-26JUL24-T86` | **404** |
| 2026-07-28 | `KXHIGHTBOS-26JUL27-T85` | **404** |
| **2026-07-30** | `KXHIGHTBOS-26JUL29-T75` | **404** ← last rejected close |
| **2026-07-31** | `KXHIGHTBOS-26JUL30-T75` | **OK (3 daily)** ← first served close |
| 2026-08-01 | `KXHIGHTBOS-26AUG01-T89` | OK (2) |
| 2026-08-03 | `KXHIGHTBOS-26AUG02-T88` | OK (2) |

**Boundary (empirical, sharp):** the candlestick price series begins at market
close **2026-07-31**. Everything at/older than close **2026-07-30** returns
`404 Not Found`.

### Endpoints checked

| endpoint | result |
|---|---|
| `v2 /series/{series}/markets/{ticker}/candlesticks` (pre-boundary) | **404** |
| `v1 /candlesticks/historical` (the path named in the order) | **404 page not found** |
| `trading-api.kalshi.com/trade-api/v1/...` | **401 — "API has been moved to api.elections.kalshi.com"** |
| `v2 /markets/{ticker}` (single) | 404 |
| `v2 /markets/{ticker}/trades` | 404 |
| `v2 /historical/markets` (ticker discovery) | **200 — lists tickers back to ≥2022** |
| `v2 /historical/trades?ticker=...` | **200 — returns trade prints back to 2026-06-01** |

So the **candlestick** price source is strictly bounded, even though `/historical/markets`
still enumerates the old **tickers**. This confirms and tightens the E2 finding
(which had only sampled monthly).

## 3. Reachable window → B2 scope

| | window | days |
|---|---|---|
| live z8 snapshots (pre-E2) | 2026-09-07 → 2026-10-05 | ~29 |
| E2 backfill reach adds | 2026-08-01 → 2026-09-06 | ~37 |
| **DB now** (`min snapshot_time`) | **2026-08-01T04:00Z → 2026-10-06T19:30Z** | **67** |
| B2 target (frozen sweep) | 2026-03-09 → present | 210 |

**Reachable ≈ 67 days; unreachable gap = 2026-03-09 → 2026-07-30 (~144 days).**

### Decision (formatted acceptance)

**The ~65-day window IS B2's scope for now.** The pre-2026-08-01 gap is **not
recoverable from the public Kalshi candlestick API** and is formally accepted as
a known limitation. Do **not** re-litigate the candlestick path; it is bounded.

## 4. Residual option (documented, NOT auto-executed)

`/historical/trades` returns raw trade prints for pre-boundary tickers (verified:
`KXHIGHTBOS-26MAY31-T73` prints dated 2026-06-01). This is a *different* price
axis: taker trade prints (executed fill prices), not bid/ask **mid** snapshots.
Building a `mid_price` series from trade prints is a research item (spread
unobservable, sparse, not the live collector's `(bid+ask)/2` definition), and it
was **out of the order's scope** (F3 said candlesticks-else-accept). Recorded
here as the only known partial path to the missing 144 days. Dan/Gilfoyle call.

## 5. Verification (5-minute)

```bash
cd prototypes/weather-engine-source
.venv-gefs/bin/python - <<'PY'
import sys; sys.path.insert(0,'.'); sys.path.insert(0,'scripts')
import scripts.market_snapshot_backfill as mb, sqlite3
for tk,label in [("KXHIGHTBOS-26JUL29-T75","close 07-30"),
                 ("KXHIGHTBOS-26JUL30-T75","close 07-31")]:
    n=len(mb.candles_for("KXHIGHTBOS",tk,"2026-07-29T00:00:00","2026-08-02T23:59:59",1440))
    print(label, tk, "->", n, "candles")
c=sqlite3.connect('file:data/kalshi_market_snapshots.db?mode=ro',uri=True)
print("min snap:", c.execute("select min(snapshot_time) from market_snapshots").fetchone()[0])
print("days:", c.execute("select count(distinct substr(snapshot_time,1,10)) from market_snapshots").fetchone()[0])
PY
# -> close 07-30 -> 0 candles ; close 07-31 -> 3 candles
# -> min snap: 2026-08-01T04:00:00+00:00 ; days: 67
```

## 6. Files

| path | change |
|---|---|
| `docs/plans/F3-B2-MARKET-BACKFILL-WINDOW-2026-10-06.md` | this formal gap acceptance |
| `scripts/market_snapshot_backfill.py` | unchanged (E2 tooling; no code change needed) |

No code was changed for F3 — the probe resolved the question, and the answer is
"accept the window". Backfill remains runnable for the reachable span via the
existing E2 script.
