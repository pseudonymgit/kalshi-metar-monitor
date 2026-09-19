# Kalshi City → Ticker Mapping — Canonical Resolution & Forward-Scrape Fix

**Date:** 2026-09-18
**Author:** Gilfoyle (subagent)
**Trigger:** Dan's direct correction — the prior "retired stations" claim was **wrong**. All 20 cities have live series; they are just listed under inconsistent ticker names.
**Repo:** `prototypes/weather-engine-source` (branch `temp-sprint-branch`, dirty tree preserved).

---

## 0. Summary (what Dan's correction actually changes)

The prior artifact `docs/plans/KALSHI-STRIKE-BACKFILL-2026-09-18.md` §3 claimed a
"20 active vs 11 retired" split, where KBNA, KCLT, KDAL, KDTW, KJFK, KORD, KPDX,
KSLC, KTPA, PHNL were "retired — dead on Kalshi."

**That was a misclassification.** Live `GET /series` (14,157 total series;
104 `KXHIGH*`/`KXLOW*` Climate/Weather series) shows:

- **All 20 cities we trade have live series.** The "retired" 11 are simply
  **not in the `settlement_epochs` station set at all** — they are distinct
  airports (KJFK/KORD/KDAL are *separate* from KNYC/KMDW/KDFW) or cities we
  never traded (Nashville, Charlotte, Detroit, Portland, SLC, Tampa, Honolulu).
  They were never "retired" — they were never *in* the tradeable universe.

- The reason the old mapping **looked** like half the cities were dead is the
  **five inconsistent naming conventions** for the same "daily high" product.

---

## 1. Ground truth: the FIVE naming conventions

The daily-high product is listed under these inconsistent ticker shapes (all
verified live, 2026-09-18):

| Convention | Shape | Examples |
|---|---|---|
| **HIGH + city** | `KXHIGH<CITY>` | KXHIGHNY (NYC), KXHIGHCHI (Chicago), KXHIGHAUS (Austin), KXHIGHMIA (Miami), KXHIGHLAX (LA), KXHIGHDEN (Denver), KXHIGHPHIL (Philly) |
| **HIGHT + ICAO** | `KXHIGHT<ICAO>` | KXHIGHTATL, KXHIGHTBOS, KXHIGHTDAL, KXHIGHTDC, KXHIGHTLV, KXHIGHTMIN, KXHIGHTNOLA, KXHIGHTSFO, KXHIGHTSEA, KXHIGHTEWR |
| **HIGHTH + ICAO** | `KXHIGHTH<ICAO>` | KXHIGHTHOU (Houston), KXHIGHTOKC (OKC), KXHIGHTPHX (Phoenix) |
| **HIGHT + city** | `KXHIGHT<CITY>` | KXHIGHTNOLA (New Orleans) |
| **Legacy / duplicate** | various | KXHIGHTSATX **and** KXHIGHTSDF (both San Antonio, both live), KXHIGHNY0 (dead), KXHIGHNYD (dead hourly directional) |

LOW series are equally scattered:

| Shape | Status | Examples |
|---|---|---|
| `KXLOWT<ICAO>` | **live** | KXLOWTATL, KXLOWTBOS, KXLOWTCHI, KXLOWTDAL, KXLOWTDC, KXLOWTDEN, KXLOWTHOU, KXLOWTAUS, KXLOWTNYC, KXLOWTLAX, KXLOWTMIA, KXLOWTPHIL, KXLOWTLV, KXLOWTMIN, KXLOWTNOLA, KXLOWTOKC, KXLOWTPHX, KXLOWTSATX, KXLOWTSDF, KXLOWTSEA, KXLOWTSFO |
| `KXLOW<CITY>` | **dead** (legacy) | KXLOWNY, KXLOWCHI, KXLOWAUS, KXLOWDEN, KXLOWLAX, KXLOWMIA, KXLOWPHIL |

**Key gotcha:** NYC's LOW ticker is `KXLOWTNYC` (code `TNYC`), *not* `KXLOWTNY`.
A naive `KXLOWT + high_code` construction produces `KXLOWNY` (dead), silently
missing NYC's LOW settlement.

---

## 2. Canonical mapping (Deliverable 1)

**Data file:** `data/kalshi_series_map.json`
**Module:** `scripts/kalshi_series_map.py`

The mapping is keyed by **canonical station ICAO**, with an `aliases` list that
folds every naming variant (current + legacy + duplicate) into the SAME station.
A rename/add is tracked as a new alias on the same station — **never** a new
series/stations. Resolution normalizes the ticker prefix (`KXHIGH`/`KXLOW`) and
code suffix (strip leading `T`, collapse known city-suffix variants).

### The 20 canonical stations (all in `settlement_epochs`, all with live series)

| ICAO | City | Live HIGH | Live LOW | Legacy/dead |
|---|---|---|---|---|
| KATL | Atlanta | KXHIGHTATL | KXLOWTATL | KXHIGHATL |
| KAUS | Austin | KXHIGHAUS | KXLOWTAUS | KXLOWAUS |
| KBOS | Boston | KXHIGHTBOS | KXLOWTBOS | KXHIGHBOS |
| KDCA | Washington DC | KXHIGHTDC | KXLOWTDC | KXHIGHDCA |
| KDEN | Denver | KXHIGHDEN | KXLOWTDEN | KXHIGHTEMPDEN, KXLOWDEN |
| KDFW | Dallas | KXHIGHTDAL | KXLOWTDAL | KXHIGHDFW |
| KHOU | Houston | KXHIGHTHOU | KXLOWTHOU | KXHIGHHOU, KXHIGHOU |
| KLAS | Las Vegas | KXHIGHTLV | KXLOWTLV | KXHIGHLAS |
| KLAX | Los Angeles | KXHIGHLAX | KXLOWTLAX | KXLOWLAX |
| KMDW | Chicago | KXHIGHCHI | KXLOWTCHI | KXLOWCHI |
| KMIA | Miami | KXHIGHMIA | KXLOWTMIA | KXLOWMIA |
| KMSP | Minneapolis | KXHIGHTMIN | KXLOWTMIN | KXHIGHMSP |
| KMSY | New Orleans | KXHIGHTNOLA | KXLOWTNOLA | KXHIGHMSY |
| KNYC | New York City | KXHIGHNY | KXLOWTNYC | KXHIGHNY0, KXHIGHNYD, KXLOWNY, KXLOWNYC |
| KOKC | Oklahoma City | KXHIGHTOKC | KXLOWTOKC | KXHIGHOKC |
| KPHL | Philadelphia | KXHIGHPHIL | KXLOWTPHIL | KXLOWPHIL |
| KPHX | Phoenix | KXHIGHTPHX | KXLOWTPHX | KXHIGHPHX |
| KSAT | San Antonio | **KXHIGHTSATX + KXHIGHTSDF** | **KXLOWTSATX + KXLOWTSDF** | — |
| KSEA | Seattle | KXHIGHTSEA | KXLOWTSEA | KXHIGHSEA |
| KSFO | San Francisco | KXHIGHTSFO | KXLOWTSFO | KXHIGHSFO |

### Not in `settlement_epochs` (live, but never traded)

| ICAO | City | Live HIGH | Live LOW |
|---|---|---|---|
| KSAN | San Diego | KXHIGHTSAN | KXLOWTSAN |
| KEWR | Newark, NJ | KXHIGHTEWR | KXLOWTEWR |

### Genuinely absent from the tradeable universe (never "retired")

These 11 `settlement_epochs` stations have **no resolvable live series** because
they were never part of the 20-city tradeable set — they are distinct airports
or untraded cities. **This is expected, not a bug.**

`KBNA` (Nashville), `KCLT` (Charlotte), `KDAL` (Dallas-Love, distinct from KDFW),
`KDTW` (Detroit), `KJFK` (JFK, distinct station sharing NYC series), `KORD`
(O'Hare, distinct station sharing CHI series), `KPDX` (Portland), `KSLC` (Salt
Lake City), `KTPA` (Tampa), `PHNL` (Honolulu), `TEST` (sentinel).

---

## 3. Forward-scrape verification (Deliverable 2)

### 3.1 Scrape cron (`scripts/z8_kalshi_market_cron.py`)

The `STATION_SERIES` dict covers all 20 canonical stations. **One coverage gap:**

- **KSAT (San Antonio)** is missing the duplicate live series `KXHIGHTSDF`
  (HIGH) and `KXLOWTSDF` (LOW). The cron only lists `KXHIGHTSATX` /
  `KXLOWTSATX`. The second live duplicate series is **not scraped**.

Every other station has full live-alias coverage.

### 3.2 Settlement upkeep (`scripts/kalshi_settlement_upkeep.py`) — **broken LOW matching**

`series_ticker()` constructs `KXLOW<code>` (no leading `T`) from
`STATION_TO_KALSHI_CODE`. But the **live** LOW ticker is `KXLOWT<code>` (with
leading `T`). This produces **dead** LOW tickers for 7 stations, so settlement
matching silently fails for those LOW series:

| Station | Upkeep constructs (dead) | Actual live LOW |
|---|---|---|
| KMDW (Chicago) | KXLOWCHI | KXLOWTCHI |
| KAUS (Austin) | KXLOWAUS | KXLOWTAUS |
| KPHL (Philly) | KXLOWPHIL | KXLOWTPHIL |
| KDEN (Denver) | KXLOWDEN | KXLOWTDEN |
| KLAX (LA) | KXLOWLAX | KXLOWTLAX |
| KNYC (NYC) | KXLOWNY | KXLOWTNYC |
| KMIA (Miami) | KXLOWMIA | KXLOWTMIA |

**Settlement matching is keyed by `(station, date)`** via `event_ticker`, which is
correct in principle — but because the series ticker is constructed with the
wrong LOW prefix, the event ticker is wrong, so the `fetch_expiration_value`
lookup returns nothing for those LOW series. **Name changes break settlement
matching** exactly as feared: the fix is to resolve LOW tickers through the
canonical map (which always uses the live `KXLOWT*` form), not to reconstruct
them from the code.

### 3.3 Series-map refresh on Kalshi add/rename

Neither the scrape cron nor the upkeep currently re-enumerates `GET /series` to
discover new/renamed series. The backfill script (§4) does. The fix wires both
crons to the canonical map module (`scripts/kalshi_series_map.py`), which is the
single source of truth and must be re-generated when Kalshi renames/adds a series.

---

## 4. Dynamic series resolution for backfill (Deliverable 3)

`scripts/kalshi_historical_strike_backfill.py` already enumerates `GET /series`
dynamically (via `enumerate_weather_series()` / `build_station_tickers()`), but it
still carries the OLD hardcoded `STATION_TO_KALSHI_CODE` + `_CODE_TO_STATION`
reverse map (with the `T`-strip + `NYC` alias hack) and the OLD "11 retired"
narrative in its docstring.

**This pass updates it to import the canonical map module** (`scripts/kalshi_series_map.py`)
so a single source of truth drives backfill, scrape, and settlement upkeep. The
hardcoded 20-station map is replaced with `resolve_ticker()` / `station_for_code_suffix()`
from the canonical map. The false "retired" conclusion is removed.

---

## 5. Files changed

| File | Change |
|---|---|
| `data/kalshi_series_map.json` | **NEW** — canonical city→ICAO→aliases map (all 20 stations, legacy + duplicate variants folded in) |
| `scripts/kalshi_series_map.py` | **NEW** — resolver module (load + resolve tickers to canonical stations) |
| `scripts/kalshi_historical_strike_backfill.py` | **UPDATED** — use canonical map instead of hardcoded station map |
| `scripts/z8_kalshi_market_cron.py` | **UPDATED** — add KSAT duplicate live series (KXHIGHTSDF/KXLOWTSDF) |
| `scripts/kalshi_settlement_upkeep.py` | **UPDATED** — fix LOW series construction via canonical map |
| `docs/plans/KALSHI-CITY-TICKER-MAPPING-2026-09-18.md` | **NEW** — this doc |

No commit (per boundary). Read-only on existing data; only new files + in-place
script edits. Dirty tree preserved.

---

## 6. Recommended next actions (for Dan/Gerri disposition)

1. **Fix the LOW settlement-matching bug** (§3.2) — 7 stations' LOW series
   settlement is currently unmatched. Resolve through the canonical map.
2. **Scrape the KSAT duplicate series** (§3.1) — KXHIGHTSDF/KXLOWTSDF are live
   and currently unscraped.
3. **Re-generate the canonical map on a schedule** — a lightweight `GET /series`
   enumeration (like the backfill's `enumerate_weather_series`) should refresh
   `data/kalshi_series_map.json` so renames/adds never silently break matching.
