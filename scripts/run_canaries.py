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

    Label source hierarchy (GR5 §3 settled-admission gate alignment):
      1. trades.canonical_settled_f / canonical_outcome — the canonical truth
      2. kalshi_settlements.db (finalized/historical_api) — fallback
      3. trades.settled_value (booked) — last resort, marked

    Rows whose label source is implausible (outside the °F band) are EXCLUDED.
    Disagreements >3°F between canonical and booked are reported for attribution
    but no longer drive the label.
    """
    if not SBOX_DB.exists():
        return [], {"excluded_unverifiable": 0, "disagreements": [],
                    "canonical_source": 0, "settlements_source": 0,
                    "booked_fallback": 0}
    settle = load_settlement_index()
    c = _ro(SBOX_DB)
    try:
        rows = c.execute(
            "SELECT station, market_type, trade_type, trade_price, quantity, "
            "settled_value, settlement_date_utc, notes, canonical_settled_f, "
            "canonical_outcome FROM trades WHERE status='closed'"
        ).fetchall()
    finally:
        c.close()

    trades: List[CanaryTrade] = []
    excluded = 0
    disagreements: List[dict] = []
    canonical_source_n = 0
    settlements_source_n = 0
    booked_fallback_n = 0
    for (station, mtype, ttype, price, qty, settled, sdate, notes,
         canon_f, canon_outcome) in rows:
        strike = _strike_from_notes(notes or "")
        if strike is None or price is None:
            continue
        iso = str(sdate)[:10] if sdate else None

        # ── Label source: canonical #1 ──
        if canon_f is not None:
            temp = float(canon_f)
            label_source = "canonical"
            canonical_source_n += 1
        else:
            # ── Label source: independent settlements DB #2 ──
            temp_ind = settle.get((station, iso)) if iso else None
            if temp_ind is not None and (TEMP_MIN_F <= temp_ind <= TEMP_MAX_F):
                temp = float(temp_ind)
                label_source = "settlements_db"
                settlements_source_n += 1
            else:
                # ── Label source: booked fallback #3 (explicitly marked) ──
                booked = float(settled) if settled is not None else None
                if booked is None or not (TEMP_MIN_F <= booked <= TEMP_MAX_F):
                    excluded += 1
                    continue
                temp = booked
                label_source = "booked_fallback"
                booked_fallback_n += 1

        # Track canonical-vs-booked divergence for attribution (diagnostic only)
        booked = float(settled) if settled is not None else None
        if booked is not None and label_source != "booked_fallback":
            if abs(temp - booked) > 3.0:
                disagreements.append({"station": station, "date": iso,
                                      "label_temp": temp, "booked": booked,
                                      "label_source": label_source})

        direction = "UP" if (ttype or "").lower().endswith("yes") else "DOWN"
        trades.append(CanaryTrade(
            station=station, market_type=(mtype or "HIGH").upper(),
            strike=float(strike), decision_price=float(price),
            quantity=int(qty or 0), direction=direction,
            settlement_temp_f=float(temp),
            settlement_date=iso or "0000-00-00",
            label_source=label_source,
        ))
    return trades, {"excluded_unverifiable": excluded,
                    "disagreements": disagreements,
                    "canonical_source": canonical_source_n,
                    "settlements_source": settlements_source_n,
                    "booked_fallback": booked_fallback_n}


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
              f"{len(diag['disagreements'])} canonical/booked disagreements")
        print(f"label sources: {diag['canonical_source']} canonical, "
              f"{diag['settlements_source']} settlements_db, "
              f"{diag['booked_fallback']} booked_fallback")
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
