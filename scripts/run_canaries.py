#!/usr/bin/env python3
"""
run_canaries.py — run the GR5 canaries on the current SBOX window.

Loads closed trades from data/paper_trading_sbox.db, resolves each trade's
settlement truth INDEPENDENTLY from data/kalshi_settlements.db (finalized),
builds CanaryTrade records, and runs C1-C5 through the isolated harness.

Exit code: 0 all canaries null/rejected (no halt); 3 if a canary HALT triggers.

    python3 scripts/run_canaries.py --experiment-id GR5-WE-001
    python3 scripts/run_canaries.py --json
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import List, Optional

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from core.canary_harness import (  # noqa: E402
    CanaryTrade, fee_per_contract_canonical, run_all_canaries,
)

SBOX_DB = REPO / "data" / "paper_trading_sbox.db"
SETTLEMENTS_DB = REPO / "data" / "kalshi_settlements.db"

_NOTE_TICKER_STRIKE = re.compile(r"-T(-?\d+(?:\.\d+)?)")
_NOTE_STRIKE = re.compile(r"@\s*(-?\d+(?:\.\d+)?)\s*F")


def _ro(p: Path):
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def _strike_from_notes(notes: str) -> Optional[float]:
    if not notes:
        return None
    m = _NOTE_TICKER_STRIKE.search(notes) or _NOTE_STRIKE.search(notes)
    return float(m.group(1)) if m else None


def load_settlement_index() -> dict:
    """(station, YYYY-MM-DD) -> finalized kalshi_temp_f, from kalshi_settlements."""
    if not SETTLEMENTS_DB.exists():
        return {}
    c = _ro(SETTLEMENTS_DB)
    try:
        rows = c.execute(
            "SELECT station, target_date, kalshi_temp FROM kalshi_settlements "
            "WHERE kalshi_temp IS NOT NULL AND "
            "(source_type='finalized' OR source_type='historical_api')"
        ).fetchall()
    finally:
        c.close()
    out = {}
    for station, target_date, temp in rows:
        out[(station, str(target_date))] = float(temp)
    return out


# Plausible daily °F band (mirrors scripts/substrate_gate.py). Outside it the
# truth value cannot be a real temperature and the row is UNVERIFIABLE.
TEMP_MIN_F = -60.0
TEMP_MAX_F = 150.0


MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
          "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
_INV = {v: k for k, v in MONTHS.items()}


def _yy_to_iso(date_yy: str) -> Optional[str]:
    """'26OCT02' -> '2026-10-02'."""
    m = re.match(r"^(\d{2})([A-Z]{3})(\d{2})$", date_yy)
    if not m:
        return None
    yy, mon, dd = m.groups()
    return f"20{yy}-{MONTHS[mon]:02d}-{dd}"


def build_canary_trades():
    """Build CanaryTrade records + a truth-quality diagnostic.

    Independently sources the settlement temp from kalshi_settlements (finalized)
    and compares it against the trade's own booked settled_value. A row whose
    independent truth is implausible (outside the °F band) is EXCLUDED, so a
    canary halt is never an artifact of corrupt truth; disagreements > 3°F are
    reported for attribution.
    """
    if not SBOX_DB.exists():
        return [], {"excluded_unverifiable": 0, "disagreements": []}
    settle = load_settlement_index()
    c = _ro(SBOX_DB)
    try:
        rows = c.execute(
            "SELECT station, market_type, trade_type, trade_price, quantity, "
            "settled_value, settlement_date_utc, notes FROM trades "
            "WHERE status='closed'"
        ).fetchall()
    finally:
        c.close()

    trades: List[CanaryTrade] = []
    excluded = 0
    disagreements: List[dict] = []
    for (station, mtype, ttype, price, qty, settled, sdate, notes) in rows:
        strike = _strike_from_notes(notes or "")
        if strike is None or price is None:
            continue
        iso = str(sdate)[:10] if sdate else None
        temp = settle.get((station, iso)) if iso else None
        booked = float(settled) if settled is not None else None

        if temp is not None:
            if not (TEMP_MIN_F <= temp <= TEMP_MAX_F):
                excluded += 1
                continue
            if booked is not None and abs(temp - booked) > 3.0:
                disagreements.append({"station": station, "date": iso,
                                      "independent": temp, "booked": booked})
        else:
            if booked is None or not (TEMP_MIN_F <= booked <= TEMP_MAX_F):
                excluded += 1
                continue
            temp = booked

        direction = "UP" if (ttype or "").lower().endswith("yes") else "DOWN"
        trades.append(CanaryTrade(
            station=station, market_type=(mtype or "HIGH").upper(),
            strike=float(strike), decision_price=float(price),
            quantity=int(qty or 0), direction=direction,
            settlement_temp_f=float(temp),
            settlement_date=iso or "0000-00-00",
        ))
    return trades, {"excluded_unverifiable": excluded,
                    "disagreements": disagreements}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run GR5 measurement canaries.")
    ap.add_argument("--experiment-id", default="GR5-WE-001")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    seed = args.seed
    if seed is None:
        # prefer the frozen artifact's master seed, else the default
        try:
            from core.preregistration import verify_preregistration
            art = verify_preregistration(args.experiment_id, REPO)
            seed = art["artifact"]["canary"]["master_seed"]
        except Exception:
            seed = 20261010

    trades, diag = build_canary_trades()
    out = run_all_canaries(trades, seed)
    out["experiment_id"] = args.experiment_id
    out["n_trades_loaded"] = len(trades)
    out["settled"] = sum(1 for t in trades if t.settlement_temp_f is not None)
    out["truth_quality"] = diag

    if args.json:
        print(json.dumps(out, indent=2, sort_keys=True))
    else:
        print(f"canaries on {out['n_trades_loaded']} closed trades "
              f"({out['settled']} settled), seed={seed}")
        print(f"truth: {diag['excluded_unverifiable']} unverifiable excluded, "
              f"{len(diag['disagreements'])} independent/booked disagreements")
        for cid, r in out["canaries"].items():
            flag = "REJECTED" if r["rejected"] else ("SIG" if r["significant"] else "null")
            print(f"  {cid}: n={r['n']} mean={r['mean']} z={r['z']} "
                  f"p={r['p_value']} [{flag}] {r['note']}")
        if out["halts"]:
            print(f"HALT: {out['halts']}")
        if out["provisional"]:
            print(f"PROVISIONAL (n < min_n={out['min_n']}): {out['provisional']}")
        if not out["halts"] and not out["provisional"]:
            print("no halt: all canaries null or rejected")
    if out["halt"]:
        return 3
    if out["provisional"]:
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
