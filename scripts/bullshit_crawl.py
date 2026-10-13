#!/usr/bin/env python3
"""
bullshit_crawl.py — sanity-crawl the live paper-trading book.

WHY: surface numbers in the live SBOX book that fail basic sanity checks, so
that no fabricated/implausible figure survives without a flag.

READ-ONLY. Does not modify any DB. Reads:
  * data/paper_trading_sbox.db          (the live book)
  * data/kalshi_market_snapshots.db     (open interest for the strike)
  * data/iem_asos_1min.db               (rate_1h momentum at the entry window)
  * data/station_metadata.json          (station -> longitude, for region)

Checks (a row may trip several):
  1. realized_pnl > bankroll_x10          -> "bankroll/position mismatch"
  2. quantity > 25% of open_interest      -> "over-market position"
  3. win rate quoted without a CI width   -> "no uncertainty"
  4. p_fused >= 0.9 but rate_1h <= 0      -> "grade/divergence mismatch"
  5. implied return > 100% of price       -> "extreme edge"
  6. position_size_usd > 25               -> "over max position"
  7. position_size_usd but p_fused < 0.6  -> "low conf but entered"

Output: data/bullshit_crawl_results.json
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
BOOK = REPO / "data" / "paper_trading_sbox.db"
SNAP = REPO / "data" / "kalshi_market_snapshots.db"
ASOS = REPO / "data" / "iem_asos_1min.db"
STATION_META = REPO / "data" / "station_metadata.json"
OUT = REPO / "data" / "bullshit_crawl_results.json"

GRID_HOURS = [6, 10, 12, 14, 16, 17, 18, 20, 22]
MOM_LOOKBACK = 4
BANKROLL_X10_RULE = 10.0
OI_FRACTION = 0.25
MAX_POSITION_USD = 25.0
LOW_CONF = 0.6
GRADE_S_P = 0.9

_TZ = {
    "KATL": "America/New_York", "KAUS": "America/Chicago", "KBOS": "America/New_York",
    "KDCA": "America/New_York", "KDEN": "America/Denver", "KDFW": "America/Chicago",
    "KHOU": "America/Chicago", "KLAS": "America/Los_Angeles", "KLAX": "America/Los_Angeles",
    "KMDW": "America/Chicago", "KMIA": "America/New_York", "KMSP": "America/Chicago",
    "KMSY": "America/Chicago", "KNYC": "America/New_York", "KOKC": "America/Chicago",
    "KPHL": "America/New_York", "KPHX": "America/Phoenix", "KSAT": "America/Chicago",
    "KSEA": "America/Los_Angeles", "KSFO": "America/Los_Angeles",
}


def _ro(p: Path) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def nearest_grid_hour(h: int) -> int:
    cands = [g for g in GRID_HOURS if g <= h]
    return cands[-1] if cands else GRID_HOURS[0]


def load_rate_1h_index():
    """{(station, local_date, window_hour): rate_1h} from the ASOS 1-min archive.

    rate_1h = mean(bucket at window) - mean(4 prior 15-min bucket means).
    Mirrors scripts/sweep_intraday_rate1h_crossing.py exactly.
    """
    con = sqlite3.connect(f"file:{ASOS}?mode=ro", uri=True)
    cur = con.execute(
        "SELECT station, substr(valid,1,10), "
        "(cast(substr(valid,12,2) as integer)*60 + (cast(substr(valid,15,2) as integer)/15)*15), "
        "avg(tmpf) FROM observations WHERE tmpf IS NOT NULL GROUP BY station, 2, 3"
    )
    bm = {}
    for st, d, mod, m in cur:
        try:
            base = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        dt = base + timedelta(minutes=mod)
        loc = dt.astimezone(ZoneInfo(_TZ.get(st, "America/New_York")))
        bm[(st, loc.date().isoformat(), mod)] = m
    con.close()

    by_sd = defaultdict(dict)
    for (st, ld, mod), m in bm.items():
        by_sd[(st, ld)][mod] = m

    idx = {}
    for (st, ld), mods in by_sd.items():
        ordered = sorted(mods.items())
        minutes = [m for m, _ in ordered]
        means = [v for _, v in ordered]
        for h in GRID_HOURS:
            dec = h * 60
            if dec not in mods:
                continue
            i = minutes.index(dec)
            if i < MOM_LOOKBACK:
                continue
            prior = means[i - MOM_LOOKBACK:i]
            idx[(st, ld, h)] = means[i] - sum(prior) / len(prior)
    return idx


def load_book():
    con = _ro(BOOK)
    cols = [d[1] for d in con.execute("PRAGMA table_info(trades)")]
    rows = [dict(r) for r in con.execute("SELECT * FROM trades")]
    con.close()
    return cols, rows


def bankroll():
    con = _ro(BOOK)
    r = con.execute(
        "SELECT closing_balance FROM daily_balances ORDER BY date_utc DESC LIMIT 1"
    ).fetchone()
    con.close()
    return float(r[0]) if r else 0.0


def snapshot_for_ticker(con, ticker, entry_iso):
    if not ticker or not entry_iso:
        return None
    r = con.execute(
        "SELECT snapshot_time, yes_bid, yes_ask, mid_price, open_interest "
        "FROM market_snapshots WHERE market_ticker=? AND snapshot_time<=? "
        "ORDER BY snapshot_time DESC LIMIT 1",
        (ticker, entry_iso),
    ).fetchone()
    return dict(r) if r else None


_WINRATE_RE = re.compile(r"win\s*rate|winrate|hit\s*rate|%\s*win|accuracy", re.I)
_CI_RE = re.compile(r"ci\b|95%|±|\+/-|confidence interval|\[.*,.*\]", re.I)


def winrate_without_ci(rows):
    """Flag rows whose TEXT fields quote a win rate with no CI width stated."""
    text_cols = ("notes", "reasons", "evidence", "detail", "bucket_reliability_stats")
    flags = []
    for r in rows:
        for c in text_cols:
            v = r.get(c)
            if not v:
                continue
            s = str(v)
            if _WINRATE_RE.search(s) and not _CI_RE.search(s):
                flags.append((r.get("trade_uuid") or r.get("id"), c, s[:160]))
    return flags


def main():
    bank = bankroll()
    idx = load_rate_1h_index()
    _, rows = load_book()
    snap = _ro(SNAP)

    flagged = []
    passed = 0

    # Rule 3 is book-wide (quote-text), evaluated once.
    wr_flags = winrate_without_ci(rows)

    for r in rows:
        reasons = []
        vals = {}
        pnl = r.get("realized_pnl")
        qty = r.get("quantity") or 0
        pos = r.get("position_size_usd") or 0
        pf = r.get("p_fused")
        an = r.get("analytical_prob")
        price = r.get("trade_price")

        # 1. P&L vs bankroll
        if pnl is not None and bank and pnl > bank * BANKROLL_X10_RULE:
            reasons.append("bankroll/position mismatch")
            vals["realized_pnl"] = pnl
            vals["bankroll_x10"] = round(bank * BANKROLL_X10_RULE, 2)

        # 2. position vs open interest
        snaprow = snapshot_for_ticker(snap, r.get("market_ticker"), r.get("created_at_utc"))
        if snaprow and snaprow.get("open_interest"):
            oi = float(snaprow["open_interest"])
            if oi > 0 and qty > OI_FRACTION * oi:
                reasons.append("over-market position")
                vals["quantity_pct_of_oi"] = round(100 * qty / oi, 2)
                vals["open_interest"] = oi

        # 4. grade S on flat/negative rate_1h
        if pf is not None and pf >= GRADE_S_P:
            ld = None
            if r.get("created_at_utc"):
                ts = datetime.fromisoformat(r["created_at_utc"])
                ld = ts.astimezone(ZoneInfo(_TZ.get(r["station"], "America/New_York"))).date().isoformat()
                win = nearest_grid_hour(ts.hour)
                rr = idx.get((r["station"], ld, win))
                if rr is not None and rr <= 0:
                    reasons.append("grade/divergence mismatch")
                    vals["p_fused"] = pf
                    vals["rate_1h"] = round(rr, 3)
                    vals["rate1h_window"] = f"{win:02d}Z"

        # 5. implied return > 100% of price
        if an is not None and price not in (None, 0):
            ir = (an - price) / price
            if ir > 1.0:
                reasons.append("extreme edge")
                vals["implied_return_pct"] = round(100 * ir, 1)
                vals["trade_price"] = price

        # 6. over max position
        if pos > MAX_POSITION_USD:
            reasons.append("over max position")
            vals["position_size_usd"] = pos

        # 7. low conf but entered
        if pos and pf is not None and pf < LOW_CONF:
            reasons.append("low conf but entered")
            vals["p_fused"] = pf
            vals["position_size_usd"] = pos

        if reasons:
            flagged.append({
                "trade_uuid": r.get("trade_uuid"),
                "station": r.get("station"),
                "trade_date_utc": r.get("trade_date_utc"),
                "rules": reasons,
                "values": vals,
            })
        else:
            passed += 1

    snap.close()

    result = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "bankroll_usd": round(bank, 2),
        "book_rows": len(rows),
        "rows_flagged": len(flagged),
        "rows_passed": passed,
        "pass_pct": round(100 * passed / len(rows), 1) if rows else 0.0,
        "rule3_winrate_without_ci": [
            {"ref": x[0], "field": x[1], "text": x[2]} for x in wr_flags
        ],
        "flagged_trades": flagged,
        "rule_definitions": {
            "bankroll/position mismatch": f"realized_pnl > bankroll x {BANKROLL_X10_RULE}",
            "over-market position": f"quantity > {OI_FRACTION:.0%} of strike open_interest",
            "no uncertainty": "win-rate claim in text with no CI width stated",
            "grade/divergence mismatch": f"p_fused >= {GRADE_S_P} on a station-day whose rate_1h at the entry window is <= 0",
            "extreme edge": "implied return (analytical-1)/price > 100% of price",
            "over max position": f"position_size_usd > {MAX_POSITION_USD}",
            "low conf but entered": f"position_size_usd set but p_fused < {LOW_CONF}",
        },
    }
    OUT.write_text(json.dumps(result, indent=2))

    # ---- console report ---------------------------------------------------
    print(f"bankroll ${bank:.2f} | {len(rows)} book rows | "
          f"{len(flagged)} flagged, {passed} passed ({result['pass_pct']}% pass)")
    print(f"{'trade_uuid':38} {'station':6} {'date':10} {'rule':32} {'values'}")
    for f in flagged:
        for rule in f["rules"]:
            print(f"{f['trade_uuid'][:36]:38} {f['station']:6} {f['trade_date_utc']:10} "
                  f"{rule:32} {json.dumps(f['values'])}")
    if wr_flags:
        print("rule3 win-rate-without-CI hits:", wr_flags)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
