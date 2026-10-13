#!/usr/bin/env python3
"""
market_microstructure_timing_2026-10-13.py — Item 2 of the Brier improvement sprint.

QUESTION
  data/kalshi_market_snapshots.db holds ~3.27M snapshot rows. Hypothesis: entering
  when the bid-ask spread is narrowest gives better fills. Is there a predictable
  time-of-day when spreads are narrowest? Does it vary by station? Does the 13:30
  station-local decision pin land inside the narrow-spread window?

WHAT IT DOES
  * Loads NON-DEGENERATE quotes (yes_bid>0, yes_ask<1, mid strictly in (0,1)).
  * spread      = yes_ask - yes_bid          ($ per contract, the round-trip cost)
    rel_spread  = spread / mid               (fraction of notional; price-conditioned)
  * Aggregates median spread by station-hour (UTC and station-local).
  * Reports the narrowest-spread window per station and globally.
  * Locates the 13:30 station-local decision pin in UTC per station and compares
    the pin-hour spread to the station's minimum.
  * Quantifies "dead re-polls": consecutive snapshots where the quote is unchanged.

READ-ONLY on the live book. Writes ONLY:
  data/market_timing_analysis.json

Usage: python3 scripts/market_microstructure_timing_2026-10-13.py
"""
from __future__ import annotations

import json
import sqlite3
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from core.station_time import station_timezone_name  # noqa: E402

SNAPS = REPO / "data" / "kalshi_market_snapshots.db"
OUT = REPO / "data" / "market_timing_analysis.json"

STATIONS = [
    "KATL", "KAUS", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU", "KLAS", "KLAX",
    "KMDW", "KMIA", "KMSP", "KMSY", "KNYC", "KOKC", "KPHL", "KPHX", "KSAT",
    "KSEA", "KSFO",
]
PIN_LOCAL_MIN = 13 * 60 + 30     # 13:30 station-local


def ro(p: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def median(xs):
    return round(st.median(xs), 5) if xs else None


def main():
    tz = {s: ZoneInfo(station_timezone_name(s)) for s in STATIONS}
    c = ro(SNAPS)
    rows = c.execute(
        "SELECT station, market_ticker, snapshot_time, yes_bid, yes_ask, mid_price, "
        "volume_24h, open_interest FROM market_snapshots "
        "WHERE yes_bid > 0 AND yes_ask < 1 AND mid_price > 0 AND mid_price < 1 "
        "AND market_type='binary'").fetchall()
    c.close()

    # per station-hour accumulators
    abs_spread = defaultdict(list)      # (station, hour_utc) -> [spread]
    rel_spread = defaultdict(list)
    nm_abs = defaultdict(list)          # near-money mid in [0.2,0.8]
    nm_rel = defaultdict(list)
    hour_utc_all = defaultdict(list)
    hour_local_all = defaultdict(list)
    per_ticker = defaultdict(list)      # ticker -> [(ts, mid, spread)]
    pin_abs = defaultdict(list)         # (station, local_hour) near-money spread
    liq_vol = defaultdict(list)         # hour_utc -> [volume_24h]
    liq_oi = defaultdict(list)          # hour_utc -> [open_interest]
    buck_spread = defaultdict(list)     # (hour_utc, mid_decile) -> [spread]

    n_rows = 0
    for station, tk, stt, yb, ya, mid, vol, oi in rows:
        try:
            dt = datetime.fromisoformat(stt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        n_rows += 1
        spread = float(ya) - float(yb)
        if spread <= 0:
            continue
        rel = spread / float(mid)
        hub = dt.hour
        tzs = tz.get(station)
        if tzs is None:
            continue
        loc = dt.astimezone(tzs)
        hl = loc.hour
        abs_spread[(station, hub)].append(spread)
        rel_spread[(station, hub)].append(rel)
        hour_utc_all[hub].append(spread)
        hour_local_all[hl].append(spread)
        per_ticker[tk].append((dt.timestamp(), float(mid), spread))
        buck_spread[(hub, min(9, int(float(mid) * 10)))].append(spread)
        if vol is not None:
            liq_vol[hub].append(float(vol))
        if oi is not None:
            liq_oi[hub].append(float(oi))
        if 0.2 <= mid <= 0.8:
            nm_abs[(station, hub)].append(spread)
            nm_rel[(station, hub)].append(rel)
            pin_abs[(station, hl)].append(spread)

    # pin hour per station (13:30 local -> UTC hour)
    pin_hour = {}
    for s in STATIONS:
        try:
            d = datetime(2026, 8, 1, 13, 30, tzinfo=tz[s])
            pin_hour[s] = d.astimezone(timezone.utc).hour
        except Exception:
            pin_hour[s] = None

    # global hourly profile (UTC)
    g_hourly = {h: {"n": len(v), "median_spread": median(v),
                    "mean_spread": round(sum(v) / len(v), 5)} for h, v in sorted(hour_utc_all.items())}
    g_hourly_nm = {}
    for h in sorted(hour_utc_all):
        vals = [x for (s, hh), v in nm_abs.items() if hh == h for x in v]
        g_hourly_nm[h] = {"n": len(vals), "median_spread": median(vals)}

    # per-station hourly profile (near-money = most comparable across the day)
    per_station = {}
    for s in STATIONS:
        prof = {}
        for h in range(24):
            v = nm_abs.get((s, h), [])
            if not v:
                continue
            prof[h] = {"n": len(v), "median_spread": median(v),
                       "median_rel_spread": median(nm_rel.get((s, h), []))}
        if not prof:
            continue
        best_h = min(prof, key=lambda h: prof[h]["median_spread"])
        ph = pin_hour[s]
        per_station[s] = {
            "pin_hour_utc": ph,
            "pin_median_spread": prof.get(ph, {}).get("median_spread"),
            "narrowest_hour_utc": best_h,
            "narrowest_hour_spread": prof[best_h]["median_spread"],
            "hourly": prof,
        }

    # dead re-poll quantification
    dead = live = 0
    changes = []
    for tk, pts in per_ticker.items():
        pts.sort()
        prev_mid = None
        for _, mid, _sp in pts:
            if prev_mid is not None:
                if abs(mid - prev_mid) < 1e-9:
                    dead += 1
                else:
                    live += 1
                    changes.append(abs(mid - prev_mid))
            prev_mid = mid

    # global narrowest window (UTC)
    glob_best = min(g_hourly, key=lambda h: g_hourly[h]["median_spread"])
    prod = {
        "n_non_degenerate_rows": n_rows,
        "n_tickers": len(per_ticker),
        "spread_definition": "yes_ask - yes_bid ($/contract); rel = spread/mid",
        "global_hourly_utc": g_hourly,
        "global_hourly_utc_near_money": g_hourly_nm,
        "global_narrowest_hour_utc": glob_best,
        "global_narrowest_spread": g_hourly[glob_best]["median_spread"],
        "global_widest_hour_utc": max(g_hourly, key=lambda h: g_hourly[h]["median_spread"]),
        "dead_repolls": {"unchanged": dead, "changed": live,
                         "pct_dead": round(100.0 * dead / max(1, dead + live), 2),
                         "median_change": median(changes)},
        # price-MATCHED spread by mid-decile, narrow window (0-13Z) vs pin window (14-20Z).
        # Rules out a composition artifact: if overnight quoting merely favoured
        # cheap strikes, matched bins would show no difference. They DO show one.
        "price_matched": {
            str(b): {
                "narrow_0_13Z": {"n": len([x for (h, bb), v in buck_spread.items()
                                             if bb == b and h < 14 for x in v]),
                               "median": median([x for (h, bb), v in buck_spread.items()
                                                 if bb == b and h < 14 for x in v])},
                "pin_14_20Z": {"n": len([x for (h, bb), v in buck_spread.items()
                                           if bb == b and 14 <= h <= 20 for x in v]),
                               "median": median([x for (h, bb), v in buck_spread.items()
                                                 if bb == b and 14 <= h <= 20 for x in v])},
            } for b in range(10)
        },
        # liquidity context: median 24h volume / open interest by hour (UTC)
        "liquidity_by_hour_utc": {
            h: {"n": len(liq_vol.get(h, [])),
                "median_volume_24h": median(liq_vol.get(h, [])),
                "median_open_interest": median(liq_oi.get(h, []))}
            for h in sorted(g_hourly)
        },
        "per_station": per_station,
    }

    # actionable gate
    pin_vs_best = []
    for s, d in per_station.items():
        if d["pin_median_spread"] and d["narrowest_hour_spread"]:
            pin_vs_best.append({
                "station": s, "pin_hour_utc": d["pin_hour_utc"],
                "pin_spread": d["pin_median_spread"],
                "best_hour_utc": d["narrowest_hour_utc"],
                "best_spread": d["narrowest_hour_spread"],
                "improvement_pct": round(100.0 * (d["pin_median_spread"] - d["narrowest_hour_spread"])
                                         / d["pin_median_spread"], 2) if d["pin_median_spread"] else None,
            })
    prod["pin_vs_narrowest"] = pin_vs_best
    prod["median_improvement_pct_if_move_to_best"] = median([x["improvement_pct"] for x in pin_vs_best
                                                             if x["improvement_pct"] is not None])

    OUT.write_text(json.dumps(prod, indent=2))
    print(json.dumps({k: v for k, v in prod.items() if k not in ("global_hourly_utc",
          "global_hourly_utc_near_money", "per_station")}, indent=2))
    print("\nglobal hourly (UTC) median spread:")
    for h, v in sorted(g_hourly.items()):
        print(f"  {h:02d}Z n={v['n']:6d} med={v['median_spread']} nm={g_hourly_nm[h]['median_spread']}")
    print("\nper-station pin vs best:")
    for x in sorted(pin_vs_best, key=lambda y: -(y["improvement_pct"] or 0)):
        print("  ", x)
    print("\nwrote", OUT)


if __name__ == "__main__":
    main()
