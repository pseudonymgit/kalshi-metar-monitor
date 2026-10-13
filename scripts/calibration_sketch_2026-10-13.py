#!/usr/bin/env python3
"""
calibration_sketch_2026-10-13.py — bounded, READ-ONLY calibration sketch + entry-gate rebuild.

Context (EXPERT-METHODOLOGY-REVIEW-2026-10-13.md, E2/§0):
  p_fused is saturated at 0.99 (37/82 trades), the realized win rate is 67.6%, and the
  entry rule selects on max(p_fused - market_price) — which maximizes an uncalibrated,
  ~30pp-overstated gap and therefore selects the deepest-OTM, widest-spread contracts.

This script is NOT production. It is a bounded analysis that asks: if we squash the
saturated confidence into a plausible range, how many trades still clear a Dan-grade
entry gate, and what does the P&L look like under grade-scaled sizing?

Sketch transform (per task):
    calibrated_p = (p_dir - 0.50) / (0.99 - 0.50) * 0.25 + 0.50
  mapping the 0.50-0.99 range into ~0.50-0.75 (near the observed 67.6% win rate).

  p_dir = our probability for the TRADED direction:
          p_fused for UP trades; (1 - p_fused) for DOWN trades.
          (The book stores p_fused = P(high >= strike); 80/82 trades are UP, so
           p_dir == p_fused for almost every row. Only id=246 is a closed DOWN.)

Entry gate (rebuilt):
    enter  iff  grade(calibrated_p) >= C  (calibrated_p >= 0.60)  AND  entry_EV > 0
    entry_EV = calibrated_p - market_price - slippage_per_contract

Dan's ladder (scripts/sbox_bankroll_timestop_qdist.py, 2026-10-12):
    S >= 0.90 -> cap $25, A >= 0.80 -> $15, B >= 0.70 -> $10, C >= 0.60 -> $5, F -> $0

Sizing resimulation (no new DB writes):
    new_q   = floor(new_cap / trade_price)
    new_pnl = new_q * (settled_value - trade_price)     # per-contract settle P&L

READ-ONLY over data/paper_trading_sbox.db. Writes data/calibration_sketch.json only.
"""
from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BOOK = REPO / "data" / "paper_trading_sbox.db"
OUT = REPO / "data" / "calibration_sketch.json"

# ── constants (sourced from the repo, not invented) ──────────────────────────
SLIPPAGE = 0.005                       # core/market_cost_model.py DEFAULT_SLIPPAGE
MEASURED_MEAN_SPREAD = 0.031           # core/market_cost_model.py MEASURED_MEAN_SPREAD
HALF_SPREAD = MEASURED_MEAN_SPREAD / 2.0
P_LO, P_HI, SPAN = 0.50, 0.99, 0.25    # squash source range / target span
GRADE_BANDS = [                        # Dan's 2026-10-12 contiguous ladder
    (0.90, "S", 25.0),
    (0.80, "A", 15.0),
    (0.70, "B", 10.0),
    (0.60, "C", 5.0),
]


def taker_fee(price: float) -> float:
    """Canonical Kalshi taker fee per contract: ceil(0.07*P*(1-P)*100)/100."""
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return math.ceil(0.07 * price * (1.0 - price) * 100.0) / 100.0


def squash(p_dir: float) -> float:
    """Map 0.50-0.99 -> 0.50-0.75 (clamped)."""
    v = (p_dir - P_LO) / (P_HI - P_LO) * SPAN + P_LO
    return max(P_LO, min(P_HI, v))


def grade_of(p: float):
    for lo, g, cap in GRADE_BANDS:
        if p >= lo:
            return g, cap
    return "F", 0.0


def _ro() -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{BOOK}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def main() -> None:
    con = _ro()
    rows = [dict(r) for r in con.execute(
        "SELECT id, trade_uuid, station, trade_date_utc, market_type, signal_direction,"
        " forecast_prob, market_price, trade_price, trade_type, quantity, position_size_usd,"
        " realized_pnl, settled_value, status, p_gefs, p_analog, p_trajectory, p_fused,"
        " canonical_outcome"
        " FROM trades ORDER BY id")]
    con.close()

    all_rows = rows
    closed = [r for r in rows if r["status"] == "closed"
              and r["canonical_outcome"] in ("correct", "incorrect")]

    # ── baseline (as-traded) ─────────────────────────────────────────────────
    base_pnl = sum(r["realized_pnl"] or 0.0 for r in closed)
    base_wins = sum(1 for r in closed if r["canonical_outcome"] == "correct")
    base_n = len(closed)

    # saturation profile on the whole book
    pf_all = [r["p_fused"] for r in all_rows if r["p_fused"] is not None]
    sat = {
        "n_all_trades": len(all_rows),
        "n_closed": base_n,
        "p_fused_ge_0.985": sum(1 for p in pf_all if p >= 0.985),
        "p_fused_ge_0.985_pct": round(100.0 * sum(1 for p in pf_all if p >= 0.985) / len(pf_all), 1),
        "p_fused_eq_0.99": sum(1 for p in pf_all if abs(p - 0.99) < 1e-9),
        "p_fused_eq_0.99_pct": round(100.0 * sum(1 for p in pf_all if abs(p - 0.99) < 1e-9) / len(pf_all), 1),
        "p_fused_max": max(pf_all),
        "p_fused_min": min(pf_all),
        "realized_win_rate_pct": round(100.0 * base_wins / base_n, 1),
    }

    # ── per-trade calibration sketch ─────────────────────────────────────────
    per_trade = []
    entered = []
    skipped = []
    for r in closed:
        p_fused = r["p_fused"]
        direction = r["signal_direction"]
        p_dir = p_fused if direction == "UP" else (1.0 - p_fused)
        cal = squash(p_dir)
        mkt = r["market_price"]
        ev_slip = cal - mkt - SLIPPAGE
        # full round-trip execution cost variant (spread/2 + slippage + taker fee)
        rt_cost = HALF_SPREAD + SLIPPAGE + taker_fee(mkt)
        ev_rt = cal - mkt - rt_cost
        g, cap = grade_of(cal)
        g_old, cap_old = grade_of(p_fused)
        trade_price = r["trade_price"]
        correct = r["canonical_outcome"] == "correct"
        entry = (g != "F") and (ev_slip > 0.0)
        rec = {
            "id": r["id"], "station": r["station"], "trade_date_utc": r["trade_date_utc"],
            "direction": direction, "market_price": round(mkt, 4),
            "trade_price": round(trade_price, 4), "quantity": r["quantity"],
            "p_fused": round(p_fused, 4), "p_dir": round(p_dir, 4),
            "calibrated_p": round(cal, 4),
            "grade_old": g_old, "grade_calibrated": g,
            "cap_old_usd": cap_old, "cap_calibrated_usd": cap,
            "ev_slip": round(ev_slip, 4), "ev_round_trip": round(ev_rt, 4),
            "enter": entry,
            "canonical_outcome": r["canonical_outcome"],
            "as_traded_pnl": round(r["realized_pnl"] or 0.0, 4),
        }
        # Per-contract settlement P&L from the BINARY outcome (settled_value holds
        # the settled TEMPERATURE, not a 0/1 payout, so it must not be used here):
        #   buy_yes: payoff 1 if correct else 0; cost = trade_price
        #   buy_no : cost = 1 - trade_price; payoff 1 if correct else 0
        if r["trade_type"] == "buy_no":
            pnl_per = trade_price if correct else -(1.0 - trade_price)
        else:
            pnl_per = (1.0 if correct else 0.0) - trade_price
        # resimulate P&L at calibrated grade cap (only if entering)
        if entry and trade_price and trade_price > 0:
            new_q = int(cap / trade_price)
            new_pnl = new_q * pnl_per
            rec["calibrated_contracts"] = new_q
            rec["calibrated_pnl"] = round(new_pnl, 4)
            rec["pnl_per_contract"] = round(pnl_per, 4)
            entered.append(rec)
        else:
            rec["calibrated_contracts"] = 0
            rec["calibrated_pnl"] = 0.0
            skipped.append(rec)
        per_trade.append(rec)

    cal_pnl = sum(x["calibrated_pnl"] for x in entered)
    cal_wins = sum(1 for x in entered if x["canonical_outcome"] == "correct")
    cal_n = len(entered)

    # constant-1-contract variant (removes the sizing ladder fiction entirely)
    c1_pnl = sum(x["pnl_per_contract"] for x in entered)
    c1_n = len(entered)
    # sanity: recomputed as-traded P&L must match the book
    recon_pnl = 0.0
    for r in closed:
        tp = r["trade_price"]
        ok = r["canonical_outcome"] == "correct"
        per = (tp if ok else -(1.0 - tp)) if r["trade_type"] == "buy_no" else ((1.0 if ok else 0.0) - tp)
        recon_pnl += r["quantity"] * per

    grade_dist_cal = Counter(x["grade_calibrated"] for x in per_trade)
    grade_dist_old = Counter(x["grade_old"] for x in per_trade)

    # what the gate would have kept vs dropped, and P&L by kept/dropped
    dropped_pnl_as_traded = sum(x["as_traded_pnl"] for x in skipped)

    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_db": "data/paper_trading_sbox.db (read-only)",
        "method": "bounded calibration sketch + entry-gate rebuild (NOT production)",
        "transform": "calibrated_p = (p_dir - 0.50)/(0.99-0.50)*0.25 + 0.50",
        "slippage_per_contract": SLIPPAGE,
        "measured_mean_spread": MEASURED_MEAN_SPREAD,
        "grade_bands": {g: lo for lo, g, _ in GRADE_BANDS} | {"F": 0.0},
        "grade_caps_usd": {g: cap for _, g, cap in GRADE_BANDS} | {"F": 0.0},
        "saturation": sat,
        "as_traded": {
            "n_closed": base_n,
            "win_rate_pct": round(100.0 * base_wins / base_n, 1),
            "realized_pnl_usd": round(base_pnl, 2),
            "recomputed_pnl_usd": round(recon_pnl, 2),
            "recon_delta_usd": round(recon_pnl - base_pnl, 2),
        },
        "calibrated_gate": {
            "rule": "enter iff grade(calibrated_p) >= C (>=0.60) AND (calibrated_p - market - slippage) > 0",
            "n_entered": cal_n,
            "n_skipped": len(skipped),
            "pct_entered": round(100.0 * cal_n / base_n, 1),
            "entered_win_rate_pct": round(100.0 * cal_wins / cal_n, 1) if cal_n else 0.0,
            "calibrated_sizing_pnl_usd": round(cal_pnl, 2),
            "constant_1_contract_pnl_usd": round(c1_pnl, 2),
            "dropped_as_traded_pnl_usd": round(dropped_pnl_as_traded, 2),
            "grade_distribution_calibrated": dict(sorted(grade_dist_cal.items())),
            "grade_distribution_before": dict(sorted(grade_dist_old.items())),
            "max_possible_grade": max(grade_dist_cal, key=lambda k: {"S": 5, "A": 4, "B": 3, "C": 2, "F": 1}[k]),
        },
        "note": (
            "calibrated_p is capped at 0.75 by construction, so the best reachable grade is B; "
            "the S/A bands are unreachable under this sketch. Gate re-admits the same deep-OTM cohort "
            "because market_price << 0.50 keeps EV positive — the squash lowers confidence but does not, "
            "by itself, change the selection ranking. A cost/liquidity filter (spread/mid, depth) is the "
            "missing complement (see EXPERT-METHODOLOGY-REVIEW E3)."
        ),
        "trades": per_trade,
    }

    OUT.write_text(json.dumps(summary, indent=1))
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
    print("saturation:", json.dumps(sat))
    print("gate:", json.dumps({k: v for k, v in summary["calibrated_gate"].items()
                               if k != "grade_distribution_calibrated"}))
    print("entered pnl=%.2f (n=%d) | const-1 pnl=%.2f" % (cal_pnl, cal_n, c1_pnl))


if __name__ == "__main__":
    main()
