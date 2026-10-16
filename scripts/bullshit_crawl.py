#!/usr/bin/env python3
"""
bullshit_crawl.py — reality-check crawl of the live paper-trading book.

READ-ONLY over `data/paper_trading_sbox.db`. For every row in `trades`, flag any
number that fails a basic reality check. No DB is modified; no temp DBs.

2026-10-16 RE-TUNE (Kelly paradigm — sole om_gfs + honest calibration).
The pre-Kelly rules were built for the OLD fused probability (saturated ~0.99,
only trade p>=0.6). Under the sole-om_gfs path the decision variable is EDGE
(model p minus the market price): a deep-OTM penny can be a GOOD trade with a LOW
raw p (KBOS: p=0.079 vs market 0.025). The old rules therefore flagged 10/10
live rows with rule-vs-paradigm mismatches, not pathologies. Each rule below is
re-defined against the current paradigm; the raw-p threshold rules are gone.

Rules (a row may trip several):
  R1  realized_pnl > 200 * 10          -> "bankroll/position mismatch" (unchanged)
  R2  position_size_usd > 25           -> "over max position" (unchanged)
  R3  booked p BELOW the price paid    -> "negative edge entered"
        (edge = forecast_prob - market_price < 0). Kelly trades on edge; the
        pathology is a NEGATIVE edge, never a low absolute p. Was:
        "position_size_usd > 0 AND p_fused < 0.6".
  R4  booked p pinned AT the per-station honest ceiling
                                        -> "saturated flat signal"
        (calibrated_p >= ceiling - eps). The raw signal hit its cap, so the grade
        reflects saturation, not resolution. Was: "p_fused >= 0.9" (which under
        the honest ~0.98 ceiling is normal, not a flag). Reads the SOLE p, where
        p_gefs == p_fused by construction.
  R5  RETIRED (raw %-edge was meaningless for penny entries: KAUS edge 3031% by
        construction). Replaced by an honest BOUND check: booked p outside
        [0, 1] or ABOVE the ceiling -> "calibration bound violated". This can
        only fire on a real calibration/bookkeeping defect, never on a legitimate
        deep-OTM edge. The v2 honest-EV crawl remains the real selection gate.

Output: data/bullshit_crawl.json  (table per flagged trade + counts).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
BOOK = REPO / "data" / "paper_trading_sbox.db"
OUT = REPO / "data" / "bullshit_crawl.json"

BANKROLL = 200.0            # the $200 book
BANKROLL_MULT = 10.0        # R1: pnl > bankroll * mult
MAX_POSITION_USD = 10.0     # R2 (single source: sbox_intraday_ev_bridge MAX_POSITION_USD; Dan 2026-10-13: max $10)
CEILING_EPS = 1e-6          # R4: pinned-at-ceiling tolerance
POOLED_CEILING = 0.982      # honest pooled ceiling fallback


def _ro(p: Path) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _companion(con: sqlite3.Connection) -> dict:
    """Per-trade companion: {trade_uuid: (calibrated_p, ceiling)}.

    `calibrated_p` is the YES-side probability (P(high>=strike)) — the quantity
    the per-station honest ceiling caps. The booked `forecast_prob` is the
    BOOKED-SIDE probability (1-p for DOWN trades), so a ceiling bound MUST be
    tested against the YES-side calibrated_p, never the booked p.
    """
    out = {}
    try:
        for tuuid, cal, ce in con.execute(
                "SELECT trade_uuid, calibrated_p, ceiling FROM "
                "trade_forecaster_probs WHERE ceiling IS NOT NULL"):
            out[tuuid] = (cal, float(ce))
    except sqlite3.OperationalError:
        pass
    return out


def _comp_for(row, by_uuid: dict):
    return by_uuid.get(row.get("trade_uuid"), (None, None))


def main() -> None:
    con = _ro(BOOK)
    rows = [dict(r) for r in con.execute("SELECT * FROM trades")]
    by_uuid = _companion(con)
    con.close()

    flagged = []
    tally = {
        "bankroll/position mismatch": 0,
        "over max position": 0,
        "negative edge entered": 0,
        "saturated flat signal": 0,
        "calibration bound violated": 0,
    }
    for r in rows:
        pnl = r.get("realized_pnl")
        pos = r.get("position_size_usd") or 0.0
        # The booked decision probability (kept in sync with the companion's
        # calibrated_p by ITEM2). Fall back to p_fused only when absent.
        p = r.get("forecast_prob")
        if p is None:
            p = r.get("p_fused")
        mkt = r.get("market_price")
        cal_yes, ceiling = _comp_for(r, by_uuid)
        if ceiling is None:
            ceiling = POOLED_CEILING
        # The SOLE raw p is the YES-side P(high>=strike); p_gefs == p_fused by
        # construction on the sole path. Used for the saturation check.
        raw_yes = r.get("p_gefs")
        if raw_yes is None:
            raw_yes = r.get("p_fused")
        if cal_yes is None:
            cal_yes = raw_yes
        reasons = []
        values = {}

        # R1 — P&L exceeds 10x the bankroll
        if pnl is not None and pnl > BANKROLL * BANKROLL_MULT:
            reasons.append("bankroll/position mismatch")
            values["realized_pnl"] = pnl
            values["bankroll_x10"] = round(BANKROLL * BANKROLL_MULT, 2)

        # R2 — position over the $10 cap (single source: sbox_intraday_ev_bridge)
        if pos > MAX_POSITION_USD:
            reasons.append("over max position")
            values["position_size_usd"] = pos

        # R3 — entered at a NEGATIVE edge (model p below the price paid). Kelly
        # trades on edge; a low absolute p (a deep-OTM penny) is NOT a pathology.
        if pos > 0 and p is not None and mkt is not None:
            edge = float(p) - float(mkt)
            if edge < 0.0:
                reasons.append("negative edge entered")
                values["booked_p"] = round(float(p), 4)
                values["market_price"] = mkt
                values["edge"] = round(edge, 4)

        # R4 — the SOLE raw p is PINNED at the per-station honest ceiling (the
        # clamp engaged: the grade reflects the cap, not resolution). Reads the
        # SOLE p (p_gefs == p_fused under the sole path), compared on the YES
        # side — the side the ceiling bounds. A low p (a legitimate deep-OTM
        # penny) or a high-but-uncapped p is NOT a flag.
        if raw_yes is not None and float(raw_yes) >= ceiling - CEILING_EPS:
            reasons.append("saturated flat signal")
            values["raw_yes_p"] = round(float(raw_yes), 4)
            values["ceiling"] = round(float(ceiling), 4)
            values["p_fused"] = r.get("p_fused")
            values["p_gefs"] = r.get("p_gefs")

        # R5 (retired raw-%-edge) — honest calibration BOUND check: the YES-side
        # calibrated_p outside the unit interval or ABOVE its ceiling is a
        # calibration/bookkeeping defect (the clamp can never exceed the cap, so
        # this can only fire on a true defect — never on a legitimate edge).
        if cal_yes is not None and (float(cal_yes) < 0.0 or float(cal_yes) > 1.0
                                    or float(cal_yes) > ceiling + CEILING_EPS):
            reasons.append("calibration bound violated")
            values["calibrated_p_yes"] = round(float(cal_yes), 4)
            values["ceiling"] = round(float(ceiling), 4)

        for rule in reasons:
            tally[rule] += 1

        if reasons:
            flagged.append({
                "trade_uuid": r.get("trade_uuid"),
                "station": r.get("station"),
                "trade_date_utc": r.get("trade_date_utc"),
                "status": r.get("status"),
                "settlement_outcome": r.get("settlement_outcome"),
                "rules": reasons,
                "values": values,
            })

    try:
        src_db = str(BOOK.relative_to(REPO))
    except ValueError:
        src_db = str(BOOK)
    result = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_db": src_db,
        "paradigm": "sole om_gfs + honest calibration + Kelly edge sizing",
        "bankroll_usd": BANKROLL,
        "total_trades": len(rows),
        "flagged_trades": len(flagged),
        "clean_trades": len(rows) - len(flagged),
        "flagged_pct": round(100 * len(flagged) / len(rows), 1) if rows else 0.0,
        "flags_per_rule": tally,
        "rule_definitions": {
            "bankroll/position mismatch": f"realized_pnl > {BANKROLL} x {BANKROLL_MULT} = {BANKROLL * BANKROLL_MULT:.0f}",
            "over max position": f"position_size_usd > {MAX_POSITION_USD}",
            "negative edge entered": "position_size_usd > 0 and forecast_prob - market_price < 0 "
                                     "(Kelly trades on edge; low absolute p is not a flag)",
            "saturated flat signal": "sole raw YES-side p >= per-station honest ceiling "
                                     "(the calibration clamp engaged)",
            "calibration bound violated": "YES-side calibrated_p outside [0,1] or above its "
                                          "ceiling (replaces the retired raw-%-edge rule)",
        },
        "rule_changes_2026_10_16": [
            "R3: absolute p_fused<0.6 -> edge < 0 (model below market). Under Kelly a "
            "deep-OTM penny (KBOS p=0.079 @ 0.025) is a good trade, not 'low conf'.",
            "R4: p_fused>=0.9 -> booked p pinned at the per-station ceiling. 0.9 is "
            "normal under the honest ~0.98 ceiling; only true saturation flags.",
            "R5: RETIRED raw %-edge (KAUS 3031% by construction on a penny entry) -> "
            "honest ceiling/unit-interval bound check; v2 honest-EV crawl is the gate.",
        ],
        "flagged": flagged,
    }
    OUT.write_text(json.dumps(result, indent=2))

    print(f"bullshit crawl: {len(rows)} trades, {len(flagged)} flagged "
          f"({result['flagged_pct']}%), {len(rows) - len(flagged)} clean")
    for rule, n in sorted(tally.items(), key=lambda kv: -kv[1]):
        print(f"  {rule:32} {n}")
    try:
        out_rel = OUT.relative_to(REPO)
    except ValueError:
        out_rel = OUT
    print(f"wrote {out_rel}")


if __name__ == "__main__":
    main()
