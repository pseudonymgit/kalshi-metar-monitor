#!/usr/bin/env python3
"""
look200_measurement.py — GR5-WE-001 pre-registered measurement battery (Look-200).

Read-only. Deterministic. No AI/ML.

Runs the frozen GR5-WE-001 look-200 battery against the live SBOX book cohort:
  * accuracy on the strike-crossing axis (canonical outcomes; strict '>')
  * per-trade net P&L net of the canonical Kalshi fee
  * Sharpe (per-trade; NOT bankroll-compounded)
  * block-bootstrap CI on mean per-trade net P&L (blocks = settlement date)
  * the C1-C5 canaries + CUSUM drift monitor (frozen harness, isolated path)
  * the A3 net-EV hurdle read-out
  * win rate by direction, East/West accuracy split, per-station weakness

The canonical fee is core.fees.fee_per_contract(P) = ceil(0.07*P*(1-P)*100)/100.
The book's `realized_pnl` is GROSS (per the settlement convention); this script
charges the canonical fee to produce net P&L. Gross and net are both reported.

Usage:
    python3 scripts/look200_measurement.py --json       # machine-readable
    python3 scripts/look200_measurement.py              # human summary
"""
from __future__ import annotations

import argparse
import json
import math
import random
import re
import sqlite3
import sys
from collections import OrderedDict, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from core.fees import fee_per_contract            # noqa: E402
from core.canary_harness import (                  # noqa: E402
    CanaryTrade, run_all_canaries,
)
from core.preregistration import (                 # noqa: E402
    verify_preregistration, analysis_code_hash, ANALYSIS_CODE_MODULES,
)

SBOX_DB = REPO / "data" / "paper_trading_sbox.db"
EXPERIMENT_ID = "GR5-WE-001"
BOOTSTRAP_REPS = 10000          # prereg frozen bootstrap_reps
BOOTSTRAP_SEED = 20261010
MIN_N_LOOK = 200                # first O'Brien-Fleming look == hurdle min_events

_NOTE_TICKER_STRIKE = re.compile(r"-T(-?\d+(?:\.\d+)?)")
_NOTE_STRIKE = re.compile(r"@\s*(-?\d+(?:\.\d+)?)\s*F")

# Region map (lat/lon/region) from core/spatial_coherence.py; East/West split
# at the 100th meridian (lon >= -100 => East). NE/SE/SC/MW => East; RW/PAC => West.
WEST_LON = -100.0


def _ro(p: Path):
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def _strike_from_notes(notes: str) -> Optional[float]:
    if not notes:
        return None
    m = _NOTE_TICKER_STRIKE.search(notes) or _NOTE_STRIKE.search(notes)
    return float(m.group(1)) if m else None


def wilson_ci(k: int, n: int, z: float = 1.959963985) -> Tuple[float, float]:
    """Wilson score interval for a binomial proportion."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def load_cohort() -> List[Dict[str, Any]]:
    """The usable settled cohort: settlement_confirmed=1 AND canonical_outcome NOT NULL."""
    con = _ro(SBOX_DB)
    try:
        rows = con.execute(
            "SELECT id, station, market_type, signal_direction, trade_type, "
            "forecast_prob, trade_price, quantity, canonical_settled_f, "
            "canonical_outcome, settlement_date_utc, notes, realized_pnl, "
            "settlement_return_amount FROM trades "
            "WHERE settlement_confirmed=1 AND canonical_outcome IS NOT NULL "
            "ORDER BY id"
        ).fetchall()
    finally:
        con.close()

    out: List[Dict[str, Any]] = []
    for (tid, station, mtype, sdir, ttype, fp, price, qty, cf, cout,
         sdate, notes, rpnl, sret) in rows:
        strike = _strike_from_notes(notes or "")
        out.append({
            "id": tid, "station": station, "market_type": (mtype or "HIGH").upper(),
            "signal_direction": (sdir or "UP").upper(),
            "trade_type": ttype,
            "forecast_prob": fp, "trade_price": float(price), "quantity": int(qty or 0),
            "canonical_settled_f": cf, "canonical_outcome": cout,
            "settlement_date": str(sdate)[:10] if sdate else "0000-00-00",
            "strike": strike,
            "realized_pnl": float(rpnl) if rpnl is not None else None,
            "settlement_return_amount": sret,
        })
    return out


def axis_outcome(row: Dict[str, Any]) -> Optional[str]:
    """Raw strike-crossing YES/NO truth (strict '>'): 'up' if ITM else 'down'."""
    t, s, mt = row["canonical_settled_f"], row["strike"], row["market_type"]
    if t is None or s is None:
        return None
    if mt == "HIGH":
        itm = t > s
        return "up" if itm else "down"
    itm = t < s
    return "down" if itm else "up"   # LOW: temp<strike => DOWN side won


def build_canary_trades(cohort: List[Dict[str, Any]]) -> List[CanaryTrade]:
    """Same construction as scripts/run_canaries.build_canary_trades (canonical label)."""
    trades: List[CanaryTrade] = []
    for r in cohort:
        if r["strike"] is None or r["canonical_settled_f"] is None:
            continue
        direction = "UP" if (r["trade_type"] or "").lower().endswith("yes") else "DOWN"
        trades.append(CanaryTrade(
            station=r["station"], market_type=r["market_type"], strike=float(r["strike"]),
            decision_price=float(r["trade_price"]), quantity=int(r["quantity"]),
            direction=direction, settlement_temp_f=float(r["canonical_settled_f"]),
            settlement_date=r["settlement_date"], label_source="canonical",
        ))
    return trades


def block_bootstrap_mean(values: List[float], blocks: List[str],
                         reps: int = BOOTSTRAP_REPS, seed: int = BOOTSTRAP_SEED
                         ) -> Tuple[float, float]:
    """Percentile CI on the mean, resampling settlement-date blocks with replacement."""
    groups: "OrderedDict[str, List[float]]" = OrderedDict()
    for v, b in zip(values, blocks):
        groups.setdefault(b, []).append(v)
    keys = list(groups)
    if not keys:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    means: List[float] = []
    nblocks = len(keys)
    for _ in range(reps):
        pool: List[float] = []
        for _ in range(nblocks):
            pool.extend(groups[keys[rng.randrange(nblocks)]])
        means.append(sum(pool) / len(pool))
    means.sort()
    lo = means[int(0.025 * reps)]
    hi = means[min(reps - 1, int(0.975 * reps))]
    return (lo, hi)


def mean_std(vals: List[float]) -> Tuple[float, float]:
    n = len(vals)
    if n == 0:
        return (0.0, 0.0)
    m = sum(vals) / n
    if n < 2:
        return (m, 0.0)
    var = sum((v - m) ** 2 for v in vals) / (n - 1)
    return (m, math.sqrt(var))


def station_region(station: str) -> str:
    """East/West via the 100th meridian using the core.spatial_coherence lon map."""
    lon = _STATION_LON.get(station)
    if lon is None:
        return "UNKNOWN"
    return "East" if lon >= WEST_LON else "West"


def _load_station_lon() -> Dict[str, float]:
    src = REPO / "core" / "spatial_coherence.py"
    out: Dict[str, float] = {}
    if not src.exists():
        return out
    for st, body in re.findall(r"'(K[A-Z]{3})':\s*\{([^}]*)\}", src.read_text()):
        m = re.search(r"'lon':\s*(-?[\d.]+)", body)
        if m and st not in out:
            out[st] = float(m.group(1))
    return out


_STATION_LON = _load_station_lon()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="GR5-WE-001 look-200 measurement battery")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--experiment-id", default=EXPERIMENT_ID)
    args = ap.parse_args(argv)

    # ── 0) frozen contract verification ──────────────────────────────────────
    pre = verify_preregistration(args.experiment_id, REPO)
    art = pre["artifact"]
    live_code = analysis_code_hash(REPO, ANALYSIS_CODE_MODULES)
    code_ok = (live_code["combined_sha256"] == art["analysis_code"]["combined_sha256"])
    module_ok = all(
        live_code["modules"].get(k) == v
        for k, v in art["analysis_code"]["modules"].items()
    )

    # ── 1) cohort ────────────────────────────────────────────────────────────
    cohort = load_cohort()
    n = len(cohort)

    # Canonical-outcome accuracy (direction-aware; the pre-registered label).
    correct = sum(1 for r in cohort if r["canonical_outcome"] == "correct")
    acc = correct / n if n else 0.0
    acc_lo, acc_hi = wilson_ci(correct, n)

    # Raw strike-crossing axis cross-check (should agree on LONG-YES rows).
    axis_agree = 0
    axis_checked = 0
    for r in cohort:
        ax = axis_outcome(r)
        if ax is None:
            continue
        axis_checked += 1
        want = "up" if r["signal_direction"] == "UP" else "down"
        if (ax == want) == (r["canonical_outcome"] == "correct"):
            axis_agree += 1

    # ── 2) P&L: gross vs net of the canonical fee ─────────────────────────────
    net_dollars: List[float] = []
    gross_dollars: List[float] = []
    net_per_contract: List[float] = []
    blocks: List[str] = []
    fee_total = 0.0
    gross_total = 0.0
    anomalies: List[Dict[str, Any]] = []
    for r in cohort:
        if r["realized_pnl"] is None:
            continue
        qty = r["quantity"]
        fpc = fee_per_contract(r["trade_price"])
        fee = fpc * qty
        g = r["realized_pnl"]
        net = g - fee
        gross_dollars.append(g)
        net_dollars.append(net)
        net_per_contract.append((g - fee) / qty if qty else 0.0)
        blocks.append(r["settlement_date"])
        gross_total += g
        fee_total += fee
        # Anomaly screen: a long-YES payoff is qty*(1-price) on a win and
        # -qty*price on a loss; realized_pnl == +qty*price is never legitimate
        # for long YES (it would mean booking cost as payoff). A winning NO trade
        # legitimately pays +qty*price, so NO rows are excluded from this screen.
        is_long_yes = (r["signal_direction"] == "UP"
                       and (r["trade_type"] or "").endswith("yes"))
        if is_long_yes and qty and abs(g - qty * r["trade_price"]) < 1e-6:
            anomalies.append({"id": r["id"], "station": r["station"],
                              "direction": r["signal_direction"],
                              "trade_type": r["trade_type"], "qty": qty,
                              "trade_price": r["trade_price"], "realized_pnl": g,
                              "note": "long-YES row where realized_pnl == +qty*price (cost booked as payoff)"})

    nd_mean, nd_std = mean_std(net_dollars)
    nd_sharpe = (nd_mean / nd_std) if nd_std > 0 else 0.0
    pc_mean, pc_std = mean_std(net_per_contract)
    pc_sharpe = (pc_mean / pc_std) if pc_std > 0 else 0.0
    boot_lo, boot_hi = block_bootstrap_mean(net_dollars, blocks)
    boot_pc_lo, boot_pc_hi = block_bootstrap_mean(net_per_contract, blocks)

    # Sensitivity: drop the anomalous non-YES row.
    non_anom_ids = {a["id"] for a in anomalies}
    nd_clean = [v for v, r in zip(net_dollars, [x for x in cohort if x["realized_pnl"] is not None])
                if r["id"] not in non_anom_ids]
    nd_clean_mean, nd_clean_std = mean_std(nd_clean)
    nd_clean_sharpe = (nd_clean_mean / nd_clean_std) if nd_clean_std > 0 else 0.0

    # A3 hurdle read-out: one-sided 95% upper bound on mean net-EV per contract.
    se_pc = pc_std / math.sqrt(len(net_per_contract)) if net_per_contract else 0.0
    upper_95_bound = pc_mean + 1.645 * se_pc

    # ── 3) canaries (frozen isolated harness) ────────────────────────────────
    canary_trades = build_canary_trades(cohort)
    can = run_all_canaries(canary_trades, art["canary"]["master_seed"], min_n=MIN_N_LOOK)

    # ── 4) sub-group splits ──────────────────────────────────────────────────
    by_dir: Dict[str, Dict[str, int]] = defaultdict(lambda: {"n": 0, "correct": 0})
    by_region: Dict[str, Dict[str, int]] = defaultdict(lambda: {"n": 0, "correct": 0})
    by_station: Dict[str, Dict[str, int]] = defaultdict(lambda: {"n": 0, "correct": 0})
    dates_per_station: Dict[str, set] = defaultdict(set)
    for r in cohort:
        ok = 1 if r["canonical_outcome"] == "correct" else 0
        by_dir[r["signal_direction"]]["n"] += 1
        by_dir[r["signal_direction"]]["correct"] += ok
        reg = station_region(r["station"])
        by_region[reg]["n"] += 1
        by_region[reg]["correct"] += ok
        by_station[r["station"]]["n"] += 1
        by_station[r["station"]]["correct"] += ok
        dates_per_station[r["station"]].add(r["settlement_date"])

    region_out = {}
    for reg, d in by_region.items():
        lo, hi = wilson_ci(d["correct"], d["n"])
        region_out[reg] = {"n": d["n"], "correct": d["correct"],
                           "accuracy": round(d["correct"] / d["n"], 4),
                           "ci95": [round(lo, 4), round(hi, 4)]}

    station_out = {st: {"n": d["n"], "correct": d["correct"],
                        "accuracy": round(d["correct"] / d["n"], 4),
                        "distinct_dates": len(dates_per_station[st])}
                   for st, d in by_station.items()}

    all_dates = sorted({r["settlement_date"] for r in cohort})

    result = {
        "experiment_id": args.experiment_id,
        "generated_for": "look-200 (provisional; N<200)",
        "prereg": {
            "artifact_sha256": pre["sha256"],
            "frozen_at_utc": art["frozen_at_utc"],
            "repo_git_sha": art["repo_git_sha"],
            "analysis_code_combined_match": code_ok,
            "analysis_code_modules_match": module_ok,
            "analysis_code_live_combined": live_code["combined_sha256"],
        },
        "cohort": {
            "n_usable": n,
            "correct": correct,
            "accuracy": round(acc, 4),
            "accuracy_ci95_wilson": [round(acc_lo, 4), round(acc_hi, 4)],
            "axis_crosscheck_agree": f"{axis_agree}/{axis_checked}",
            "n_distinct_settlement_dates": len(all_dates),
            "prereg_N_threshold": art["cohort"]["N"],
            "primary_min_trades": art["primary_metric"]["thresholds"]["min_trades"],
            "is_provisional": n < art["cohort"]["N"],
            "below_hurdle_min_events": n < MIN_N_LOOK,
        },
        "pnl": {
            "gross_total_usd": round(gross_total, 4),
            "fee_total_usd": round(fee_total, 4),
            "net_total_usd": round(gross_total - fee_total, 4),
            "mean_net_per_trade_usd": round(nd_mean, 6),
            "std_net_per_trade_usd": round(nd_std, 6),
            "sharpe_per_trade_usd_NOT_bankroll_compounded": round(nd_sharpe, 4),
            "mean_net_per_contract_usd": round(pc_mean, 6),
            "std_net_per_contract_usd": round(pc_std, 6),
            "sharpe_per_contract": round(pc_sharpe, 4),
            "block_bootstrap_ci95_mean_net_per_trade_usd": [round(boot_lo, 6), round(boot_hi, 6)],
            "block_bootstrap_ci95_mean_net_per_contract_usd": [round(boot_pc_lo, 6), round(boot_pc_hi, 6)],
            "bootstrap_reps": BOOTSTRAP_REPS, "bootstrap_block_key": "settlement_date",
            "sensitivity_drop_non_yes_row": {
                "mean_net_per_trade_usd": round(nd_clean_mean, 6),
                "sharpe": round(nd_clean_sharpe, 4),
            },
        },
        "hurdle_a3": {
            "metric": "net_ev_per_accepted_trade",
            "events": n,
            "min_events": MIN_N_LOOK,
            "can_trigger_at_this_n": n >= MIN_N_LOOK,
            "upper_95_bound_net_ev_per_contract_usd": round(upper_95_bound, 6),
            "note": ("A3 needs >=200 events; at n=%d it cannot fire. Upper 95%% bound "
                     "reported for trajectory only." % n),
        },
        "canaries": can,
        "win_rate_by_direction": {d: {"n": v["n"], "correct": v["correct"],
                                      "accuracy": round(v["correct"] / v["n"], 4)}
                                  for d, v in by_dir.items()},
        "east_west_split": region_out,
        "by_station": station_out,
        "anomalies": anomalies,
    }

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"GR5-WE-001 LOOK-200 (PROVISIONAL, n={n} < 200)")
        print(f"prereg sha256 {pre['sha256'][:16]}…  analysis-code match={code_ok}")
        print(f"accuracy {acc:.3f} ({correct}/{n}) 95%CI [{acc_lo:.3f},{acc_hi:.3f}]  "
              f"axis-crosscheck {axis_agree}/{axis_checked}")
        print(f"gross ${gross_total:.2f}  fee ${fee_total:.2f}  net ${gross_total-fee_total:.2f}")
        print(f"mean net/trade ${nd_mean:.4f}  Sharpe(per-trade,usd) {nd_sharpe:.3f}  "
              f"CI95 [{boot_lo:.4f},{boot_hi:.4f}]")
        print(f"mean net/contract ${pc_mean:.4f}  Sharpe(per-contract) {pc_sharpe:.3f}  "
              f"upper95 net-EV ${upper_95_bound:.4f}")
        print(f"canaries: halt={can['halt']} provisional={len(can['provisional'])}")
        for cid, r in can["canaries"].items():
            flag = "REJECTED" if r["rejected"] else ("SIG" if r["significant"] else "null")
            print(f"  {cid}: n={r['n']} mean={r['mean']} z={r['z']} p={r['p_value']} "
                  f"cusum={r['cusum']} [{flag}]")
        print(f"E/W: {region_out}")
        for a in anomalies:
            print(f"ANOMALY: {a}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
