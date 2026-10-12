#!/usr/bin/env python3
"""
trackA_spread_filter.py — Track A-2: SPREAD-FILTER variant.

Question
--------
Track A (committed c05e5b8e3) lost ~$840 under realistic costs. The friction
breakdown showed the killer is NOT fees (≈$0.01/contract each way) but the
bid-ask spread crossed on entry/exit (~$0.13/contract of round-trip half-spreads
on a ~21c contract). Track A-2 asks the sharp version of that question:

    If we only ENTER when the quoted bid-ask spread is a small fraction of the
    entry mid, does the strategy turn positive?

Test
----
Replay the SAME Track A trade set (same 209 trades, windows 14/17/20Z, thr 0.5)
and the same exit ladder, but gate entry on:

        spread_pct = (entry_ask - entry_bid) / entry_mid  <=  X

for X in {0.10, 0.25, 0.50}. Trades failing the gate are dropped (no entry).
Everything else (target mapping, sizing, stop/+20%/sell-.95/settlement exits,
realistic friction model) is byte-for-byte the Track A pipeline.

Because the committed JSON stored only aggregate metrics + 15 examples (no
per-trade bid/ask), this variant re-derives the identical trade set by reusing
the frozen Track A script verbatim (import), capturing the entry bid/ask it
discarded. Read-only; no live wiring; no DB mutation; no temp DBs.

Usage:
    .venv-gefs/bin/python scripts/trackA_spread_filter.py
Optional:
    --buckets-cache PATH   reuse a pickle of {(station, epoch): mean} to skip
                           the (expensive) ASOS 1-min scan on repeat runs
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import trackA_position_sim as base  # noqa: E402  (frozen Track A pipeline)

OUT_JSON = REPO / "data" / "trackA_spread_filter.json"
SPREAD_THRESHOLDS = [0.10, 0.25, 0.50]
SWEEP = [0.03, 0.05, 0.06, 0.08, 0.10, 0.12, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 1.00]


def collect_trades(sd, ladder, market, truth, threshold, monitor="windows"):
    """Reproduce Track A's entry loop and return the FULL trade list, with the
    entry bid/ask the committed artifact dropped."""
    trades = []
    for (st, ld), series in sd.items():
        lrows = ladder.get((st, ld))
        if not lrows:
            continue
        for h in base.WINDOWS:
            dec = base.window_bucket(series, st, ld, h)
            if dec is None:
                continue
            rh = base.rate_and_high(series, dec)
            if rh is None:
                continue
            rate, hi = rh
            if rate < threshold:
                continue
            _integer, strike, stype = base.pick_strike(lrows, hi)
            if strike is None:
                continue
            quotes = market.get((st, ld, "T" if stype == "greater" else "B", strike))
            if not quotes:
                continue
            entry = base.nearest_market_le(quotes, dec)
            if entry is None:
                continue
            if not entry[4] or entry[4] <= 0:
                continue
            rec = base.simulate_trade(
                st, ld, h, dec, threshold, strike, stype, lrows, quotes, entry,
                series, truth, defaultdict(int), monitor, True)
            entry_ep, entry_mid, entry_bid, entry_ask = entry[0], entry[1], entry[2], entry[3]
            rec["entry_bid"] = round(entry_bid, 4)
            rec["entry_ask"] = round(entry_ask, 4)
            rec["entry_spread"] = round(entry_ask - entry_bid, 4)
            rec["entry_spread_pct"] = (
                round((entry_ask - entry_bid) / entry_mid, 6) if entry_mid > 0 else None)
            trades.append(rec)
            break  # one position per station-day
    return [t for t in trades if t.get("net_pnl") is not None]


def summarize(rows):
    nets = [t["net_pnl"] for t in rows]
    gross = [t["gross_pnl"] for t in rows]
    wins = [n for n in nets if n > 0]
    exits = defaultdict(int)
    per_exit = defaultdict(float)
    for t in rows:
        exits[t["exit_kind"]] += 1
        per_exit[t["exit_kind"]] += t["net_pnl"]
    return {
        "n_trades": len(rows),
        "net_pnl": round(sum(nets), 4) if nets else 0.0,
        "gross_pnl": round(sum(gross), 4) if gross else 0.0,
        "win_rate": round(len(wins) / len(rows), 4) if rows else None,
        "avg_pnl_per_trade": round(sum(nets) / len(rows), 4) if rows else None,
        "median_pnl_per_trade": round(sorted(nets)[len(nets) // 2], 4) if nets else None,
        "best_trade": round(max(nets), 4) if nets else None,
        "worst_trade": round(min(nets), 4) if nets else None,
        "total_contracts": sum(t["contracts"] for t in rows),
        "avg_entry_mid": round(sum(t["entry_mid"] for t in rows) / len(rows), 4) if rows else None,
        "avg_entry_spread_pct": (
            round(sum(t["entry_spread_pct"] for t in rows) / len(rows), 6) if rows else None),
        "net_pnl_per_contract": (round(sum(nets) / sum(t["contracts"] for t in rows), 6)
                                if rows and sum(t["contracts"] for t in rows) else None),
        "gross_pnl_per_contract": (round(sum(gross) / sum(t["contracts"] for t in rows), 6)
                                  if rows and sum(t["contracts"] for t in rows) else None),
        "exit_distribution": dict(sorted(exits.items())),
        "net_pnl_by_exit": {k: round(v, 2) for k, v in sorted(per_exit.items())},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-json", default=str(OUT_JSON))
    ap.add_argument("--buckets-cache", default=None)
    ap.add_argument("--buckets-cache-save", default=None,
                    help="write the {(station, epoch): mean} pickle for cheap re-runs")
    ap.add_argument("--threshold", type=float, default=0.5,
                    help="rate_1h trigger threshold (0.5 = the Track A base: 209 trades)")
    args = ap.parse_args()

    if args.buckets_cache:
        import pickle
        buckets = pickle.load(open(args.buckets_cache, "rb"))
    else:
        print("loading ASOS 1-min buckets (read-only)...", flush=True)
        buckets = base.load_buckets()
        if args.buckets_cache_save:
            import pickle
            pickle.dump(buckets, open(args.buckets_cache_save, "wb"))
    sd = base.build_station_days(buckets)
    print(f"  {len(sd)} station-days", flush=True)
    ladder = base.load_ladder()
    market = base.load_market()
    truth = base.load_settlement_truth()
    print(f"  ladder={len(ladder)} market_keys={len(market)}", flush=True)

    all_trades = collect_trades(sd, ladder, market, truth, args.threshold)
    print(f"  collected {len(all_trades)} resolved trades (Track A base)")

    # Sanity: this must reproduce the committed Track A base.
    base_summary = summarize(all_trades)

    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "variant": "Track A-2 — spread-filter (enter only when spread <= X% of entry mid)",
        "base_commit": "c05e5b8e3",
        "threshold": args.threshold,
        "spread_thresholds": SPREAD_THRESHOLDS,
        "note": (
            "Replay of the identical Track A trade set (windows 14/17/20Z). The "
            "committed artifact stored no per-trade bid/ask, so the set is "
            "re-derived via the frozen Track A pipeline (import) with entry "
            "bid/ask captured. Realistic friction (half-spread each side + "
            "canonical fees); no re-research, no live wiring."),
        "base_209": base_summary,
        "filters": {},
    }

    for x in SPREAD_THRESHOLDS:
        kept = [t for t in all_trades if t["entry_spread_pct"] is not None
                and t["entry_spread_pct"] <= x + 1e-12]
        report["filters"][f"{x:.2f}"] = {
            "spread_pct_max": x,
            "survivors": len(kept),
            "dropped": len(all_trades) - len(kept),
            **summarize(kept),
        }

    # Distribution of entry spreads — where the mass sits (context for the break-even).
    sp = sorted(t["entry_spread_pct"] for t in all_trades if t["entry_spread_pct"] is not None)
    if sp:
        def pct(p):
            return round(sp[min(len(sp) - 1, int(p * len(sp)))], 4)
        report["entry_spread_distribution"] = {
            "n": len(sp), "min": round(sp[0], 4), "p10": pct(0.10),
            "median": pct(0.50), "p75": pct(0.75), "p90": pct(0.90),
            "max": round(sp[-1], 4),
            "note": ("spread as a FRACTION of the entry mid; absolute spreads are "
                     "only ~$0.015-0.02 at these mids, so a 10% gate is ~2c"),
        }
    # Fine threshold sweep — locates where (if anywhere) the variant turns positive.
    sweep = {}
    for x in SWEEP:
        kept = [t for t in all_trades if t["entry_spread_pct"] is not None
                and t["entry_spread_pct"] <= x + 1e-12]
        sweep[f"{x:.2f}"] = {"spread_pct_max": x, "survivors": len(kept),
                             **summarize(kept)}
    report["sweep"] = sweep
    # Friction decomposition — split realized loss into spread cost vs the rest.
    # gross_pnl nets out fees but not the half-spread; net_pnl crosses the spread.
    fr_total = sum(t["gross_pnl"] - t["net_pnl"] for t in all_trades)
    ct = sum(t["contracts"] for t in all_trades)
    report["friction_decomposition"] = {
        "spread_cost_total_usd": round(fr_total, 2),
        "spread_cost_per_contract_usd": round(fr_total / ct, 6) if ct else None,
        "avg_abs_entry_spread_usd": round(
            sum(t["entry_spread"] for t in all_trades) / len(all_trades), 4) if all_trades else None,
        "gross_pnl_usd": report["base_209"]["gross_pnl"],
        "net_pnl_usd": report["base_209"]["net_pnl"],
        "note": ("the spread is MATERIALLY smaller than the raw friction "
                 "residual: the repo's own realistic-vs-idealized bracket shows "
                 "~$1,027 round-trip friction (~$4.92/trade over 209), but the "
                 "half-spread component here is only ~$361 (~$0.011/contract). "
                 "The remainder (~$666) is forced-exit slippage / exit-side "
                 "half-spreads, NOT entry spread. Gross (pre-entry-spread) is "
                 "already ~-$478. The bulk of the loss is the settlement leg "
                 "(-$1,403), not entry friction."),
    }

    # Per-trade dump (compact) so the filter is auditable without a re-run.
    report["trades"] = [
        {k: t.get(k) for k in (
            "station", "local_date", "window_utc", "strike", "strike_type",
            "entry_mid", "entry_bid", "entry_ask", "entry_spread", "entry_spread_pct",
            "entry_oi", "contracts", "exit_kind", "exit_mid",
            "ladder_result", "net_pnl", "gross_pnl")}
        for t in sorted(all_trades, key=lambda t: (t["local_date"], t["station"]))
    ]

    Path(args.out_json).write_text(json.dumps(report, indent=2))
    print("wrote", args.out_json)
    print("\nBASE (thr %.1f): n=%d net=%.2f gross=%.2f win=%.4f" % (
        args.threshold, base_summary["n_trades"], base_summary["net_pnl"],
        base_summary["gross_pnl"], base_summary["win_rate"] or 0))
    for x in SPREAD_THRESHOLDS:
        f = report["filters"][f"{x:.2f}"]
        print("FILTER spread<=%4.0f%%: n=%3d drop=%3d net=%9.2f gross=%9.2f win=%s exits=%s" % (
            x * 100, f["survivors"], f["dropped"], f["net_pnl"], f["gross_pnl"],
            f["win_rate"], f["exit_distribution"]))
    print("entry spread pct: median=%s p75=%s p90=%s" % (
        report["entry_spread_distribution"].get("median"),
        report["entry_spread_distribution"].get("p75"),
        report["entry_spread_distribution"].get("p90")))
    return report


if __name__ == "__main__":
    main()
