#!/usr/bin/env python3
"""
B-Mode P1 — Directional Calibration Targeted Sweep

Runs the calibrated backtest baseline plus 4 targeted config variations and
produces a comparison table + JSON results file.

Configs:
  1. baseline         — calibrated only, no other changes
  2. edge-0.01        — edge_threshold = 0.01 (lower gate, more trades)
  3. edge-0.03        — edge_threshold = 0.03 (stricter gate, fewer trades)
  4. kelly-0.75       — kelly_fraction = 0.75 (larger sizing on honest confidence)
  5. station-sizing   — losers -50% (KLAS/KMIA/KPHL), top-4 winners +20% (KNYC/KLAX/KDCA/KDFW)

Output:
  docs/weather-engine/backtests/bmode_p1_directional_sweep_<stamp>.json

Usage:
  python3 scripts/bmode_p1_directional_sweep.py [--days 365] [--start 2025-08-03]
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bmode_p1_backtest import (
    BacktestConfig,
    run_backtest,
    STATIONS,
)

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
OUTPUT_DIR = REPO_ROOT / "docs" / "weather-engine" / "backtests"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Top-4 winners and losers from the calibrated baseline run (2025-08-03 → 2026-08-02)
WINNERS = ["KNYC", "KLAX", "KDCA", "KDFW"]   # +20% sizing
LOSERS = ["KLAS", "KMIA", "KPHL"]            # -50% sizing


def main():
    parser = argparse.ArgumentParser(description="Directional calibration targeted sweep")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--start", type=str, default="2025-08-03")
    args = parser.parse_args()

    station_sizing = {s: 1.2 for s in WINNERS}
    station_sizing.update({s: 0.5 for s in LOSERS})

    configs = [
        BacktestConfig(tag="baseline", use_calibration=True),
        BacktestConfig(tag="edge-0.01", edge_threshold=0.01, use_calibration=True),
        BacktestConfig(tag="edge-0.03", edge_threshold=0.03, use_calibration=True),
        BacktestConfig(tag="kelly-0.75", kelly_fraction=0.75, use_calibration=True),
        BacktestConfig(tag="station-sizing", use_calibration=True, station_sizing=station_sizing),
    ]

    config_tags = [c.tag for c in configs]

    results = {}
    summary_rows = []
    for cfg in configs:
        print(f"\n{'=' * 72}\n  RUNNING: {cfg.tag}\n{'=' * 72}")
        result = run_backtest(cfg, args.start, args.days)
        results[cfg.tag] = result

        r = result["results"]
        summary_rows.append({
            "config": cfg.tag,
            "trades": r["trades"],
            "accuracy": r["accuracy"],
            "total_pnl": r["total_pnl"],
            "daily_sharpe": r["daily_sharpe"],
            "max_drawdown_pct": r["max_drawdown_pct"],
            "profit_factor": r["profit_factor"],
        })

    # ── Summary table ──
    print(f"\n\n{'=' * 92}")
    print("  DIRECTIONAL CALIBRATION SWEEP — SUMMARY")
    print(f"{'=' * 92}")
    print(f"  Period: {args.start} → {(datetime.strptime(args.start, '%Y-%m-%d') + timedelta(days=args.days - 1)).strftime('%Y-%m-%d')} ({args.days} days)")
    print(f"  {'Config':<16} {'Trades':>7} {'Acc%':>7} {'P&L':>12} {'Sharpe':>8} {'MaxDD%':>7} {'PF':>6}")
    print(f"  {'-' * 78}")

    best_sharpe = None
    best_pnl = None
    for row in summary_rows:
        print(f"  {row['config']:<16} {row['trades']:>7} {row['accuracy']*100:>6.1f}% "
              f"${row['total_pnl']:>+11.2f} {row['daily_sharpe']:>8.2f} {row['max_drawdown_pct']:>6.2f}% {row['profit_factor']:>6.2f}")
        if best_sharpe is None or row["daily_sharpe"] > best_sharpe["daily_sharpe"]:
            best_sharpe = row
        if best_pnl is None or row["total_pnl"] > best_pnl["total_pnl"]:
            best_pnl = row

    print(f"  {'-' * 78}")
    print(f"  BEST SHARPE: {best_sharpe['config']} (Sharpe {best_sharpe['daily_sharpe']:.2f}, "
          f"acc {best_sharpe['accuracy']*100:.1f}%, P&L ${best_sharpe['total_pnl']:+.2f})")
    print(f"  BEST P&L:    {best_pnl['config']} (P&L ${best_pnl['total_pnl']:+.2f}, "
          f"Sharpe {best_pnl['daily_sharpe']:.2f}, acc {best_pnl['accuracy']*100:.1f}%)")

    # ── Per-station breakdown for each config ──
    print(f"\n\n  PER-STATION P&L COMPARISON (losers & winners of interest):")
    print(f"  {'Station':<7}" + "".join(f"{t:>14}" for t in config_tags))
    print(f"  {'-' * (7 + 14 * len(config_tags))}")
    interest = sorted(set(LOSERS + WINNERS + ["KMSP", "KMDW", "KSFO", "KATL", "KDEN"]))
    for st in interest:
        row = f"  {st:<7}"
        for tag in config_tags:
            ps = results[tag]["per_station"].get(st, {})
            row += f"${ps.get('pnl', 0):>+12.0f} "
        print(row)

    # ── Save JSON ──
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = OUTPUT_DIR / f"bmode_p1_directional_sweep_{stamp}.json"
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "period": {"start": args.start, "days": args.days},
        "configs": config_tags,
        "summary": summary_rows,
        "best_sharpe": best_sharpe["config"],
        "best_pnl": best_pnl["config"],
        "per_config": {k: {"results": v["results"], "per_station": v["per_station"]}
                       for k, v in results.items()},
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"\n  Sweep results → {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())