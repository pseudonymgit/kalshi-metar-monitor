# Kalshi Historical-Strike Backfill — Endpoint Verdict & Plan (rev 2)

Date: 2026-09-18
Author: Gilfoyle (subagent)
Approved by: Dan — read-only, script-only, optimized, parallel.
Repo: `prototypes/weather-engine-source` (branch `temp-sprint-branch`, dirty tree preserved).

> **REV 2 — corrected.** The rev-1 plan (and the prior script) targeted `/events`
> + `/markets?event_ticker`, which has a ~7.5-month retention window and was
> **wrong**. The data-hunter (KALSHI-HISTORICAL-DATA-ACQUISITION-2026-09-18.md)
> found the correct endpoint: **`GET /historical/markets`** returns settled
> markets back to **August 2021**, free and unauthenticated, with the strike
> embedded in every market object. This doc and the script now use that.

---

## 1. Endpoint verdict (probed live, 2026-09-18)

### The correct endpoint: `GET /historical/markets`

| Probe | Result |
|---|---|
| `/historical/markets?series_ticker=KXHIGHNY` (paginated) | **9,214 markets**, `2021-08-06 → 2026-07-18` |
| `/historical/markets?series_ticker=KXHIGHCHI` | **9,211 markets**, `2021-08-20 → 2026-07-19` |
| `/markets?series_ticker=KXHIGHNY` (live tail) | **420 markets**, `2026-07-12 → 2026-09-19` |
| `strike_type` / `floor_strike` / `cap_strike` | **embedded per market** — no reconstruction |

Each historical market object carries `strike_type` (`greater`/`less`/`between`),
`floor_strike`, `cap_strike`, `settlement_value_dollars`, `result`, `close_time`,
`settlement_ts`. The strike is read directly — the round-number reconstruction
(`round_number_anchoring.py`) is now only a sanity check, never the source.

> **Note:** `KXHIGHNY` and `KXHIGHCHI` are the **only two** series that reach 2021.
> The other 18 active cities launch their HIGH series between 2023-08 and 2026-04
> (see §4). The endpoint itself has no retention limit — it's the *series* that
> didn't exist yet.

### Live/historical partition (rolling)

- Cutoff is **~`2026-07-20T00:00:00Z`** and advances over time. Markets settled
  *before* it live only in `/historical/*`; *after* it in `/markets`.
- So we merge **both** per series: `/historical/markets` (deep, 2021→cutoff) +
  `/markets` (recent tail, cutoff→now).
- `/historical/markets` supports only `limit`, `cursor`, `tickers`,
  `event_ticker`, `series_ticker`, `mve_filter`. `min/max_settled_ts` are
  **silently ignored** (verified) — paginate by `cursor` newest-first to reach 2021.

### What rev-1 got wrong (and why this is now fixed)

Rev-1 assumed `/events` was the only entry point and that retention was a hard
~7.5-month cap. Both wrong:
- `/events` returns events with ~7.5-month retention, but `/historical/markets`
  returns the *settled market* history back to 2021 — a separate endpoint family.
- "7.5 months" was the live endpoint's *window*, not a data-expiry limit. The
  full 2021→present span is recoverable.

---

## 2. Strike semantics (authoritative, from market JSON)

| `strike_type` | `floor_strike` | `cap_strike` | `strike_price` (engine) |
|---|---|---|---|
| `greater` | integer (e.g. 86) | NULL | `floor_strike` |
| `less` | NULL | integer (e.g. 79) | `cap_strike` |
| `between` | lower (85) | upper (86) | `(floor+cap)/2` |

- **Strike type is independent of HIGH/LOW series.** A `KXLOW*` series still has a
  `greater` market. Do not infer from series direction.
- The engine trades **`greater`** (floor) for HIGH series and **`less`** (cap) for
  LOW series; `between` is a bin fallback. This matches the existing 56 populated
  rows (`greater`→32, `less`→24).

### Date mapping (verified)

Market ticker `KXHIGHNY-26JUL18-T86` → suffix `-26JUL18` = **trading date**
`2026-07-18` (= `settlement_epochs.local_trading_date`). `close_time` is the next
day (`2026-07-19T04:59Z`), `settlement_ts` the next day noon (`2026-07-19T12:01Z`).
The `-YYMMMDD` suffix is the day the temperature was measured.

---

## 3. Station mapping (the tricky part — inconsistent ticker naming)

Kalshi series tickers are `KXHIGH<CODE>` / `KXLOW<CODE>`. `<CODE>` is **NOT** the
ICAO and is **NOT** predictable. Verified naming pattern:

- **HIGH series:** `T` prefix for some cities (`KXHIGHTATL`, `KXHIGHTBOS`,
  `KXHIGHTDC`), no `T` for others (`KXHIGHNY`, `KXHIGHCHI`, `KXHIGHLAX`).
- **LOW series:** *always* `KXLOWT<key>` (leading `T`). The non-`T` LOW variants
  (`KXLOWNY`, `KXLOWCHI`, `KXLOWLAX`, `KXLOWMIA`, `KXLOWDEN`, `KXLOWAUS`,
  `KXLOWPHIL`) are **dead** tickers — 0 markets.
- **NYC alias:** HIGH code is `NY` (`KXHIGHNY`), but LOW code is `TNYC`
  (`KXLOWTNYC`), *not* `TNY`. A naive `KXLOWT + high_code` construction silently
  misses NYC's LOW series.

The authoritative station→code map lives in `core/kalshi_price_fetcher.py`
(`STATION_TO_KALSHI_CODE`). The script **reverses** it by normalizing each code
(strip all leading `T`s → canonical city key) and adds a `NYC→KNYC` alias. This
resolves **all 40 alive tickers** across the 20 active stations.

### 20 active stations (mapped, backfillable) vs 11 retired

The 31 stations in `settlement_epochs` break into:

- **20 active** — map to live Kalshi series: KATL, KAUS, KBOS, KDCA, KDEN, KDFW,
  KHOU, KLAS, KLAX, KMDW, KMIA, KMSP, KMSY, KNYC, KOKC, KPHL, KPHX, KSAT, KSEA, KSFO.
- **11 retired** — no resolvable series (dead on Kalshi): KBNA, KCLT, KDAL, KDTW,
  KJFK, KORD, KPDX, KSLC, KTPA, PHNL, + `TEST` (single sentinel row).

The retired 11 end at `2026-08-04` (or `2025-08-27` for KDAL) and are
**unrecoverable** — their tickers no longer resolve. This is expected, not a bug.

### Series enumeration (dynamic, not hardcoded)

The script enumerates `GET /series` (cursor-paginated), filters to category
`Climate and Weather` + `KXHIGH*`/`KXLOW*` prefix (excluding non-weather namesakes
like `KXHIGHINFLATION`, `KXHIGHMOVDJT`, `KXLOWESTRATE`), then maps each alive
ticker's code → station via the reverse map, intersected against
`settlement_epochs`. A hardcoded 20-station loop would silently miss ~half due to
the inconsistent naming.

---

## 4. Real sizing (rev 2 — full per-city coverage, probed 2026-09-18)

**Critical correction to the data-hunter's "~50 cities back to Aug 2021" claim.**
That is true of the *total* Kalshi universe (50+ cities, many international), but
**for the 20 US stations actually in `settlement_epochs`, HIGH-series launch dates
vary from 2021 to 2026** — only 2 cities have 2021 history.

### HIGH series launch dates (oldest settled market, probed live)

| Launch window | Cities | # |
|---|---|---|
| **2021-08** | KNYC (08-06), KMDW/CHI (08-19) | 2 |
| **2023-08** | KAUS, KMIA | 2 |
| **2024-12** | KDEN, KPHL | 2 |
| **2025-02** | KLAX | 1 |
| **2026-01/02** | KDCA (01-14), KLAS, KMSY, KSEA, KSFO (02-01) | 5 |
| **2026-04** | KATL, KBOS, KDFW, KHOU, KMSP, KOKC, KPHX, KSAT | 8 |

### LOW series launch dates

LOW series did **not exist before Dec 2025**. Verified:
- **7 cities** → `2025-12-13`: KAUS, KDEN, KLAX, KMDW (CHI), KMIA, KNYC, KPHL.
- **13 cities** → `2026-04-03`: KATL, KBOS, KDCA, KDFW, KHOU, KLAS, KMSP, KMSY,
  KOKC, KPHX, KSAT, KSEA, KSFO.

### Backfillable row counts (settlement_epochs ∩ actual listing span)

| Metric | Rev-1 (wrong, /events) | Rev-2 (correct, /historical/markets) |
|---|---|---|
| HIGH backfillable rows | ~4,560 | **~10,400** |
| LOW backfillable rows | ~4,560 | **~4,100** |
| **Total backfillable** | **~9,120** | **~14,400** |

Dry-run (actual fetch) confirms: **14,265 rows** map to existing
`settlement_epochs` rows (14,431 fetched → 14,265 after idempotent key filter).
The analytical ceiling is ~14,500; the ~235-row gap is live-tail boundary noise.

### What is genuinely unreachable
- The **11 retired stations** (KBNA, KCLT, KDAL, KDTW, KJFK, KORD, KPDX, KSLC,
  KTPA, PHNL) + `TEST` — their tickers no longer resolve on Kalshi. ~50k rows
  unrecoverable.
- **Pre-launch HIGH rows** — e.g. KATL has `settlement_epochs` HIGH rows back to
  2021-01-01, but Kalshi only listed `KXHIGHTATL` from 2026-04-01. The ~5.5 years
  of KATL HIGH rows before 2026-04 are **not backfillable** (Kalshi never listed
  them). This is the dominant reason the total is ~14.4k, not ~43k.
- **Pre-Dec-2025 LOW rows** — LOW series genuinely did not exist before then.

`settlement_epochs` holds 117,557 total rows; ~14,400 are backfillable. The
remaining ~103k rows are retired stations, pre-launch HIGH, or pre-launch LOW.

---

## 5. ETA (rev 2)

| Run | API calls | ETA |
|---|---|---|
| `--probe` | ~4 (1 series depth + series enumeration) | **~10 s** |
| `--dry-run` | ~40 series × (~1-10 hist pages + ~1 live page) ≈ **~120** | **~6 min** (measured: 343 s) |
| Full `--parallel 10` | same fetch as dry-run (already full) + upsert | **~6 min** |

At `--parallel 10` with the 1.5s inter-request throttle (~6.7 req/s), the full run
is **~6 minutes** (measured dry-run 343 s). The historical pagination is bounded by
the per-city launch depth: only KNYC (10 pages) and KMDW (10 pages) paginate back
to 2021; the other 18 cities are 1-2 pages each. This is far lighter than the
~43k-row estimate because most cities launched recently.

---

## 6. Deliverable: script

`scripts/kalshi_historical_strike_backfill.py` — standalone, deterministic, no AI in the loop.

### Modes
```
python3 scripts/kalshi_historical_strike_backfill.py --probe            # endpoint verdict + one-series sample
python3 scripts/kalshi_historical_strike_backfill.py --dry-run          # scope only, NO writes
python3 scripts/kalshi_historical_strike_backfill.py --parallel 10      # real backfill, no commit
python3 scripts/kalshi_historical_strike_backfill.py --parallel 10 --commit  # opt-in commit
```

### Safety guarantees (unchanged from rev-1, per boundaries)
- **Read-only on existing data** — only `UPDATE settlement_epochs SET strike_price=?, strike_type=?` on the 2 strike columns; never touches any other column or table.
- **No commit** — writes to SQLite via an open connection but does **not** call `.commit()`. Changes roll back on exit unless `--commit` (off by default).
- **Idempotent** — keyed upsert by `(station, market_type, local_trading_date)`.
- **Preserves dirty tree** — script writes only new files (`scripts/kalshi_historical_strike_backfill.py`, this doc); never `git add`/`commit`.
- **No live trading** — read-only GET requests to the public API; no order/trade endpoints touched.
- **No auth needed** — `/historical/markets` is unauthenticated. (The rev-1 `--auth` RSA-PSS mode was removed as unnecessary; it never extended the window anyway.)

### Key implementation changes (rev-1 → rev-2)
1. **Endpoint:** `/historical/markets` (cursor-paginated, `limit=1000`) replaces
   `/events` + `/markets?event_ticker`.
2. **Merge:** each series is fetched from BOTH `/historical/markets` (deep) and
   `/markets` (recent tail), grouped by trading date, deduped.
3. **Station map:** reverse of `STATION_TO_KALSHI_CODE` with `T`-strip normalization
   + `NYC` alias, intersected against `settlement_epochs` stations.
4. **Strike extraction:** reads `strike_type`/`floor_strike`/`cap_strike` directly
   (no reconstruction); collapses a date's markets to the median `greater`/`less`
   rung the engine trades.
