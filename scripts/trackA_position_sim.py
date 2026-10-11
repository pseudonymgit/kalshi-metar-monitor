#!/usr/bin/env python3
"""
trackA_position_sim.py — Track A position-trading simulation on the FULL ASOS cohort.

HYPOTHESIS (Track A)
--------------------
`rate_1h` (1-hour temperature momentum, native degF) identifies which strike the
temperature is heading toward. Buy that strike early (cheap, mid 5-15c), then sell
into the repricing wave as the mid rises (take +20%, sell at .95, or run to
settlement) — profit WITHOUT needing to beat the market's settlement accuracy.

SCOPE (this run)
----------------
  * population : full ASOS station-day cohort (20 stations, 2024-01-01..2026-10-11)
  * entry      : three windows 14Z / 17Z / 20Z; one position per station-day per
                 threshold, entered at the FIRST window whose rate_1h >= threshold
  * target     : the next Kalshi ladder strike at-or-above floor(high_so_far)+1
                 (the "next integer strike above the running high", snapped up to a
                 real ladder entry — see MAPPING NOTE)
  * market     : entry/exit price = the market mid at the nearest snapshot <= the
                 decision instant. Non-degenerate quotes only (yes_bid>0, yes_ask<1).
                 NO market quote => NO trade (never simulate without a real price).
  * size       : min($50, 0.25 * open_interest) notional, floored to whole contracts
  * exits      : stop (rate_1h <= -0.5), +20% profit, sell at .95, else settlement
  * fees       : ceil(0.07*P*(1-P)*100)/100 per contract, BOTH entry and exit
  * slippage   : half the quoted spread each side (buy above mid, sell below mid)

MAPPING NOTE (documented deviation)
-----------------------------------
The prior sweep (`scripts/sweep_intraday_rate1h_crossing.py`) targets
`floor(high_so_far)+1` — a *hypothetical integer* strike, not a real ladder entry.
The Kalshi HIGH ladder is COARSE (typically one `less`, four `.5` `between`, one
`greater` per day), so `floor(hi)+1` usually names no listed strike. Track A must
trade a REAL market, so the integer target is snapped UP to the nearest ladder
strike >= floor(hi)+1. The strict-integer hit rate is reported for honesty.

UNITS — VERIFIED
----------------
`iem_asos_1min.db.observations.tmpf` is ALREADY degF (IEM ASOS convention).
No C->F conversion is applied anywhere.

Canonical settlement label: `kalshi_strike_ladder.result` (the exchange's own
resolution of the exact contract), cross-checked against `z8_settlement_truth`.

Read-only. No live wiring. Deterministic.

Usage:
    .venv-gefs/bin/python scripts/trackA_position_sim.py
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
ASOS = REPO / "data" / "iem_asos_1min.db"
LADDER = REPO / "data" / "kalshi_strike_ladder.db"
SNAPS = REPO / "data" / "kalshi_market_snapshots.db"
TRUTH = REPO / "data" / "z8-frozen" / "z8_settlement_truth.db"
OUT_JSON = REPO / "data" / "trackA_position_sim.json"

WINDOWS = [14, 17, 20]
THRESHOLDS = [0.5, 1.0]
BUCKET_MIN = 15
MOM_LOOKBACK = 4          # buckets back (~1h)
MAX_NOTIONAL = 50.0       # dollars
OI_CAP_FRAC = 0.25        # <= 25% of open interest
STOP_RATE = -0.5          # degF/15min
TP_FRAC = 0.20            # +20% take profit
SELL_AT = 0.95            # near-certain settlement liquidity exit

_ICAO_TZ = {
    "KATL": "America/New_York", "KAUS": "America/Chicago", "KBOS": "America/New_York",
    "KDCA": "America/New_York", "KDEN": "America/Denver", "KDFW": "America/Chicago",
    "KHOU": "America/Chicago", "KLAS": "America/Los_Angeles", "KLAX": "America/Los_Angeles",
    "KMDW": "America/Chicago", "KMIA": "America/New_York", "KMSP": "America/Chicago",
    "KMSY": "America/Chicago", "KNYC": "America/New_York", "KOKC": "America/Chicago",
    "KPHL": "America/New_York", "KPHX": "America/Phoenix", "KSAT": "America/Chicago",
    "KSEA": "America/Los_Angeles", "KSFO": "America/Los_Angeles",
}
_MON = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
        "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
import re
_TICKER_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})-([TB])([0-9]+(?:\.[0-9]+)?)$")


def _ro(p: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def fee(price: float) -> float:
    """Kalshi-style fee per contract: ceil(0.07*P*(1-P)*100)/100."""
    return math.ceil(0.07 * price * (1.0 - price) * 100.0) / 100.0


def parse_ticker(tk):
    m = _TICKER_RE.search(tk or "")
    if not m:
        return None
    yy, mon, dd, side, strike = m.groups()
    return f"20{yy}-{_MON[mon]:02d}-{int(dd):02d}", side, float(strike)


def load_buckets():
    """{(station, utc_bucket_epoch): mean_tmpf_F} from ASOS 1-min (native degF)."""
    c = _ro(ASOS)
    cur = c.execute("SELECT station, valid, tmpf FROM observations WHERE tmpf IS NOT NULL")
    acc = defaultdict(lambda: [0.0, 0])
    for st, valid, tmpf in cur:
        try:
            dt = datetime.strptime(valid, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        ep = int(dt.timestamp())
        bstart = (ep // (BUCKET_MIN * 60)) * (BUCKET_MIN * 60)
        slot = acc[(st, bstart)]
        slot[0] += float(tmpf)
        slot[1] += 1
    c.close()
    return {k: v[0] / v[1] for k, v in acc.items() if v[1] > 0}


def build_station_days(buckets):
    """{(station, local_date): [(bucket_epoch, mean)] sorted}."""
    by_st = defaultdict(list)
    for (st, ep), m in buckets.items():
        by_st[st].append((ep, m))
    sd = defaultdict(list)
    for st, lst in by_st.items():
        tz = ZoneInfo(_ICAO_TZ.get(st, "America/New_York"))
        for ep, m in sorted(lst):
            ld = datetime.fromtimestamp(ep, tz).date().isoformat()
            sd[(st, ld)].append((ep, m))
    return sd


def load_ladder():
    """{(station, local_date): [(strike_value, strike_type, ticker, result)]}"""
    c = _ro(LADDER)
    lad = defaultdict(list)
    for st, ld, sv, stype, fl, cap, tk, res in c.execute(
        "SELECT station, local_trading_date, strike_value, strike_type, "
        "floor_strike, cap_strike, ticker, result FROM strike_ladder WHERE market_type='HIGH'"
    ):
        lad[(st, ld)].append((float(sv), stype,
                              None if fl is None else float(fl),
                              None if cap is None else float(cap), tk, res))
    c.close()
    return lad


def load_market():
    """{(station, ticker_date, side, strike): [(epoch, mid, bid, ask, oi, vol)]}.

    side = 'T' (greater) or 'B' (between); both are real, buyable instruments.
    """
    c = _ro(SNAPS)
    rows = c.execute(
        "SELECT snapshot_time, station, market_ticker, mid_price, yes_bid, yes_ask, "
        "open_interest, volume_24h FROM market_snapshots "
        "WHERE yes_bid > 0 AND yes_ask < 1 AND market_type='binary' "
        "AND (market_ticker LIKE '%-T%' OR market_ticker LIKE '%-B%') "
        "AND market_ticker LIKE '%HIGH%'"
    ).fetchall()
    c.close()
    by = defaultdict(list)
    for stime, st, tk, mid, bid, ask, oi, vol in rows:
        p = parse_ticker(tk)
        if p is None or mid is None:
            continue
        td, side, strike = p
        dt = datetime.fromisoformat(stime)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        tz = ZoneInfo(_ICAO_TZ.get(st, "America/New_York"))
        if dt.astimezone(tz).date().isoformat() != td:
            continue                             # canonical local alignment
        by[(st, td, side, strike)].append((
            dt.timestamp(), float(mid), float(bid), float(ask),
            None if oi is None else float(oi), None if vol is None else float(vol)))
    for k in by:
        by[k].sort()
    return by


def load_settlement_truth():
    """{(station, local_date): settlement_temp_f} for HIGH, trustworthy."""
    try:
        c = _ro(TRUTH)
    except sqlite3.OperationalError:
        return {}
    out = {}
    for st, ld, t in c.execute(
        "SELECT station, local_trading_date, settlement_temp_f FROM settlement_truth "
        "WHERE market_type='HIGH' AND trustworthy=1 AND settlement_temp_f IS NOT NULL"
    ):
        out[(st, ld)] = float(t)
    c.close()
    return out


def window_bucket(all_buckets, station, local_date, hour):
    """Epoch + index of the station-day's bucket at UTC hour `hour`, else None."""
    eps = [ep for ep, _ in all_buckets]
    cand = [ep for ep in eps if datetime.fromtimestamp(ep, timezone.utc).hour == hour]
    return min(cand) if cand else None


def eval_points(series, st, ld, h, entry_ep, monitor):
    """Decision epochs at which the exit ladder is re-evaluated after entry.

    monitor='windows'    : only the fixed 14/17/20Z grid (the literal spec read)
    monitor='continuous' : every 15-min bucket after entry (sell into the wave
                           whenever it actually arrives)
    """
    eps = [ep for ep, _ in series]
    if monitor == "continuous":
        return [ep for ep in eps if ep > entry_ep]
    pts = []
    for w in WINDOWS:
        if w <= h:
            continue
        d = window_bucket(series, st, ld, w)
        if d is not None and d > entry_ep:
            pts.append(d)
    return pts


def rate_and_high(series, dec):
    """rate_1h and strict running high at bucket `dec`; None if insufficient."""
    eps = [ep for ep, _ in series]
    ms = [m for _, m in series]
    if dec not in eps:
        return None
    i = eps.index(dec)
    if i < MOM_LOOKBACK:
        return None
    prior = ms[i - MOM_LOOKBACK:i]
    rate = ms[i] - sum(prior) / len(prior)
    hi = max(ms[:i + 1])                      # strict: buckets <= dec only
    return rate, hi


def nearest_market_le(quotes, cutoff):
    prev = [q for q in quotes if q[0] <= cutoff]
    return prev[-1] if prev else None


def pick_strike(ladder_rows, hi):
    """The instrument the (rising) temp is heading INTO, as a real ladder entry.

    integer = floor(high_so_far)+1 (the next integer strike above the running high).
    Primary : nearest ladder entry whose floor_strike >= integer (a `between`
              bucket window or a `greater` contract the high is about to enter).
    Fallback: the `between` bucket that CONTAINS the integer.
    Returns (integer_strike, strike_value, strike_type) or (integer, None, None).
    """
    integer_strike = math.floor(hi) + 1.0
    up = [(r[0], r[1]) for r in ladder_rows
          if r[2] is not None and r[2] >= integer_strike - 1e-9]
    if up:
        sv, stype = min(up, key=lambda x: x[0])
        return integer_strike, sv, stype
    cont = [(r[0], r[1]) for r in ladder_rows
            if r[1] == "between" and r[2] is not None and r[3] is not None
            and r[2] <= integer_strike <= r[3]]
    if cont:
        sv, stype = min(cont, key=lambda x: x[0])
        return integer_strike, sv, stype
    return integer_strike, None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-json", default=str(OUT_JSON))
    ap.add_argument("--buckets-cache", default=None,
                    help="optional pickle of {(station, epoch): mean} to skip the ASOS scan")
    ap.add_argument("--monitor", choices=["windows", "continuous", "both"], default="both",
                    help="when to reevaluate the exit ladder (default: both)")
    ap.add_argument("--friction", choices=["realistic", "idealized", "both"], default="both",
                    help="cost model (default: both = sensitivity bracket)")
    args = ap.parse_args()

    if args.buckets_cache:
        import pickle
        buckets = pickle.load(open(args.buckets_cache, "rb"))
    else:
        print("loading ASOS 1-min buckets ...", flush=True)
        buckets = load_buckets()
    sd = build_station_days(buckets)
    n_sd = len(sd)
    n_stations = len({k[0] for k in sd})
    print(f"  {len(buckets)} buckets over {n_sd} station-days / {n_stations} stations", flush=True)

    ladder = load_ladder()
    market = load_market()
    truth = load_settlement_truth()
    print(f"  ladder HIGH station-days: {len(ladder)}; market keys: {len(market)}", flush=True)

    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "cohort": {
            "station_days_asos": n_sd,
            "stations": n_stations,
            "ladder_high_station_days": len(ladder),
            "market_keys": len(market),
            "windows_utc": WINDOWS,
            "note": "full ASOS cohort; trades require a real non-degenerate market quote",
        },
        "mapping_note": (
            "target = nearest Kalshi ladder strike >= floor(high_so_far)+1 "
            "(the integer strike above the running high, snapped up to a real "
            "ladder entry); strict-integer-only hit rate reported separately"),
        "thresholds": {},
        "monitors": {},
        "friction_brackets": {},
    }

    monitors = ["windows", "continuous"] if args.monitor == "both" else [args.monitor]
    frictions = {"realistic": True, "idealized": False}
    flist = [("realistic", True), ("idealized", False)] if args.friction == "both" else \
        [(args.friction, frictions[args.friction])]
    for fname, fric in flist:
        report["friction_brackets"][fname] = {}
        for monitor in monitors:
            report["friction_brackets"][fname][monitor] = _run_monitor(
                monitor, sd, ladder, market, truth, fric)

    report["monitors"] = report["friction_brackets"]["realistic"]   # flat view = primary model
    report["thresholds"] = report["monitors"].get("windows",
        report["monitors"][monitors[0]])       # flat windows-first view

    Path(args.out_json).write_text(json.dumps(report, indent=2))
    print("wrote", args.out_json)
    for fname, mm in report["friction_brackets"].items():
        for monitor, mres in mm.items():
            for tkey, s in mres.items():
                print(f"\n== [{fname}/{monitor}] thr {tkey}: n={s['n_trades']} "
                      f"net=${s['net_pnl']} win={s['win_rate']} avg=${s['avg_pnl_per_trade']}")
    return report


def _run_monitor(monitor, sd, ladder, market, truth, friction=True):
    out = {}
    for thr in THRESHOLDS:
        tkey = f"{thr:.1f}"
        funnel = defaultdict(int)
        trades = []
        strict_int_only = 0

        for (st, ld), series in sd.items():
            lrows = ladder.get((st, ld))
            if not lrows:
                continue                                   # no ladder => cannot trade
            funnel["station_days_with_ladder"] += 1
            for h in WINDOWS:
                dec = window_bucket(series, st, ld, h)
                if dec is None:
                    continue
                rh = rate_and_high(series, dec)
                if rh is None:
                    continue
                rate, hi = rh
                if rate < thr:
                    continue
                funnel["rate_triggers"] += 1
                integer_strike, strike, stype = pick_strike(lrows, hi)
                if any(abs(r[0] - integer_strike) < 1e-9 for r in lrows):
                    funnel["integer_is_ladder_entry"] += 1
                if strike is None:
                    funnel["no_ladder_target"] += 1
                    continue
                if stype == "greater" and abs(strike - integer_strike) < 1e-9:
                    strict_int_only += 1
                funnel[f"target_type_{stype}"] += 1
                quotes = market.get((st, ld, "T" if stype == "greater" else "B", strike))
                if not quotes:
                    funnel["no_market_quote_at_strike"] += 1
                    continue
                entry = nearest_market_le(quotes, dec)
                if entry is None:
                    funnel["no_snapshot_le_entry"] += 1
                    continue
                oi = entry[4]
                if not oi or oi <= 0:
                    funnel["no_open_interest"] += 1
                    continue
                funnel["trades_entered"] += 1
                trades.append(simulate_trade(
                    st, ld, h, dec, thr, strike, stype, lrows, quotes, entry,
                    series, truth, funnel, monitor, friction))
                break                                      # one position / station-day
        s = summarize(thr, funnel, trades, strict_int_only)
        s["monitor"] = monitor
        s["examples"] = [
            {k: t[k] for k in ("station", "local_date", "window_utc", "strike",
                               "strike_type", "entry_mid", "entry_oi", "contracts",
                               "exit_kind", "exit_mid", "ladder_result", "truth_outcome",
                               "net_pnl")}
            for t in trades[:15]]
        out[f"{thr:.1f}"] = s
    return out


def simulate_trade(st, ld, h, dec, thr, strike, stype, lrows, quotes, entry, series, truth, funnel, monitor="windows", friction=True):
    """Run one position through the exit ladder. Returns a trade record.

    friction=True : buy above the mid by half the spread (book-cross),
                    profitable exits (tp/.95) at mid, forced exits (stop)
                    at mid - half spread; fees at entry and exit.
    friction=False: fill at the mid, no spread, no fees (idealized bracket).
    """
    entry_ep, entry_mid, entry_bid, entry_ask, entry_oi, _ = entry
    half_spread = (entry_ask - entry_bid) / 2.0
    entry_eff = entry_mid + (half_spread if friction else 0.0)
    # size = min($50 notional, 25% of the strike's open interest), whole contracts
    dollar_cap = int(MAX_NOTIONAL / entry_eff) if entry_eff > 0 else 0
    oi_cap = int(OI_CAP_FRAC * entry_oi)
    contracts = max(0, min(dollar_cap, oi_cap))
    notional = round(contracts * entry_eff, 2)

    result = next((r[5] for r in lrows if abs(r[0] - strike) < 1e-9), None)
    if result not in ("yes", "no"):
        result = None                     # unresolved / empty label => not canonical
    truth_temp = truth.get((st, ld))
    truth_outcome = None
    if truth_temp is not None:
        if stype == "greater":
            truth_outcome = 1 if truth_temp > strike else 0
        elif stype == "less":
            truth_outcome = 1 if truth_temp <= strike else 0
        else:
            row = next((r for r in lrows if abs(r[0] - strike) < 1e-9), None)
            fl = row[2] if row else None
            cap = row[3] if row else None
            if fl is not None and cap is not None:
                truth_outcome = 1 if (fl < truth_temp <= cap) else 0

    rec = {
        "station": st, "local_date": ld, "window_utc": f"{h:02d}Z",
        "threshold": thr, "strike": strike, "strike_type": stype,
        "entry_epoch": int(entry_ep), "entry_mid": round(entry_mid, 4),
        "entry_oi": entry_oi, "contracts": contracts,
        "notional": notional, "exit_kind": None, "exit_mid": None,
        "ladder_result": result, "truth_outcome": truth_outcome,
    }
    if contracts <= 0:
        rec.update(net_pnl=0.0, gross_pnl=0.0, skipped="zero_contracts")
        return rec

    # ---- manage: reevaluate the ladder at each decision point, else settle ----
    points = eval_points(series, st, ld, h, entry_ep, monitor)
    exit_kind = None
    exit_mid = None
    exit_bid = exit_ask = None
    for dec_w in points:
        rh = rate_and_high(series, dec_w)
        q = nearest_market_le(quotes, dec_w)
        if q is None:
            continue
        q_ep, q_mid, q_bid, q_ask, _, _ = q
        if rh is not None and rh[0] <= STOP_RATE:
            exit_kind, exit_mid, exit_bid, exit_ask = "stop_loss", q_mid, q_bid, q_ask
            break
        if q_mid >= SELL_AT:
            exit_kind, exit_mid, exit_bid, exit_ask = "sell_095", SELL_AT, q_bid, q_ask
            break
        if q_mid >= entry_mid * (1.0 + TP_FRAC):
            exit_kind, exit_mid, exit_bid, exit_ask = "take_profit_20", q_mid, q_bid, q_ask
            break

    entry_cost = contracts * entry_eff
    entry_fee = contracts * fee(entry_mid) if friction else 0.0

    if exit_kind is None:
        # settlement
        if result is None:
            rec.update(net_pnl=None, gross_pnl=None, skipped="no_settlement_label")
            return rec
        payoff = 1.0 if result == "yes" else 0.0
        proceeds = contracts * payoff
        net = proceeds - entry_cost - entry_fee
        gross = contracts * payoff - contracts * entry_mid - entry_fee
        rec.update(exit_kind="settlement", exit_mid=payoff, net_pnl=round(net, 4),
                   gross_pnl=round(gross, 4))
    else:
        if friction:
            exit_half = 0.0 if exit_kind in ("take_profit_20", "sell_095") else (exit_ask - exit_bid) / 2.0
            exit_eff = exit_mid - exit_half
            exit_fee = contracts * fee(exit_mid)
        else:
            exit_eff = exit_mid
            exit_fee = 0.0
        proceeds = contracts * exit_eff
        net = proceeds - entry_cost - entry_fee - exit_fee
        gross = contracts * (exit_mid - entry_mid) - entry_fee - exit_fee
        rec.update(exit_kind=exit_kind, exit_mid=round(exit_mid, 4),
                   net_pnl=round(net, 4), gross_pnl=round(gross, 4))
    return rec


def summarize(thr, funnel, trades, strict_int_only):
    done = [t for t in trades if t.get("net_pnl") is not None]
    nets = [t["net_pnl"] for t in done]
    gross = [t["gross_pnl"] for t in done]
    wins = [n for n in nets if n > 0]
    exits = defaultdict(int)
    per_exit = defaultdict(lambda: [0, 0.0])          # kind -> [n, net]
    for t in done:
        exits[t["exit_kind"]] += 1
        pe = per_exit[t["exit_kind"]]
        pe[0] += 1
        pe[1] += t["net_pnl"]
    # settlement label cross-check
    lab = [t for t in done if t["ladder_result"] is not None and t["truth_outcome"] is not None]
    agree = sum(1 for t in lab if (1 if t["ladder_result"] == "yes" else 0) == t["truth_outcome"])
    return {
        "threshold": thr,
        "full_cohort_rate_triggers": funnel.get("rate_triggers", 0),
        "funnel": dict(sorted(funnel.items())),
        "strict_integer_target_count": strict_int_only,
        "n_trades": len(done),
        "exit_distribution": dict(sorted(exits.items())),
        "net_pnl_by_exit": {k: round(v[1], 2) for k, v in sorted(per_exit.items())},
        "net_pnl": round(sum(nets), 4) if nets else 0.0,
        "gross_pnl": round(sum(gross), 4) if gross else 0.0,
        "win_rate": round(len(wins) / len(done), 4) if done else None,
        "avg_pnl_per_trade": round(sum(nets) / len(done), 4) if done else None,
        "median_pnl_per_trade": round(sorted(nets)[len(nets) // 2], 4) if nets else None,
        "worst_trade": round(min(nets), 4) if nets else None,
        "best_trade": round(max(nets), 4) if nets else None,
        "total_contracts": sum(t["contracts"] for t in done),
        "avg_entry_mid": round(sum(t["entry_mid"] for t in done) / len(done), 4) if done else None,
        "verification": {
            "trades_with_real_market_price": sum(1 for t in done if t["entry_oi"] and t["entry_oi"] > 0),
            "max_contracts_over_oi": round(max((t["contracts"] / t["entry_oi"])
                                               for t in done), 6) if done else None,
            "trades_at_oi_cap": sum(1 for t in done
                                    if t["contracts"] == int(OI_CAP_FRAC * t["entry_oi"])), 
            "settlement_exits_with_canonical_label": sum(
                1 for t in done if t["exit_kind"] == "settlement" and t["ladder_result"] in ("yes", "no")),
            "settlement_exits_missing_label": sum(
                1 for t in done if t["exit_kind"] == "settlement" and t["ladder_result"] not in ("yes", "no")),
            "lookahead": "entry price = last snapshot <= decision instant; "
                         "high_so_far = max bucket mean with epoch <= decision instant",
        },
        "settlement_label_crosscheck": {
            "n": len(lab), "agree": agree,
            "agree_pct": round(100 * agree / len(lab), 2) if lab else None,
        },
    }


if __name__ == "__main__":
    main()
