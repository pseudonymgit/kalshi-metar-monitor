#!/usr/bin/env python3
"""
SBOX Intraday EV Bridge (2026-09-30)
====================================
Wires Dan's Item-1 chain into the SBOX paper ledger, end to end:

    WS-P calibrated distribution
        -> intraday truncation (high-so-far from METAR observations TODAY)
        -> strike selection (EV vs REAL Kalshi ladder prices, net of fee)
        -> EntryTimingEngine entry decision
        -> paper trade recorded in data/paper_trading_sbox.db

Deterministic, no AI/ML. SBOX only — refuses to touch DEV/PROD.

Correctness notes (all three were bugs in the first draft, fixed 2026-09-30):
  1. The weather DAY is the station-LOCAL calendar day, not the UTC date.
     GEFS target_date, METAR obs, the Kalshi ticker suffix and settlement all key
     off the local day. Using the UTC date misaligns every one of them.
  2. HIGH-SO-FAR must come from intraday METAR observations, not the
     `daily_stats` rollup (which is the FINAL day max, i.e. a locked win).
  3. The GEFS ensemble comes from `nwp_forecasts.db` (CLEAN), NOT
     `gefs_archive.db` (documented corrupt for distribution use), and is Celsius
     -> F.

CLI:
  python3 scripts/sbox_intraday_ev_bridge.py              # record (respects window)
  python3 scripts/sbox_intraday_ev_bridge.py --dry-run    # evaluate only
  python3 scripts/sbox_intraday_ev_bridge.py --report      # distribution/edge dump
  python3 scripts/sbox_intraday_ev_bridge.py --ignore-window --min-edge 0
"""
import argparse
import json
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "core"))

from core.intraday.distribution_adapter import from_ws_p, update_from_intraday, select  # noqa: E402
from core.intraday.entry_engine import EntryTimingEngine  # noqa: E402
from core.station_time import (  # noqa: E402
    is_within_entry_window, station_timezone_name,
)

DATA = REPO / "data"
SBOX_DB = DATA / "paper_trading_sbox.db"
WS_P = DATA / "ws_p_distribution.json"
NWP = DATA / "nwp_forecasts.db"   # CLEAN GEFS source
METAR = DATA / "metar_backfill.db"
SNAPS = DATA / "kalshi_market_snapshots.db"
INSTANCE = "SBOX"

STATIONS = ["KATL", "KAUS", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU", "KLAS",
            "KLAX", "KMDW", "KMIA", "KMSP", "KMSY", "KNYC", "KOKC", "KPHL",
            "KPHX", "KSAT", "KSEA", "KSFO"]

MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
          "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


def _ro(p):
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def _c2f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def local_day(station: str, now_utc: datetime):
    """(date_str, day_start_utc, day_end_utc) for the station's LOCAL weather day."""
    tz = ZoneInfo(station_timezone_name(station))
    local = now_utc.astimezone(tz)
    d = local.date()
    start = datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    return start.astimezone(tz).strftime("%Y-%m-%d"), start, end


def _dt_yy(d: str) -> str:
    y, m, dd = d.split("-")
    inv = {v: k for k, v in MONTHS.items()}
    return f"{y[2:]}{inv[int(m)]}{dd}"


def gefs_ens(station: str, target_date: str):
    """(ens_mean_f, ens_std_f) for the daily-max GEFS ensemble, or None.

    nwp_forecasts.db is the clean source; values are Celsius -> F.
    """
    c = _ro(NWP)
    try:
        rows = c.execute(
            "SELECT variable, value, fetch_date FROM nwp_forecasts "
            "WHERE model='gefs_ens' AND station=? AND target_date=? "
            "AND variable LIKE 'temperature_2m_max_member%'",
            (station, target_date)).fetchall()
    finally:
        c.close()
    latest = {}
    for var, val, fd in rows:
        if val is None:
            continue
        if var not in latest or fd > latest[var][0]:
            latest[var] = (fd, val)
    vals = [_c2f(v) for _, v in latest.values()]
    if len(vals) < 3:
        return None
    m = sum(vals) / len(vals)
    std = (sum((v - m) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5
    return (m, std)


def high_so_far(station: str, day_start: datetime, day_end: datetime,
                now_utc: datetime) -> Optional[float]:
    """Max observed temp over [day_start, min(now, day_end)). None if no obs yet."""
    hi_iso = min(now_utc, day_end).isoformat()
    c = _ro(METAR)
    try:
        row = c.execute(
            "SELECT MAX(temp_f) FROM metar_observations WHERE station=? "
            "AND timestamp_utc >= ? AND timestamp_utc < ?",
            (station, day_start.isoformat(), hi_iso)).fetchone()
    finally:
        c.close()
    return float(row[0]) if row and row[0] is not None else None


def strike_ladder(station: str, date_yy: str, before_iso: Optional[str] = None,
                  min_volume: float = 50.0) -> List[Dict]:
    """Threshold (-T) strikes with LIQUID mid prices for one station-day.

    Filters to KXHIGH*-T tickers (the P(high>=X) proposition). Bounds snapshot
    time to `before_iso` so a post-settlement 0.5 placeholder can't be used as a
    live quote (E2E/F16). Also drops rail/illiquid quotes: the `-T` grid on these
    stations is often barely quoted (mid ~0.005, vol ~0), and trading those would
    produce fantasy fills and meaningless P&L.
    """
    c = _ro(SNAPS)
    try:
        sql = ("SELECT market_ticker, mid_price, yes_ask, volume_24h FROM market_snapshots "
               "WHERE station=? AND market_ticker LIKE ? AND market_ticker LIKE 'KXHIGH%' "
               "AND mid_price IS NOT NULL")
        params = [station, f"%-{date_yy}-%"]
        if before_iso:
            sql += " AND snapshot_time < ?"
            params.append(before_iso)
        sql += " ORDER BY snapshot_time DESC"
        rows = c.execute(sql, params).fetchall()
    finally:
        c.close()
    seen = {}
    for tk, mid, ask, vol in rows:
        tail = tk.rsplit("-", 1)[-1]
        if not tail.startswith("T"):
            continue
        try:
            strike = float(tail[1:])
        except ValueError:
            continue
        if strike in seen:
            continue
        px = mid if mid is not None else ask
        if px is None:
            continue
        px = float(px)
        if px <= 0.02 or px >= 0.98:
            continue  # rail / placeholder quote
        if (vol or 0) < min_volume:
            continue  # illiquid
        seen[strike] = {"strike": strike, "yes_price": px,
                        "no_price": round(1 - px, 4), "ticker": tk,
                        "volume_24h": vol}
    return sorted(seen.values(), key=lambda d: d["strike"])


def record_trade(conn, station, market_type, side, our_prob, market_price,
                 confidence, strike, ticker, size_usd, functionality, day_str):
    """Insert one SBOX trade row. Idempotent per (station, day, functionality)."""
    dupe = conn.execute(
        "SELECT 1 FROM trades WHERE station=? AND trade_date_utc=? AND functionality=?",
        (station, day_str, functionality)).fetchone()
    if dupe:
        return {"status": "skipped", "reason": "already_recorded_today", "station": station}
    now = datetime.now(timezone.utc).isoformat()
    price = float(market_price)
    qty = max(1, int(round(size_usd / max(price, 0.01))))
    cost = abs(qty * price)
    direction = "UP" if side == "BUY" else "DOWN"
    conn.execute(
        """INSERT INTO trades (trade_uuid, trade_date_utc, station, market_type,
           signal_direction, forecast_prob, market_price, trade_type, quantity,
           position_size_usd, trade_price, trade_cost, trade_version, functionality,
           status, notes, implied_prob, analytical_prob, calibration_error,
           confidence_indicator, created_at_utc)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (str(uuid.uuid4()), day_str, station, market_type, direction, our_prob, price,
         "buy_yes" if side == "BUY" else "sell_yes", qty, size_usd, price, cost,
         f"v3.0_{INSTANCE.lower()}", functionality, "open",
         f"EV-strike {ticker} @ {strike}F; inst={INSTANCE}",
         price, our_prob, abs(our_prob - price), confidence, now))
    conn.commit()
    return {"status": "executed", "station": station, "signal_direction": direction,
            "strike": strike, "ticker": ticker, "our_prob": round(our_prob, 4),
            "market_price": price, "position_size_usd": round(size_usd, 2), "edge": round(our_prob - price, 4)}


def run(dry_run=False, ignore_window=False, stations=None, min_edge=0.03, report=False):
    stations = stations or STATIONS
    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()

    try:
        dist = json.load(open(WS_P))
    except Exception as e:
        return {"error": f"ws_p_distribution.json unreadable: {e}"}

    engine = EntryTimingEngine()
    conn = None if dry_run else sqlite3.connect(SBOX_DB)
    if conn:
        conn.execute("PRAGMA journal_mode=WAL;")

    results, skipped, diag = [], [], []
    for st in stations:
        day_str, day_start, day_end = local_day(st, now_utc)
        date_yy = _dt_yy(day_str)
        month = int(day_str[5:7])

        if not ignore_window:
            ok, reason = is_within_entry_window(st, day_str, now_utc=now_utc)
            if not ok:
                skipped.append((st, reason))
                continue

        ens = gefs_ens(st, day_str)
        if not ens:
            skipped.append((st, "no_gefs_step24"))
            continue
        hsf = high_so_far(st, day_start, day_end, now_utc)
        if hsf is None:
            skipped.append((st, "no_intraday_obs_yet"))
            continue

        frac = max(0.0, min(1.0, (now_utc - day_start).total_seconds()
                             / (day_end - day_start).total_seconds()))
        state = from_ws_p(dist, st, ens_mean=ens[0], ens_std=ens[1], month=month, date=day_str)
        state = update_from_intraday(state, hsf, frac)

        ladder = strike_ladder(st, date_yy, before_iso=now_iso)
        if not ladder:
            skipped.append((st, "no_strike_ladder"))
            continue

        picked = select(state, ladder, min_edge=min_edge)
        if not picked or picked.get("side") is None or picked.get("ev", 0) <= 0:
            skipped.append((st, "no_positive_ev_strike"))
            continue

        strike = picked["strike"]
        row = next((s for s in ladder if abs(s["strike"] - strike) < 1e-9), None)
        if row is None:
            skipped.append((st, "no_real_price_for_picked_strike"))
            continue
        our_prob = state.p_ge(strike)
        if picked["side"] == "no":
            our_prob = 1.0 - our_prob
            mkt = row["no_price"]
        else:
            mkt = row["yes_price"]
        ticker = row["ticker"]

        diag.append({"station": st, "day": day_str, "ens": [round(ens[0], 2), round(ens[1], 2)],
                     "hsf": hsf, "frac": round(frac, 3), "mu": round(state.mu, 2),
                     "sigma": round(state.sigma, 2), "strike": strike, "side": picked["side"],
                     "our_prob": round(our_prob, 4), "mkt": mkt, "ev": picked["ev"],
                     "n_strikes": len(ladder)})
        if report:
            continue

        sig = engine.evaluate_hour(
            hour=now_utc.hour,
            hour_before_settlement=6,
            our_probability=our_prob, market_price=mkt,
            confidence=abs(our_prob - 0.5) * 2.0,
        )
        if sig is None or sig.signal_type.name == "PASS":
            skipped.append((st, "entry_engine_pass"))
            continue

        if dry_run:
            results.append({"status": "would_execute", "station": st,
                            "side": sig.signal_type.name, "strike": strike,
                            "our_prob": round(our_prob, 4), "market_price": mkt,
                            "size": round(float(sig.position_size), 2)})
        else:
            results.append(record_trade(
                conn, st, "HIGH", sig.signal_type.name, our_prob, mkt,
                abs(our_prob - 0.5) * 2.0, strike, ticker,
                sig.position_size, "intraday_ev_strike", day_str))

    if conn:
        conn.close()
    out = {"date_utc": now_utc.strftime("%Y-%m-%d"), "n_executed": len(results),
           "n_skipped": len(skipped), "executed": results, "skipped": skipped[:25]}
    if report:
        out["diagnostics"] = diag
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--ignore-window", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--stations", nargs="*", default=None)
    ap.add_argument("--min-edge", type=float, default=0.03)
    a = ap.parse_args()
    out = run(dry_run=a.dry_run, ignore_window=a.ignore_window,
              stations=a.stations, min_edge=a.min_edge, report=a.report)
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
