#!/usr/bin/env python3
"""
b2_real_price_sweep.py — B2 REAL-PRICE sweep (Sprint G G1; corrected in Sprint H).

The frozen sweep (scripts/big_sweep.py → scripts/sweep/kalshi_sweep_eval.py:140)
sets the entry price SYNTHETICALLY (`market_price = 0.5 + confidence*0.4`).
This harness replaces that with the ACTUAL Kalshi mid-price (as-of join into
data/kalshi_market_snapshots.db::market_snapshots), and reads the realized
outcome from strike_ladder.result (strict strike-crossing axis).

Window: 2026-08-01 .. 2026-10-06 (B2 scope after F3 formal gap acceptance;
the 144 days 2026-03-09..2026-07-30 are unreachable from the public Kalshi
candlestick API).

Sprint H corrections (each reviewed as a source of an unquotable number):
  H1 fee      — ONE canonical fee: ceil(0.07·P·(1-P)·100)/100 per contract
                imported from core.fees (was a bare 0.07·P·(1-P), no ceiling).
  H2 calib    — the model probability is passed through the repo's
                calibration_curves.json (per-station HIGH/up bins, then the
                pooled HIGH/up fallback) so the sweep reports CALIBRATED p,
                plus raw Brier and a reliability table. Calibration generally
                SHRINKS the edge (the model is overconfident).
  H3 entry    — entry time pinned to 13:30Z (a bare run previously defaulted to
                13:00Z and silently overwrote the canonical artifact). The
                intraday high-so-far is folded into P(high>=X) so the sweep
                mirrors the live bridge path (intraday-constrained), not the
                day-unstarted daily-max distribution.

Method (per station × settlement date):
  1. model: mu = GEFS ensemble mean (step=24), sigma = max(0.8, (max-min)/4)
             P(high >= T) = 1 - Phi((T-mu)/sigma)               [p_high_ge]
  2. ladder: threshold strikes (-T) for (station, HIGH, date) from strike_ladder,
             each carrying the realized result ('yes'/'no').
  3. price (as-of, no lookahead): mid_price for the market_ticker at the latest
             snapshot_time <= t_eval. Real price, not synthetic.
  4. intraday: P(high>=X) = 1.0 for X <= high_so_far, else a Gaussian with the
             remaining-day sigma (sigma·sqrt(remaining fraction)) — mirrors
             core/intraday/distribution_adapter.
  5. trade: pick the strike with max EV leg (yes or no) over net edge; one trade
             per station-day, side chosen by EV. Outcome = strike_ladder.result.

B-Mode compliant: no AI/ML in the loop. Read-only against source DBs.
"""
import argparse, csv, json, math, sqlite3, sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
REPO = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO))

from core.fees import fee_per_contract  # noqa: E402  H1: canonical fee primitive
from core.station_time import station_timezone_name  # noqa: E402  H3: local day

GEFS = REPO / "data" / "gefs_archive.db"
LADDER = REPO / "data" / "kalshi_strike_ladder.db"
SNAPS = REPO / "data" / "kalshi_market_snapshots.db"
METAR = REPO / "data" / "metar_backfill.db"          # H3
CALIB = REPO / "data" / "calibration_curves.json"    # H2
OUT = REPO / "data" / "b2_real_price_results.json"

DEFAULT_ENTRY_UTC = "13:30:00"  # H3: pinned (was 13:00:00)

# Ablation switches (module-level; set from CLI in main()). Defaults = corrected.
USE_LEGACY_FEE = False   # --legacy-fee : bare 0.07·P·(1-P) in EV (no ceiling)
FEE_PNL_ONLY = False     # --fee-pnl-only : legacy fee in EV, canonical in P&L
USE_CALIB = True         # --no-calibration : skip H2
USE_INTRADAY = True      # --no-intraday : skip H3 intraday constraint


def _norm_cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def p_high_ge(mu, sigma, x):
    if sigma <= 0:
        return 1.0 if mu >= x else 0.0
    return 1.0 - _norm_cdf((x - mu) / sigma)


def fee_frac(p):
    """Fee used by the EV/selection layer.

    Corrected (H1) default: canonical ceil(0.07·P·(1-P)·100)/100.
    --legacy-fee or --fee-pnl-only restore the pre-H1 bare 0.07·P·(1-P) here.
    """
    if USE_LEGACY_FEE or FEE_PNL_ONLY:
        return 0.07 * p * (1.0 - p)
    return fee_per_contract(p)


def fee_pnl(p):
    """Fee used in the REALIZED P&L line.

    Canonical (H1) by default and under --fee-pnl-only (which re-charges P&L with
    the canonical fee while leaving the legacy EV fee in place — the "fee-only"
    recompute that leaves the trade set unchanged).
    --legacy-fee restores the legacy fee here too (pure pre-H1 reproduction).
    """
    if USE_LEGACY_FEE:
        return 0.07 * p * (1.0 - p)
    return fee_per_contract(p)


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


def load_metar():
    """{station: [(ts_iso, temp_f)]} sorted — for the intraday high-so-far (H3)."""
    c = ro(METAR)
    out = defaultdict(list)
    q = """SELECT station, timestamp_utc, temp_f FROM metar_observations
           WHERE temp_f IS NOT NULL AND timestamp_utc >= ? AND timestamp_utc < ?
           ORDER BY station, timestamp_utc"""
    lo = f"{START}T00:00:00"; hi = f"{END}T23:59:59.999"
    # a bit wider so a station-local day boundary near the start still resolves
    lo = (datetime.fromisoformat(START) - timedelta(days=1)).isoformat()
    hi = (datetime.fromisoformat(END) + timedelta(days=2)).isoformat()
    for st, ts, tf in c.execute(q, (lo, hi)):
        out[st].append((ts, float(tf)))
    c.close()
    return out


def local_day_bounds(station, date_str):
    """(day_start_utc_iso, day_end_utc_iso) for the station's LOCAL weather day."""
    tz = ZoneInfo(station_timezone_name(station))
    d = datetime.fromisoformat(date_str).date()
    start = datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(timezone.utc)
    return start, start + timedelta(days=1)


def high_so_far(metar_rows, day_start, cutoff):
    """Max observed temp_f over [day_start, cutoff). None if no obs yet."""
    hi = None
    for ts, tf in metar_rows:
        try:
            t = datetime.fromisoformat(ts)
        except ValueError:
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        if day_start <= t < cutoff and (hi is None or tf > hi):
            hi = tf
    return hi


def _p_ge_intraday(mu, sigma, high_so_far_f, day_fraction_elapsed, x):
    """P(high>=X) constrained by the observed high-so-far (mirrors the live
    bridge path: P(high>=X)=1 for X<=high_so_far; remaining-day sigma shrinks)."""
    if high_so_far_f is not None and x <= high_so_far_f:
        return 1.0
    df = max(0.0, min(1.0, day_fraction_elapsed))
    remaining = max(0.05, 1.0 - df)
    sigma_t = max(0.05, max(0.8, sigma) * math.sqrt(remaining))
    mu_t = max(mu, high_so_far_f) if high_so_far_f is not None else mu
    return p_high_ge(mu_t, sigma_t, x)


# ── H2: calibration layer (read-only, repo convention) ──────────────────────
_CALIB_TABLE = None
_BIN_EDGES = [0.50 + i * 0.05 for i in range(11)]


def load_calibration():
    global _CALIB_TABLE
    if _CALIB_TABLE is None:
        try:
            _CALIB_TABLE = json.load(open(CALIB))
        except Exception as e:  # noqa: BLE001
            print(f"WARNING: calibration_curves.json unreadable ({e}); raw p only")
            _CALIB_TABLE = {}
    return _CALIB_TABLE


def _bin_label(p):
    for i in range(len(_BIN_EDGES) - 1):
        if _BIN_EDGES[i] <= p < _BIN_EDGES[i + 1]:
            return f"{_BIN_EDGES[i]:.2f}-{_BIN_EDGES[i+1]:.2f}"
    return None


def calibrate_p(station, raw_p):
    """Map a raw ensemble P(high>=strike) (>=0.5) to the empirical win rate.

    Direction is 'up' for every strike here (P(high>=X) >= 0.5 is the event the
    up-curve was fit on). Per-station HIGH/up bins first, then pooled _global
    HIGH/up, then raw. Isotonic/PAVA curves are not used — the frozen artifact
    is the empirical bin table.
    """
    table = load_calibration()
    if not table or raw_p < 0.5:
        return raw_p
    label = _bin_label(raw_p)
    if label is None:
        return raw_p
    for tbl in (table.get(station, {}).get("HIGH", {}).get("up", {}),
                table.get("_global", {}).get("HIGH", {}).get("up", {})):
        wr = (tbl.get("bins", {}).get(label, {}) or {}).get("win_rate")
        if wr is not None:
            return float(wr)
    return raw_p


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


def wilson_ci(k, n, z=1.96):
    if n <= 0:
        return (0.0, 0.0)
    p = k / n
    d = 1.0 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (round((c - m) / d, 4), round((c + m) / d, 4))


def reliability_table(trades, n_bins=10):
    """Calibration reliability: empirical P(high>=strike) per calibrated-p bin.

    The model probability `p` is P(high>=strike) = P(yes), so the calibration
    outcome axis is the YES event (result=='yes'), NOT whether the traded side
    won (a NO bet wins when the yes event does NOT happen).
    """
    edges = [i / n_bins for i in range(n_bins + 1)]
    rows = []
    for i in range(n_bins):
        lo, hi = edges[i], edges[i + 1]
        sel = [t for t in trades if lo <= t["p"] < hi or (i == n_bins - 1 and t["p"] == hi)]
        if not sel:
            rows.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": 0,
                         "mean_p": None, "empirical": None})
            continue
        rows.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": len(sel),
                     "mean_p": round(sum(t["p"] for t in sel) / len(sel), 4),
                     "empirical": round(sum(1 for t in sel if t["result"] == "yes") / len(sel), 4)})
    return rows


def main():
    global START, END
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-08-01")
    ap.add_argument("--end", default="2026-10-06")
    ap.add_argument("--min-edge", type=float, default=0.0)
    ap.add_argument("--entry-utc", default=DEFAULT_ENTRY_UTC,
                    help=f"event-day entry cutoff UTC (no lookahead); default {DEFAULT_ENTRY_UTC}")
    ap.add_argument("--stations", nargs="*", default=None)
    ap.add_argument("--out", default=str(OUT), help="output artifact path")
    ap.add_argument("--legacy-fee", action="store_true",
                    help="H1 ablation: bare 0.07*P*(1-P) in EV (no ceiling)")
    ap.add_argument("--fee-pnl-only", action="store_true",
                    help="H1 ablation: legacy fee in EV, canonical fee in P&L")
    ap.add_argument("--no-calibration", action="store_true",
                    help="H2 ablation: skip the calibration layer")
    ap.add_argument("--no-intraday", action="store_true",
                    help="H3 ablation: skip the intraday high-so-far constraint")
    a = ap.parse_args()
    START, END = a.start, a.end

    global USE_LEGACY_FEE, FEE_PNL_ONLY, USE_CALIB, USE_INTRADAY
    USE_LEGACY_FEE = a.legacy_fee
    FEE_PNL_ONLY = a.fee_pnl_only
    USE_CALIB = not a.no_calibration
    USE_INTRADAY = not a.no_intraday

    if a.entry_utc != DEFAULT_ENTRY_UTC:
        print(f"WARNING: --entry-utc={a.entry_utc} differs from the canonical "
              f"{DEFAULT_ENTRY_UTC}; this artifact is NOT the canonical B2 number.")
        print("WARNING: the canonical B2 artifact is written at 13:30Z. Use "
              "--out to a non-canonical path if you intend a sensitivity run.")

    gefs = load_gefs()
    ladder = load_ladder()
    prices = load_prices()
    metar = load_metar()

    trades = []
    skipped = defaultdict(int)
    intraday_applied = 0
    for (st, d), strikes in sorted(ladder.items()):
        if a.stations and st not in a.stations:
            continue
        if st not in gefs or d not in gefs[st]:
            skipped["no_gefs"] += 1
            continue
        mu, sigma, nm = gefs[st][d]
        # No lookahead: entry cutoff is the event-day. An end-of-day cutoff would
        # peek at the settled book.
        t_eval = f"{d}T{a.entry_utc}+00:00"
        day_start, day_end = local_day_bounds(st, d)
        cutoff_dt = datetime.fromisoformat(f"{d}T{a.entry_utc}+00:00")
        hsf = high_so_far(metar.get(st, []), day_start, cutoff_dt)
        df = max(0.0, min(1.0, (cutoff_dt - day_start).total_seconds()
                          / (day_end - day_start).total_seconds()))
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
            p_raw = p_high_ge(mu, sigma, sv)
            # H3: intraday-constrained probability (mirrors the live bridge) —
            # fold high_so_far + remaining-day sigma BEFORE calibrating, so the
            # constraint actually drives selection (not just a reported field).
            p_intra = _p_ge_intraday(mu, sigma, hsf, df, sv) if USE_INTRADAY else p_raw
            # H2: calibration layer -> the probability the EV layer trades on
            p = calibrate_p(st, p_intra) if USE_CALIB else p_intra
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
                            side=leg, p_raw=round(p_raw, 4), p=round(p, 4),
                            p_intraday=round(p_intra, 4), mid=round(mid, 4),
                            entry=round(cost, 4), ev=round(ev, 4), result=res,
                            won=bool(won), snapshot_time=ts, volume_24h=vol,
                            sigma=round(sigma, 3), n_members=nm,
                            high_so_far=round(hsf, 2) if hsf is not None else None,
                            day_frac=round(df, 4))
        if best is None:
            skipped["no_liquid_strike"] += 1
            continue
        if best["ev"] < a.min_edge:
            skipped["below_min_edge"] += 1
            continue
        if best["high_so_far"] is not None:
            intraday_applied += 1
        # P&L per contract: win => (1 - entry), lose => (-entry); minus round-trip fee
        payout = 1.0 if best["won"] else 0.0
        best["pnl_per_contract"] = round(payout - best["entry"] - fee_pnl(best["entry"]) - fee_pnl(1.0 if best["won"] else 0.0), 5)
        trades.append(best)

    # ---- metrics ----
    n = len(trades)
    wins = sum(1 for t in trades if t["won"])
    acc = (wins / n) if n else 0.0
    ci = wilson_ci(wins, n)
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
    brier_cal = sum((t["p"] - (1.0 if t["result"] == "yes" else 0.0)) ** 2 for t in trades) / n if n else None
    brier_raw = sum((t["p_raw"] - (1.0 if t["result"] == "yes" else 0.0)) ** 2 for t in trades) / n if n else None

    near = [t for t in trades if 0.15 <= t["mid"] <= 0.85]
    near_n = len(near)
    near_w = sum(1 for t in near if t["won"])
    near_acc = (near_w / near_n) if near_n else None

    by_station = defaultdict(lambda: [0, 0, 0.0])
    for t in trades:
        b = by_station[t["station"]]
        b[0] += 1
        b[1] += 1 if t["won"] else 0
        b[2] += t["pnl_per_contract"]

    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "sprint_h": {
            "h1_fee": "canonical ceil(0.07*P*(1-P)*100)/100 per contract (core.fees)",
            "h1_legacy_fee_ablation": USE_LEGACY_FEE,
            "h1_fee_pnl_only_ablation": FEE_PNL_ONLY,
            "h2_calibration": ("calibration_curves.json HIGH/up bins -> pooled _global -> raw"
                               if USE_CALIB else "disabled"),
            "h3_entry_pinned": DEFAULT_ENTRY_UTC,
            "h3_intraday_constraint": ("P(high>=X)=1 for X<=high_so_far; remaining-day sigma"
                                       if USE_INTRADAY else "disabled"),
            "entry_utc_used": a.entry_utc,
            "intraday_applied_n": intraday_applied,
        },
        "window": {"start": START, "end": END, "days": 67,
                   "caveat": "B2 results constrained to Aug-Oct window; 144 days Mar-Jul unreachable — summer regime may differ"},
        "price_source": "REAL mid_price (kalshi_market_snapshots.db, as-of join, no lookahead)",
        "outcome_source": "strike_ladder.result (strict strike-crossing)",
        "n_trades": n, "n_wins": wins,
        "accuracy": round(acc, 4),
        "accuracy_wilson_95ci": {"lo": ci[0], "hi": ci[1]},
        "near_money_0.15_0.85": {"n": near_n, "wins": near_w,
                                 "accuracy": round(near_acc, 4) if near_acc is not None else None},
        "mean_pnl_per_contract": round(mean_pnl, 5),
        "mean_model_minus_market_edge": round(mean_edge, 5),
        "mean_ev": round(mean_ev, 5),
        "sharpe_annualized": round(sharpe, 4),
        "brier_calibrated": round(brier_cal, 5) if brier_cal is not None else None,
        "brier_raw": round(brier_raw, 5) if brier_raw is not None else None,
        "reliability": reliability_table(trades),
        "skipped": dict(skipped),
        "by_station": {k: {"n": v[0], "wins": v[1],
                           "acc": round(v[1] / v[0], 4) if v[0] else None,
                           "sum_pnl_per_contract": round(v[2], 4)}
                       for k, v in sorted(by_station.items())},
        "trades": trades,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(report, indent=2))
    print(json.dumps({k: report[k] for k in
                      ["n_trades", "n_wins", "accuracy", "accuracy_wilson_95ci",
                       "near_money_0.15_0.85", "mean_pnl_per_contract",
                       "mean_model_minus_market_edge", "mean_ev", "sharpe_annualized",
                       "brier_calibrated", "brier_raw", "skipped"]}, indent=2))
    print("reliability:", json.dumps(report["reliability"]))
    print("stations:", len(by_station), "→ wrote", a.out)


if __name__ == "__main__":
    START = "2026-08-01"; END = "2026-10-06"
    main()
