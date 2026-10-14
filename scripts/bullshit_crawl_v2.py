#!/usr/bin/env python3
"""
Bullshit crawl v2 — Honest-selection audit (2026-10-14).

v1 flagged on p_fused (0.99 saturated fantasy). v2 compares the OLD decision
(p_fused) against what the HONEST signal would say (calibrated_p, ceiling-capped).
Flags any trade where old p_fused admits it but the honest EV gate would reject it.
"""
import sqlite3, json, sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BOOK = REPO / "data" / "paper_trading_sbox.db"
OUT = REPO / "data" / "bullshit_crawl_v2.json"
BANKROLL = 200.0
HONEST_EV_MIN = 0.12

con = sqlite3.connect(f"file:{BOOK}?mode=ro", uri=True)
con.row_factory = sqlite3.Row

trades = [dict(r) for r in con.execute("SELECT * FROM trades")]

# Load calibrated_p from companion table
companion = {}
try:
    for r in con.execute("SELECT trade_uuid, calibrated_p, ceiling, composite_conf, grade, p_fused as p_fused_from_companion FROM trade_forecaster_probs"):
        d = dict(r)
        companion[d["trade_uuid"]] = d
except Exception as e:
    print(f"WARN: could not read companion: {e}", file=sys.stderr)

con.close()

flips = []
for tr in trades:
    uuid = tr.get("trade_uuid", "")
    p_fused = tr.get("p_fused")
    mkt = tr.get("market_price")
    outcome = tr.get("settlement_outcome") or tr.get("canonical_outcome") or "unknown"
    pnl = tr.get("realized_pnl")

    # 1) OLD p_fused flags
    old_flags = []
    if p_fused and mkt and mkt > 0:
        edge = (p_fused - mkt) / mkt
        if edge > 0.50:
            old_flags.append("extreme_edge")
    if p_fused and p_fused >= 0.9:
        old_flags.append("s_grade_on_flat")
    if p_fused and p_fused < 0.6:
        old_flags.append("low_conf_entered")

    # 2) HONEST calibrated_p flags
    cr = companion.get(uuid, {})
    p_cal = cr.get("calibrated_p")
    ceiling = cr.get("ceiling")
    honest_flags = []
    if p_cal is not None and mkt is not None and mkt > 0:
        ev = p_cal - mkt - 0.0205
        if ev < HONEST_EV_MIN:
            honest_flags.append(f"honest_ev_too_low")
    if p_cal is not None and ceiling is not None and p_cal > ceiling:
        honest_flags.append("capped_by_ceiling")

    # 3) Flip?
    if old_flags and honest_flags:
        ev = (p_cal - mkt - 0.0205) if (p_cal is not None and mkt is not None) else None
        flips.append({
            "id": tr.get("id"), "uuid": uuid,
            "station": tr.get("station"), "date": tr.get("trade_date_utc"),
            "outcome": outcome, "pnl": pnl,
            "p_fused": p_fused, "calibrated_p": p_cal,
            "ceiling": ceiling, "market_price": mkt,
            "honest_ev": ev,
            "old_flags": old_flags, "honest_flags": honest_flags,
        })

result = {
    "generated_at_utc": None,
    "n_trades": len(trades),
    "n_with_calibrated_p": sum(1 for c in companion.values() if c.get("calibrated_p") is not None),
    "flips": len(flips),
    "flip_pct": round(100*len(flips)/len(trades), 1) if trades else 0,
    "flip_detail": flips,
}

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(result, indent=2))
print(f"wrote {OUT}")
print(f"  trades: {len(trades)}")
print(f"  with calibrated_p: {result['n_with_calibrated_p']}")
print(f"  flips (old-admits, honest-rejects): {len(flips)}")
if flips:
    c = sum(1 for f in flips if f["outcome"] == "correct")
    w = sum(1 for f in flips if f["outcome"] == "incorrect")
    print(f"  of which {c} correct / {w} incorrect (honest gate would've rejected all)")
    print(f"  first: {flips[0]['station']} {flips[0]['date']} "
          f"p_fused={flips[0]['p_fused']:.4f} cal_p={flips[0]['calibrated_p']}")