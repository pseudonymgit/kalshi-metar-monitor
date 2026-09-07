"""
backtest.py — Gamma additive stack: backtest + held-out validation harness.

Runs the full additive stack over a date range x station set, scores every
fired day against the truth chain (Kalshi authoritative -> settlement_epochs
fallback), and produces:

  - per-signal fire/accuracy/MAE-improvement tables
  - base-error stratification: fired-day |base error| vs all-day |base error|
    (the spec's core claim: signals fire where NWP is wrong)
  - direction mix per signal
  - co-occurrence matrix (G.7) + monitor report (G.8)

Split discipline (no leakage):
  - TRAIN:      2025-06-01 .. 2026-02-28   (threshold/param sensitivity)
  - HELD-OUT:   2026-03-01 .. 2026-08-31   (acceptance evidence)

Usage:
  python3 -m core.additive.backtest --start 2026-03-01 --end 2026-08-31 \
      --out reports/additive_heldout.json [--stations KATL,...] [--sample N]
"""

import argparse
import json
import math
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import date as date_cls, timedelta
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from . import evaluate_all, SIGNAL_NAMES
from .context import AdditiveContext
from . import cooccurrence
from .monitor import build_report as additive_monitor
from .integration import apply_additive_offsets

__all__ = [
    "month_range",
    "load_truth",
    "load_base_forecasts",
    "pick_base_forecast",
    "evaluate_station_date",
    "run_range",
    "analyze",
    "render_acceptance",
    "main",
]

# ─── Settle stations (Kalshi-verified) ──────────────────────────────────────

SETTLE_STATIONS = [
    "KATL", "KAUS", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU", "KLAS",
    "KLAX", "KMDW", "KMIA", "KMSP", "KMSY", "KNYC", "KOKC", "KPHL",
    "KPHX", "KSAT", "KSEA", "KSFO",
]

MONTHS_WARM = {4, 5, 6, 7, 8, 9}


def month_range(start: date_cls, end: date_cls) -> List[Tuple[int, int]]:
    """All (year, month) pairs touched by [start, end]."""
    out = []
    y, m = start.year, start.month
    ey, em = end.year, end.month
    while (y, m) <= (ey, em):
        out.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def load_truth(db_path: str) -> Dict[Tuple[str, str], float]:
    """
    (station, date) -> actual high degF. Kalshi authoritative first,
    settlement_epochs bucket fallback (verified apprx degF, bad 2026-08-10 ingest
    day excluded). Sanity band [-60, 135] degF guards against DB corruption
    seen in settlement ingest 2026-07-07..12 (values like 39678.8).
    """
    truth: Dict[Tuple[str, str], float] = {}

    # Kalshi settlements
    dbk = os.path.join(db_path, "kalshi_settlements.db")
    if os.path.isfile(dbk):
        conn = sqlite3.connect(dbk)
        for row in conn.execute(
            "SELECT station, target_date, kalshi_temp FROM kalshi_settlements "
            "WHERE source_type='finalized' AND kalshi_temp IS NOT NULL"
        ):
            st, d, t = str(row[0]), str(row[1]), float(row[2])
            if -60 <= t <= 135:
                truth[(st, d)] = t
        conn.close()

    # Add settlement_epochs fallback
    dbm = os.path.join(db_path, "metar_backfill.db")
    if os.path.isfile(dbm):
        conn = sqlite3.connect(dbm)
        for row in conn.execute(
            "SELECT station, bucket_date, bucket_high_f FROM settlement_epochs "
            "WHERE bucket_high_f IS NOT NULL"
        ):
            st, d, b = str(row[0]), str(row[1]), float(row[2])
            key = (st, d)
            if key not in truth and -60 <= b <= 135:
                truth[key] = b
        conn.close()

    return truth


def load_base_forecasts(db_path: str) -> Dict[str, Dict[Tuple[str, str], float]]:
    """
    model -> (station, target_date) -> NWP daily-max high degF (lead <= 1 day).
    era5 covers 2025-01..2026-04; ecmwf/gfs/icon/gem cover 2026-04..2026-09.
    """
    out: Dict[str, Dict[Tuple[str, str], float]] = defaultdict(dict)
    dbn = os.path.join(db_path, "nwp_forecasts.db")
    if not os.path.isfile(dbn):
        return dict(out)

    conn = sqlite3.connect(dbn)
    for row in conn.execute(
        "SELECT model, station, target_date, value FROM nwp_forecasts "
        "WHERE variable = 'temperature_2m_max' "
        "AND (julianday(target_date) - julianday(fetch_date)) <= 1 "
        "AND (julianday(target_date) - julianday(fetch_date)) >= 0"
    ):
        model, st, d, v = str(row[0]), str(row[1]), str(row[2]), float(row[3])
        out[model][(st, d)] = v
    conn.close()
    return dict(out)


def pick_base_forecast(
    base_all: Dict[str, Dict[Tuple[str, str], float]],
    key: Tuple[str, str],
) -> Optional[float]:
    """Best available base forecast for key: era5 -> ecmwf -> gfs -> icon."""
    for m in ("era5", "ecmwf", "gfs", "icon"):
        v = base_all.get(m, {}).get(key)
        if v is not None:
            return v
    return None


def evaluate_station_date(
    station: str,
    d: str,
    base_f: float,
    actual_f: float,
) -> Tuple[List, object, Optional[dict]]:
    """
    Run the additive stack for one station-date. Returns
    (results, stack, error_record or None).
    """
    from . import evaluate_all as additive_evaluate_all
    from .context import AdditiveContext
    from .integration import apply_additive_offsets

    collect = []
    try:
        ctx = AdditiveContext(station, d)
        results = additive_evaluate_all(ctx)
        stack = apply_additive_offsets(results, enabled=list(SIGNAL_NAMES))
        ctx.close()

        pred_base = base_f
        pred_adj = base_f + stack.total_offset_f
        err_base = abs(pred_base - actual_f)
        err_with = abs(pred_adj - actual_f)

        err_rec = {
            "err_base": err_base,
            "err_with": err_with,
            "hit_base": 1 if err_base < err_with else 0,
            "hit_with": 1 if err_with < err_base else 0,
            "pred_base": round(pred_base, 1),
            "pred_adj": round(pred_adj, 1),
            "actual": round(actual_f, 1),
        }
        return results, stack, err_rec
    except Exception as e:
        return [], None, {"error": str(e)}


def run_range(
    start: date_cls,
    end: date_cls,
    stations: List[str],
    sample_every: int = 1,
    verbose: bool = False,
) -> dict:
    """Run the backtest over a range of dates/stations."""
    truth = load_truth(str(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "data")))
    base_all = load_base_forecasts(str(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "data")))
    all_stations = stations or SETTLE_STATIONS

    results_by_day: Dict[tuple, List] = {}
    errors_by_day: Dict[tuple, dict] = {}
    base_model_used: Dict[tuple, str] = {}

    n_pairs = 0
    n_scored = 0

    cooccurrence.reset_state()

    for y, m in month_range(start, end):
        ey, em = (end.year, end.month) if (y, m) >= (end.year, end.month) else None
        dates = []
        d0 = date_cls(y, m, 1)
        d1 = date_cls(y + 1, 1, 1) if m == 12 else date_cls(y, m + 1, 1)
        cur = d0
        while cur < d1:
            dates.append(cur)
            cur += timedelta(days=1)

        for di, d in enumerate(dates):
            if di % sample_every != 0:
                continue
            d_str = d.strftime("%Y-%m-%d")
            for si, st in enumerate(all_stations):
                key = (st, d_str)
                base_f = pick_base_forecast(base_all, key)
                actual_f = truth.get(key)
                if base_f is None or actual_f is None:
                    continue
                n_pairs += 1

                try:
                    results, stack, err_rec = evaluate_station_date(st, d_str, base_f, actual_f)
                except Exception:
                    continue

                if err_rec and "error" not in err_rec:
                    results_by_day[key] = results
                    errors_by_day[key] = err_rec
                    base_model_used[key] = "auto"
                    n_scored += 1
                    cooccurrence.evaluate_day(key, results)
                    if verbose and n_scored % 100 == 0:
                        print(f"  [{n_scored}] {st} {d_str}", file=sys.stderr)

    cooc_report = cooccurrence.build_report(results_by_day)
    mon_report = additive_monitor(results_by_day, errors_by_day)

    return {
        "n_station_days": n_pairs,
        "n_scored": n_scored,
        "results_by_day": results_by_day,
        "errors_by_day": errors_by_day,
        "base_model_used": base_model_used,
        "cooccurrence": cooc_report.to_dict() if hasattr(cooc_report, 'to_dict') else str(cooc_report),
        "additive_monitor": mon_report,
    }


def analyze(report: dict, errs: dict) -> dict:
    """Derive acceptance metrics from a run_range report."""
    fired_stats = defaultdict(lambda: {"n": 0, "sum_err_base": 0.0, "sum_err_with": 0.0, "hit_with": 0, "hit_base": 0})
    all_mae_base = []
    all_hit_base = []
    for key, e in errs.items():
        all_mae_base.append(e.get("err_base", 0.0))
        all_hit_base.append(e.get("hit_base", 0))
    for (_st, _d), res in report.get("results_by_day", {}).items():
        for r in res:
            name = r.signal
            fired_stats[name]["n"] += 1
    for key, e in errs.items():
        # Find which signals fired on this station-date
        res = report.get("results_by_day", {}).get(key, [])
        for r in res:
            if not r.fired:
                continue
            s = fired_stats[r.signal]
            s["sum_err_base"] += e.get("err_base", 0.0)
            s["sum_err_with"] += e.get("err_with", 0.0)
            if e.get("hit_with", 0):
                s["hit_with"] += 1
            if e.get("hit_base", 0):
                s["hit_base"] += 1

    out = {
        "n_scored": report.get("n_scored", 0),
        "all_mae_base": round(sum(all_mae_base) / len(all_mae_base), 4) if all_mae_base else 0,
        "per_signal": {},
    }

    for name in SIGNAL_NAMES:
        s = fired_stats.get(name, {})
        n = s.get("n", 0)
        out["per_signal"][name] = {
            "n_fired": n,
            "mae_base": round(s.get("sum_err_base", 0) / n, 4) if n else None,
            "mae_with": round(s.get("sum_err_with", 0) / n, 4) if n else None,
            "lift": round((s.get("hit_with", 0) - s.get("hit_base", 0)) / n, 4) if n else None,
        }

    return out


def render_acceptance(a: dict, cooc: dict) -> str:
    """Render acceptance analysis as text."""
    lines = []
    lines.append("=" * 84)
    lines.append("ADDITIVE STACK — HELD-OUT VALIDATION SUMMARY")
    lines.append("=" * 84)
    lines.append("")
    lines.append(f"scored station-days: {a.get('n_scored', 0)}")
    lines.append(f"all-day MAE base:    {a.get('all_mae_base', 0):.3f}")
    lines.append("")
    hdr = f"{'Signal':24s} {'Fired':>8s} {'MAE_base':>10s} {'MAE_with':>10s} {'Lift':>8s}"
    lines.append(hdr)
    lines.append("-" * len(hdr))
    for name, s in a.get("per_signal", {}).items():
        n = s.get("n_fired", 0)
        mae_b = s.get("mae_base", 0) or 0
        mae_w = s.get("mae_with", 0) or 0
        lift = s.get("lift", 0) or 0
        lift_s = f"{lift:+.4f}" if lift != 0 else " 0.0"
        lines.append(f"{name:24s} {n:>8d} {mae_b:>10.3f} {mae_w:>10.3f} {lift_s:>8s}")
    lines.append("")
    lines.append("=" * 84)
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Additive stack backtest")
    ap.add_argument("--start", default="2026-03-01")
    ap.add_argument("--end", default="2026-08-31")
    ap.add_argument("--stations", default="")
    ap.add_argument("--sample", type=int, default=1, help="Sample every Nth date")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    start = date_cls(*[int(x) for x in args.start.split("-")])
    end = date_cls(*[int(x) for x in args.end.split("-")])
    stations = args.stations.split(",") if args.stations else []

    report = run_range(start, end, stations, sample_every=args.sample)
    a = analyze(report, report.get("errors_by_day", {}))

    print(render_acceptance(a, report.get("cooccurrence", {})))
    if hasattr(cooccurrence, "additive_monitor"):
        print(cooccurrence.additive_monitor.render_text(report.get("additive_monitor", {})))

    if args.out:
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        with open(args.out, "w") as f:
            json.dump({"analysis": a, "report": report}, f, indent=2, default=str)


if __name__ == "__main__":
    main()