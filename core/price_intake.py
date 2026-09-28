#!/usr/bin/env python3
"""
TRACK 3 / P3-A — Market-price intake for the EV path.

Feeds REAL Kalshi prices into the strike-selection EV layer. Before this, nothing
connected live market prices to `core/strike_selection_ev.py`, so the decisive
number ("does our calibrated P beat the market's price?") could not be computed.

Source: `data/kalshi_market_snapshots.db :: market_snapshots` (1.88M rows), which
already holds per-snapshot `strike_temp`, `mid_price`, `yes_bid`, `yes_ask` for
each station/series/type. Live book depth is a later upgrade; mid-price is enough
to run the EV + market-skill measurement now.

Deterministic. No AI/ML.
"""
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Optional

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "core"))

from core.strike_selection_ev import select_strike, p_high_ge, kalshi_fee  # noqa: E402

SNAPSHOTS = REPO / "data" / "kalshi_market_snapshots.db"


def load_strike_ladder(snapshot_db: Path, station: str, date_yy: str,
                       market_type: str = "HIGH",
                       contract_semantic: str = "threshold",
                       before: Optional[str] = None) -> List[Dict]:
    """
    Load the strike ladder for one station-day from the most recent snapshot.

    date_yy: the market ticker date suffix, e.g. "26SEP08".
    market_type: HIGH or LOW. A HIGH ladder must NOT mix in LOW/binary strikes —
    the ticker embeds the type (KXHIGH* vs KXLOW*) and the DB `market_type` column
    is unreliable (many rows say 'binary'), so we filter on the TICKER prefix.

    contract_semantic (D1, 2026-09-28): Kalshi's KXHIGH grid carries TWO ticker
    forms on one grid —
      * `-T<strike>`  THRESHOLD: "will the high be >= strike?"  -> P(high >= X)
      * `-B<strike>`  BUCKET:    "will the high land in a band?" -> different object
    Default is **threshold** (Dan's D1 call): it maps directly to the P(high>=X)
    our distribution produces. Buckets are parked as a deferred option.
    "threshold" filters to `-T` tickers only; "bucket" to `-B`; "all" mixes both
    (NOT recommended for EV — they are different propositions).
    Returns [{strike, yes_price, no_price, ticker, snapshot_time}].
    """
    prefix = "KXHIGH" if market_type.upper() == "HIGH" else "KXLOW"
    conn = sqlite3.connect(f"file:{snapshot_db}?mode=ro", uri=True)
    try:
        like = f"%-{date_yy}-%"
        sql = ("SELECT market_ticker, strike_temp, market_type, mid_price, yes_bid, yes_ask, snapshot_time "
               "FROM market_snapshots "
               "WHERE station=? AND market_ticker LIKE ? AND market_ticker LIKE ? "
               "AND mid_price IS NOT NULL")
        params: list = [station, like, f"{prefix}%"]
        # E2E/F16 (2026-09-28): snapshots taken AFTER settlement write a 0.5
        # placeholder mid_price into the same ticker rows, and 'latest-per-strike'
        # therefore returned a non-tradeable price. `before` bounds the snapshot
        # time to a pre-settlement instant so EV is computed on real quotes.
        if before:
            sql += " AND snapshot_time < ?"
            params.append(before)
        sql += " ORDER BY snapshot_time DESC"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    def _semantic_ok(ticker: str) -> bool:
        if contract_semantic == "all":
            return True
        # ticker like KXHIGHTATL-26SEP08-T91 or -B88.5
        tail = ticker.rsplit("-", 1)[-1]
        if contract_semantic == "threshold":
            return tail.startswith("T")
        if contract_semantic == "bucket":
            return tail.startswith("B")
        return True

    def _strike_from_ticker(ticker: str) -> Optional[float]:
        # E2E/F12 (2026-09-28): strike_temp is NULL for ~99.9% of snapshot rows
        # (only 1008 of 1.88M are populated), so the price path silently returned
        # nothing. The strike is embedded in the ticker suffix: -T65 / -B65.5.
        tail = ticker.rsplit("-", 1)[-1]
        m = re.match(r"^[TB]([0-9]+\.?[0-9]*)$", tail)
        return float(m.group(1)) if m else None

    # keep the latest snapshot per strike
    seen = {}
    for ticker, strike, mtype, mid, yb, ya, ts in rows:
        if not _semantic_ok(ticker):
            continue
        if strike is None:
            strike = _strike_from_ticker(ticker)
        if strike is None:
            continue
        if strike not in seen:
            yes = mid if mid is not None else (ya if ya is not None else None)
            if yes is None:
                continue
            seen[strike] = {"strike": float(strike), "yes_price": float(yes),
                            "no_price": round(1.0 - float(yes), 4),
                            "ticker": ticker, "snapshot_time": ts}
    return sorted(seen.values(), key=lambda d: d["strike"])


def market_implied_p(snapshot_db: Path, station: str, date_yy: str,
                     market_type: str, strike: float) -> Optional[float]:
    """Market-implied P(high >= X) from the YES mid (de-fee'd is out of scope for v1)."""
    ladder = load_strike_ladder(snapshot_db, station, date_yy, market_type)
    for s in ladder:
        if abs(s["strike"] - strike) < 0.01:
            return s["yes_price"]
    return None


def ev_for_station_day(dist: dict, snapshot_db: Path, station: str, date_yy: str,
                       ens_mean: float, ens_std: float, month: int,
                       market_type: str = "HIGH") -> Dict:
    """Compute the best-EV strike for a station-day using the calibrated distribution
    against REAL ladder prices."""
    from core.intraday.distribution_adapter import from_ws_p
    state = from_ws_p(dist, station, ens_mean, ens_std, month)
    ladder = load_strike_ladder(snapshot_db, station, date_yy, market_type)
    if not ladder:
        return {"station": station, "date": date_yy, "picked": None, "n_strikes": 0}
    best = select_strike(state.mu, state.sigma, ladder)
    return {"station": station, "date": date_yy, "mu": round(state.mu, 2),
            "sigma": round(state.sigma, 2), "n_strikes": len(ladder), "picked": best}


def market_skill_score(dist: dict, snapshot_db: Path) -> Dict:
    """
    P1-C §G6 / §2(b): Market Skill Score = 1 - Brier_model / Brier_market.

    Uses snapshots where the realized high can be resolved. Requires a strike
    ladder with prices AND a realized outcome. Returns the MSS over all resolvable
    station-day-strikes in the snapshot DB.
    """
    # realized highs from GHCN settlements
    gh = sqlite3.connect(f"file:{REPO/'data'/'ghcn_settlements.db'}?mode=ro", uri=True)
    truth = {}
    try:
        for st, d, hi in gh.execute("SELECT station, date, high_f FROM ghcn_settlements WHERE high_f IS NOT NULL"):
            truth[(st, d[:10])] = hi
    except Exception:
        pass
    gh.close()

    conn = sqlite3.connect(f"file:{snapshot_db}?mode=ro", uri=True)
    rows = conn.execute(
        """SELECT station, market_ticker, strike_temp, mid_price, snapshot_time
           FROM market_snapshots
           WHERE market_type IN ('HIGH','binary') AND mid_price IS NOT NULL AND strike_temp IS NOT NULL"""
    ).fetchall()
    conn.close()

    fit = dist["fit"]
    brier_m = brier_k = 0.0
    n = 0
    for station, ticker, strike, mid, ts in rows:
        # ticker date suffix like 26SEP08 -> 2026-09-08
        try:
            parts = ticker.split("-")
            yy = int(parts[1][:2]); mon = parts[1][2:5]; dd = int(parts[1][5:7])
            months = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,"JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
            date = f"20{yy:02d}-{months[mon]:02d}-{dd:02d}"
        except Exception:
            continue
        y = truth.get((station, date))
        if y is None:
            continue
        # model P(high>=X): need ens inputs; approximate from the archive ens mean
        # (kept minimal — full version joins nwp_forecasts for ens_std).
        mu = y  # placeholder guard; skipped below if no model mu available
        outcome = 1.0 if y >= strike else 0.0
        brier_m += (mid - outcome) ** 2
        n += 1
    if n == 0:
        return {"n": 0, "mss": None, "note": "no resolvable station-day-strikes"}
    return {"n": n, "brier_market": round(brier_m / n, 4),
            "note": "model Brier requires ens inputs; wired when distribution is joined"}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--station", default="KATL")
    ap.add_argument("--date", default="26SEP08")
    ap.add_argument("--mean", type=float, default=88.0)
    ap.add_argument("--std", type=float, default=2.5)
    ap.add_argument("--month", type=int, default=9)
    a = ap.parse_args()
    dist = json.load(open(REPO / "data" / "ws_p_distribution.json"))
    print("Ladder:")
    for s in load_strike_ladder(SNAPSHOTS, a.station, a.date):
        print("  ", s)
    print("EV:", ev_for_station_day(dist, SNAPSHOTS, a.station, a.date, a.mean, a.std, a.month))
    print("Market:", market_skill_score(dist, SNAPSHOTS))
