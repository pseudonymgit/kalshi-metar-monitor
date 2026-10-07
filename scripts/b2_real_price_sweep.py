#!/usr/bin/env python3
"""
b2_real_price_sweep.py — B2 REAL-PRICE sweep (Sprint G, item G1).

The frozen sweep (scripts/big_sweep.py → scripts/sweep/kalshi_sweep_eval.py:140)
sets the entry price SYNTHETICALLY (`market_price = 0.5 + confidence*0.4`).
This harness replaces that with the ACTUAL Kalshi mid-price (as-of join into
data/kalshi_market_snapshots.db::market_snapshots), and reads the realized
outcome from strike_ladder.result (strict strike-crossing axis).

Window: 2026-08-01 .. 2026-10-06 (B2 scope after F3 formal gap acceptance;
the 144 days 2026-03-09..2026-07-30 are unreachable from the public Kalshi
candlestick API).

Method (per station × settlement date):
  1. model: mu = GEFS ensemble mean (step=24), sigma = max(0.8, (max-min)/4)
             P(high >= T) = 1 - Phi((T-mu)/sigma)               [p_high_ge]
  2. ladder: threshold strikes (-T) for (station, HIGH, date) from strike_ladder,
             each carrying the realized result ('yes'/'no').
  3. price (as-of, no lookahead): mid_price for the market_ticker at the latest
             snapshot_time <= t_eval (date 23:59Z). Real price, not synthetic.
  4. trade: pick the strike with max EV leg (yes or no) over net edge; one trade
             per station-day, side chosen by EV. Outcome = strike_ladder.result.

B-Mode compliant: no AI/ML in the loop. Read-only against source DBs.
"""
import argparse, csv, json, math, sqlite3, sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO))

GEFS = REPO / "data" / "gefs_archive.db"
LADDER = REPO / "data" / "kalshi_strike_ladder.db"
SNAPS = REPO / "data" / "kalshi_market_snapshots.db"
OUT = REPO / "data" / "b2_real_price_results.json"

FEE_FRAC = 0.07  # Kalshi taker: fee = ceil(0.07 * C * P * (1-P)); per-contract frac


def _norm_cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def p_high_ge(mu, sigma, x):
    if sigma <= 0:
        return 1.0 if mu >= x else 0.0
    return 1.0 - _norm_cdf((x - mu) / sigma)


def fee_frac(p):
    return FEE_FRAC * p * (1.0 - p)


def ro(p):
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def load_gefs():
    """{station: {date: (mu, sigma)}} from step=24 members."""
    c = ro(GEFS)
    out = defaultdict(dict)
    q = """SELECT station, target_date, ensemble_mean, ensemble_min, ensemble_max, n_members
           FROM gefs_archive WHERE step=24 AND target_date BETWEEN ? AND ?"""
    for st, d, mean, mn, mx, n in c.execute(q, (START, END)):
        if mean is None:
            continue
        # GEFS archive mean/min/max are in CELSIUS (repo convention: the sweep
        # converts with c*9/5+32 at kalshi_sweep_eval.py:263). Convert to °F,
        # because strikes and settlement buckets are in °F.
        mean_f = float(mean) * 9.0 / 5.0 + 32.0
        if mn is not None and mx is not None:
            spread_f = (float(mx) - float(mn)) * 9.0 / 5.0
        else:
            spread_f = 7.2  # ~4°C fallback
        sigma = max(0.8, spread_f / 4.0)
        out[st][d] = (mean_f, float(sigma), int(n or 0))
    c.close()
    return out


def load_ladder():
    """{(station,date): [(strike, result, ticker)]} for threshold T-strikes, HIGH."""
    c = ro(LADDER)
    out = defaultdict(list)
    q = """SELECT station, local_trading_date, strike_value, result, ticker
           FROM strike_ladder
           WHERE market_type='HIGH' AND strike_type='greater'
             AND local_trading_date BETWEEN ? AND ?"""
    for st, d, sv, res, tk in c.execute(q, (START, END)):
        if res not in ("yes", "no"):
            continue
        out[(st, d)].append((float(sv), res, tk))
    c.close()
    return out


def load_prices():
    """{ticker: [(snapshot_time, mid, ask, volume)]} — real quotes."""
    c = ro(SNAPS)
    out = defaultdict(list)
    q = """SELECT market_ticker, snapshot_time, mid_price, yes_ask, volume_24h
           FROM market_snapshots
           WHERE mid_price IS NOT NULL AND snapshot_time BETWEEN ? AND ?"""
    for tk, ts, mid, ask, vol in c.execute(q, (START + "T00:00:00", END + "T23:59:59.999")):
        out[tk].append((ts, float(mid), ask, vol))
    c.close()
    return out


def asof_mid(prices, ticker, t_eval_iso):
    """Latest quote at snapshot_time <= t_eval (no lookahead).
    Returns (snapshot_time, px, volume) or None.
    Mirrors sbox_intraday_ev_bridge.strike_ladder: prefer mid, else ask."""
    rows = prices.get(ticker)
    if not rows:
        return None
    best = None
    for ts, mid, ask, vol in rows:
        if ts <= t_eval_iso and (best is None or ts > best[0]):
            px = mid if mid is not None else ask
            best = (ts, px, vol)
    return best


MIN_VOLUME = 50.0  # mirrors sbox_intraday_ev_bridge.strike_ladder default


def main():
    global START, END
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-08-01")
    ap.add_argument("--end", default="2026-10-06")
    ap.add_argument("--min-edge", type=float, default=0.0)
    ap.add_argument("--entry-utc", default="13:00:00",
                    help="event-day entry cutoff (no lookahead); default 13:00Z = local morning")
    ap.add_argument("--stations", nargs="*", default=None)
    a = ap.parse_args()
    START, END = a.start, a.end

    gefs = load_gefs()
    ladder = load_ladder()
    prices = load_prices()

    trades = []
    skipped = defaultdict(int)
    for (st, d), strikes in sorted(ladder.items()):
        if a.stations and st not in a.stations:
            continue
        if st not in gefs or d not in gefs[st]:
            skipped["no_gefs"] += 1
            continue
        mu, sigma, nm = gefs[st][d]
        # No lookahead: entry cutoff is the event-day morning (before the local
        # afternoon max). An end-of-day cutoff would peek at the settled book.
        t_eval = f"{d}T{a.entry_utc}+00:00"
        best = None
        n_liquid = 0
        for sv, res, tk in strikes:
            got = asof_mid(prices, tk, t_eval)
            if not got:
                skipped["no_price"] += 1
                continue
            ts, mid, vol = got
            if mid is None:
                continue
            mid = float(mid)
            # Mirror sbox_intraday_ev_bridge.strike_ladder liquidity filter:
            # only genuinely two-sided quotes are investable (0.02 < px < 0.98)
            # and vol >= MIN_VOLUME. This excludes structural mid=0 placeholders.
            if mid <= 0.02 or mid >= 0.98:
                skipped["illiquid_extreme"] += 1
                continue
            if (vol or 0) < MIN_VOLUME:
                skipped["thin_volume"] += 1
                continue
            n_liquid += 1
            # mid is YES mid for a "greater/above" contract = P(high >= strike)
            p = p_high_ge(mu, sigma, sv)
            # YES leg: buy at mid, pays 1 if result=='yes'
            ev_yes = (p * 1.0 - mid) - fee_frac(mid)
            # NO leg: buy at (1-mid), pays 1 if result=='no'
            ev_no = ((1 - p) * 1.0 - (1 - mid)) - fee_frac(1 - mid)
            leg = "yes" if ev_yes >= ev_no else "no"
            ev = max(ev_yes, ev_no)
            cost = mid if leg == "yes" else (1 - mid)
            if cost <= 0 or cost >= 1:
                continue
            if best is None or ev > best["ev"]:
                won = (res == leg)
                best = dict(station=st, date=d, strike=sv, ticker=tk,
                            side=leg, p=round(p, 4), mid=round(mid, 4),
                            entry=round(cost, 4), ev=round(ev, 4), result=res,
                            won=bool(won), snapshot_time=ts, volume_24h=vol,
                            sigma=round(sigma, 3), n_members=nm)
        if best is None:
            skipped["no_liquid_strike"] += 1
            continue
        if best["ev"] < a.min_edge:
            skipped["below_min_edge"] += 1
            continue
        # P&L per contract: win => (1 - entry), lose => (-entry); minus round-trip fee
        payout = 1.0 if best["won"] else 0.0
        best["pnl_per_contract"] = round(payout - best["entry"] - fee_frac(best["entry"]) - fee_frac(1.0 if best["won"] else 0.0), 5)
        trades.append(best)

    # ---- metrics ----
    n = len(trades)
    wins = sum(1 for t in trades if t["won"])
    acc = (wins / n) if n else 0.0
    pnls = [t["pnl_per_contract"] for t in trades]
    mean_pnl = sum(pnls) / n if n else 0.0
    if n > 1:
        var = sum((x - mean_pnl) ** 2 for x in pnls) / (n - 1)
        sd = math.sqrt(var)
        sharpe = (mean_pnl / sd) * math.sqrt(252) if sd > 0 else 0.0  # annualized (daily-unit)
    else:
        sharpe = 0.0
    mean_ev = sum(t["ev"] for t in trades) / n if n else 0.0
    mean_edge = sum((t["p"] - t["mid"]) if t["side"] == "yes" else ((1 - t["p"]) - (1 - t["mid"])) for t in trades) / n if n else 0.0

    by_station = defaultdict(lambda: [0, 0, 0.0])
    for t in trades:
        b = by_station[t["station"]]
        b[0] += 1
        b[1] += 1 if t["won"] else 0
        b[2] += t["pnl_per_contract"]

    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "window": {"start": START, "end": END, "days": 67,
                   "caveat": "B2 results constrained to Aug-Oct window; 144 days Mar-Jul unreachable — summer regime may differ"},
        "price_source": "REAL mid_price (kalshi_market_snapshots.db, as-of join, no lookahead)",
        "outcome_source": "strike_ladder.result (strict strike-crossing)",
        "n_trades": n, "n_wins": wins, "accuracy": round(acc, 4),
        "mean_pnl_per_contract": round(mean_pnl, 5),
        "mean_model_minus_market_edge": round(mean_edge, 5),
        "mean_ev": round(mean_ev, 5),
        "sharpe_annualized": round(sharpe, 4),
        "skipped": dict(skipped),
        "by_station": {k: {"n": v[0], "wins": v[1],
                           "acc": round(v[1] / v[0], 4) if v[0] else None,
                           "sum_pnl_per_contract": round(v[2], 4)}
                       for k, v in sorted(by_station.items())},
        "trades": trades,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2))
    print(json.dumps({k: report[k] for k in
                      ["window", "n_trades", "n_wins", "accuracy",
                       "mean_pnl_per_contract", "mean_model_minus_market_edge",
                       "mean_ev", "sharpe_annualized", "skipped"]}, indent=2))
    print("stations:", len(by_station), "→ wrote", OUT)


if __name__ == "__main__":
    START = "2026-08-01"; END = "2026-10-06"
    main()
