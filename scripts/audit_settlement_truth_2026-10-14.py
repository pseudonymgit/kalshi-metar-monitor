#!/usr/bin/env python3
"""Ground-truth settlement audit for data/paper_trading_sbox.db (2026-10-14).

Resolves each book row's Kalshi market and compares the book's stored
outcome/P&L against Kalshi's OWN `result` field + `strike_type`/floor/cap.

Why: the book's settlement engine assumed every `-T##` ticker is a "greater"
(floor) market. Kalshi's daily-high tail markets are actually offered as BOTH
"or above" (`strike_type=greater`, floor) and "or below" (`strike_type=less`,
cap). The ticker string `-T##` is ambiguous between the two; only `strike_type`
disambiguates it.

Precedence for truth: live API single market -> live API event ladder (match
ticker) -> local authoritative kalshi_strike_ladder.db row (by ticker).

Read-only. Writes artifacts/settlement-truth/truth_2026-10-14.json.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SBOX = REPO / "data" / "paper_trading_sbox.db"
LADDER = REPO / "data" / "kalshi_strike_ladder.db"
OUT = REPO / "artifacts" / "settlement-truth"
API = "https://api.elections.kalshi.com/trade-api/v2/markets"
CACHE = Path("/tmp/kalshi_truth_cache.json")
EVENT_RE = re.compile(r"^(.*?-\d{2}[A-Z]{3}\d{2})-([TB])")


def api_get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "we-settlement-audit"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def strike_from_notes(notes: str):
    m = re.search(r"@\s*(-?\d+(?:\.\d+)?)F", notes or "")
    return float(m.group(1)) if m else None


def is_win(trade_type: str, yes: bool):
    if trade_type == "buy_yes":
        return yes
    if trade_type == "buy_no":
        return not yes
    return None


def main() -> int:
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    sbox = sqlite3.connect(f"file:{SBOX}?mode=ro", uri=True)
    sbox.row_factory = sqlite3.Row
    rows = [dict(r) for r in sbox.execute(
        "SELECT id, status, station, market_type, trade_type, signal_direction, "
        "market_ticker, canonical_settled_f, canonical_outcome, settlement_outcome, "
        "settled_value, realized_pnl, realized_pnl_old_2026, trade_price "
        "FROM trades ORDER BY id")]

    # local authoritative ladder keyed by ticker
    lad = sqlite3.connect(f"file:{LADDER}?mode=ro", uri=True)
    lad.row_factory = sqlite3.Row
    ladder = {}
    for r in lad.execute(
            "SELECT ticker, station, market_type, strike_value, strike_type, "
            "floor_strike, cap_strike, settlement_value_dollars, result "
            "FROM strike_ladder WHERE ticker IS NOT NULL"):
        ladder.setdefault(r["ticker"], dict(r))

    out = []
    for r in rows:
        t = r["market_ticker"]
        rec = {"id": r["id"], "status": r["status"], "station": r["station"],
               "market_type": r["market_type"], "trade_type": r["trade_type"],
               "ticker": t, "strike_notes": strike_from_notes_notes(r) if False else None,
               "canonical_settled_f": r["canonical_settled_f"],
               "book_canonical_outcome": r["canonical_outcome"],
               "book_settlement_outcome": r["settlement_outcome"],
               "book_settled_value": r["settled_value"],
               "book_realized_pnl": r["realized_pnl"],
               "trade_price": r["trade_price"], "src": None,
               "strike_type": None, "floor": None, "cap": None,
               "expiration_value": None, "kalshi_result": None}
        src = None
        mk = None
        if t in cache and "error" not in cache[t]:
            mk = cache[t]
            src = "api_market"
        if mk is None and t:
            try:
                mk = api_get(f"{API}/{t}").get("market")
                src = "api_market_fresh"
                cache[t] = mk
                time.sleep(0.05)
            except Exception:
                mk = None
        if mk is None and t:
            m = EVENT_RE.match(t)
            if m:
                ev = m.group(1)
                try:
                    for cand in api_get(f"{API}?event_ticker={ev}&limit=100").get("markets", []):
                        if cand.get("ticker") == t:
                            mk = cand
                            src = "api_event"
                            break
                    time.sleep(0.05)
                except Exception:
                    pass
        if mk is None and t in ladder:
            L = ladder[t]
            mk = {"strike_type": L["strike_type"], "floor_strike": L["floor_strike"],
                  "cap_strike": L["cap_strike"], "result": L["result"],
                  "expiration_value": None, "title": None}
            src = "ladder"
        if mk:
            rec["src"] = src
            rec["strike_type"] = mk.get("strike_type")
            rec["floor"] = mk.get("floor_strike")
            rec["cap"] = mk.get("cap_strike")
            rec["expiration_value"] = mk.get("expiration_value")
            rec["kalshi_result"] = mk.get("result")
            if mk.get("result") in ("yes", "no"):
                yes = mk["result"] == "yes"
                rec["truth_yes"] = yes
                rec["truth_win"] = is_win(r["trade_type"], yes)
                rec["book_win"] = (r["canonical_outcome"] == "correct") if r["canonical_outcome"] else None
                rec["flip"] = (rec["truth_win"] != rec["book_win"])
        # Also check settled_value semantics: is book settled_value the raw temp?
        rec["settled_value_is_temp"] = (
            r["settled_value"] is not None and r["canonical_settled_f"] is not None
            and abs((r["settled_value"] or -999) - (r["canonical_settled_f"] or -999)) < 1e-9)
        out.append(rec)

    CACHE.write_text(json.dumps(cache))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "truth_2026-10-14.json").write_text(json.dumps(out, indent=2))

    resolved = [x for x in out if x.get("truth_win") is not None]
    flips = [x for x in resolved if x["flip"]]
    by_st = {}
    for x in resolved:
        k = (x["strike_type"], "book_w" if x["book_win"] else "book_l")
        by_st[k] = by_st.get(k, 0) + 1
    print(f"rows={len(out)} resolved={len(resolved)} flips={len(flips)}")
    print("--- resolved by (strike_type, book) ---")
    for k in sorted(by_st, key=lambda z: (str(z[0]), z[1])):
        print("  ", k, by_st[k])
    print("--- FLIPS ---")
    for x in flips:
        print("  ", x["id"], x["ticker"], x["strike_type"], "f=", x["floor"], "c=", x["cap"],
              "exp=", x["expiration_value"], "kres=", x["kalshi_result"], x["trade_type"],
              "book=", x["book_canonical_outcome"], "->truth=", "WIN" if x["truth_win"] else "LOSS",
              "pnl_now=", x["book_realized_pnl"])
    return 0


def strike_from_notes_notes(r):
    return None


if __name__ == "__main__":
    sys.exit(main())
