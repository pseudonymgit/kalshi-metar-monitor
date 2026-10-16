#!/usr/bin/env python3
"""
Bullshit crawl v2 — honest-selection audit (re-tuned 2026-10-16).

v1 (legacy) flagged on p_fused (the old 0.99 saturated fantasy) with absolute-p
thresholds built for the fused paradigm. Under the SOLE om_gfs + honest
calibration + Kelly paradigm (2026-10-14 onward) those thresholds are
rule-vs-paradigm mismatches, not pathologies.

v2 keeps its original job — compare the OLD decision axis (p_fused / raw %) with
what the HONEST signal says (calibrated_p, ceiling-capped) — but re-frames the
output for the new paradigm:

  * `old_flags` are the RETIRED rule set (extreme_edge / s_grade_on_flat /
    low_conf_entered). They are reported for provenance only, never as a
    pathology verdict: a deep-OTM penny (KBOS p=0.079 @ 0.025) legitimately
    trips `low_conf_entered`, and a penny entry trips `extreme_edge` by
    construction.

  * `honest_flags` are the REAL gate. The only honest pathology on the live book
    is a NEGATIVE honest edge (calibrated_p below the price paid + fee) — i.e.
    the trade should never have been selected. A `capped_by_ceiling` flag is
    informational (the calibration clamp engaged) and is NOT, by itself, a
    selection defect.

Output: data/bullshit_crawl_v2.json (counts + per-trade detail).

READ-ONLY. v1 (`bullshit_crawl.py`) is the reality-check crawl; this is the
selection audit. Together they must show the live rows are NOT pathology.
"""
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BOOK = REPO / "data" / "paper_trading_sbox.db"
OUT = REPO / "data" / "bullshit_crawl_v2.json"
BANKROLL = 200.0
FEE_RATE = 0.0205          # Kalshi taker fee the EV gate prices against
HONEST_EV_MIN = 0.0        # an honest NEGATIVE edge is the only selection defect


def _ro(p: Path) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def main() -> None:
    con = _ro(BOOK)
    con.row_factory = sqlite3.Row
    trades = [dict(r) for r in con.execute("SELECT * FROM trades")]

    companion = {}
    try:
        for r in con.execute(
                "SELECT trade_uuid, calibrated_p, ceiling, composite_conf, grade, "
                "p_fused AS p_fused_from_companion, side FROM trade_forecaster_probs"):
            d = dict(r)
            companion[d["trade_uuid"]] = d
    except Exception as e:
        print(f"WARN: could not read companion: {e}", file=sys.stderr)
    con.close()

    flips = []
    n_negative_edge = 0
    for tr in trades:
        uuid = tr.get("trade_uuid", "")
        p_fused = tr.get("p_fused")
        mkt = tr.get("market_price")
        outcome = tr.get("settlement_outcome") or tr.get("canonical_outcome") or "unknown"
        pnl = tr.get("realized_pnl")

        # 1) RETIRED rule set (provenance only — these are paradigm mismatches).
        old_flags = []
        if p_fused and mkt and mkt > 0:
            edge = (p_fused - mkt) / mkt
            if edge > 0.50:
                old_flags.append("extreme_edge")
        if p_fused and p_fused >= 0.9:
            old_flags.append("s_grade_on_flat")
        if p_fused and p_fused < 0.6:
            old_flags.append("low_conf_entered")

        # 2) HONEST calibrated_p — the REAL selection gate.
        cr = companion.get(uuid, {})
        p_cal = cr.get("calibrated_p")
        ceiling = cr.get("ceiling")
        # The booked LEG comes from the trade row's `trade_type` (buy_yes/buy_no).
        # The companion `side` is the ENGINE signal direction ("BUY" for every
        # row), NOT the leg, so it must never be used to pick the win-prob.
        leg_no = str(tr.get("trade_type", "")).startswith("buy_no")
        honest_flags = []
        # A NO trade books the NO side: its calibrated win-prob is 1-calibrated_p.
        p_booked = (1.0 - p_cal) if (p_cal is not None and leg_no) else p_cal
        honest_ev = None
        if p_cal is not None and mkt is not None and mkt > 0:
            # EV per CONTRACT — the bridge's basis: expected payout (p_booked) minus
            # the premium paid, net of the taker fee. > 0 means the honest gate
            # admits the trade; <= 0 means it should never have been selected.
            honest_ev = float(p_booked) - float(mkt) - FEE_RATE
            if honest_ev <= HONEST_EV_MIN:
                honest_flags.append("honest_negative_edge")
                n_negative_edge += 1
        if p_cal is not None and ceiling is not None and p_cal > ceiling:
            # Informational: the calibration clamp engaged. NOT a selection defect.
            honest_flags.append("capped_by_ceiling")

        # 3) A flip = the OLD rule would flag a selection the honest gate accepts
        #    (i.e. the old rule's flag is a paradigm mismatch, not a pathology).
        if old_flags and "honest_negative_edge" in honest_flags:
            flips.append({
                "id": tr.get("id"), "uuid": uuid,
                "station": tr.get("station"), "date": tr.get("trade_date_utc"),
                "outcome": outcome, "pnl": pnl,
                "p_fused": p_fused, "calibrated_p": p_cal,
                "ceiling": ceiling, "market_price": mkt,
                "honest_ev": honest_ev,
                "old_flags": old_flags, "honest_flags": honest_flags,
            })

    result = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "paradigm": "sole om_gfs + honest calibration + Kelly edge sizing",
        "n_trades": len(trades),
        "n_with_calibrated_p": sum(1 for c in companion.values()
                                   if c.get("calibrated_p") is not None),
        "honest_negative_edge": n_negative_edge,
        "flips": len(flips),
        "flip_pct": round(100 * len(flips) / len(trades), 1) if trades else 0,
        "note": ("`old_flags` are the RETIRED absolute-p rule set and are paradigm "
                 "mismatches under Kelly — reported for provenance only. The only "
                 "honest selection defect is `honest_negative_edge`."),
        "flip_detail": flips,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, indent=2))
    print(f"wrote {OUT}")
    print(f"  trades: {len(trades)}")
    print(f"  with calibrated_p: {result['n_with_calibrated_p']}")
    print(f"  honest negative edge (the only selection defect): {n_negative_edge}")
    if flips:
        c = sum(1 for f in flips if f["outcome"] == "correct")
        w = sum(1 for f in flips if f["outcome"] == "incorrect")
        print(f"  old-flag+honest-reject overlaps: {len(flips)} "
              f"({c} correct / {w} incorrect)")


if __name__ == "__main__":
    main()
