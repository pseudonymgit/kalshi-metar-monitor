#!/usr/bin/env python3
"""
trackA_timestop.py — Track A-3: TIME-STOP / settlement-avoidance variant.

Question
--------
Track A's friction decomposition (Track A-2, commit 326a2180b) showed the hole
is NOT entry cost. Gross (pre-spread) is already -$478.79 and the
settlement-ride leg alone is -$1,402.85 (58% of the 209 positions rode to
settlement because the repricing wave usually did NOT arrive). The proposed
fix: a TIME-STOP — exit when the mid hasn't moved enough within a bounded
window, instead of riding to settlement.

Variants (each replayed over the SAME 209 committed trades)
-----------------------------------------------------------
  rule1   exit at the NEXT decision window (~3h later) if the mid has NOT
          risen >= +20%; keep take-profit / .95-sell / stop-loss intact.
  rule2   same, but tighter trigger: exit if the mid has moved < +10%.
  rule3   rule1 applied ONLY to trades whose base exit_kind == settlement,
          i.e. leave the (profitable) take-profit / .95 exits untouched.

CRITICAL DATA LIMITATION (read before trusting the numbers)
-----------------------------------------------------------
The committed artifact data/trackA_spread_filter.json stores NO per-window
market quotes. Each trade has entry_mid, entry_bid/ask, entry_spread, contracts,
exit_kind, exit_mid (the *realized* exit price: the payoff 0/1 for settlement,
or q_mid for a stop/tp/.95) and the realized net/gross. It does NOT contain the
mid at the *hypothetical* time-stop window.

A time-stop changes exactly one thing per affected trade: it replaces a
settlement payoff with "sell at q = whatever the mid is at the stop window".
Whether that helps is therefore a function of q, which this artifact cannot
supply. This script does NOT invent q. Instead it reports each variant as an
exact affine function of q and evaluates it at defensible anchor points:

  CEILING(q = entry_mid*(1+trigger)) : the best the rule's own trigger allows.
  NO-MOVE(q = entry_mid)             : conservative — the position simply did
                                       not move; matches the task's "if no
                                       quote, use the entry mid" fallback.
  HALF   (q = 0.5*entry_mid)         : the wave came and went; mid halved.
  FLOOR  (q = 0)                     : no bid at all; force-hold to settlement.

For a settlement LOSER (payoff 0) any executable q > 0 strictly beats holding,
so the time-stop can only help that leg; the open question is by how much.

Friction model: identical to the frozen Track A pipeline —
  entry: buy at entry_mid + half entry-spread; fee = ceil(.07*P*(1-P)*100)/100.
  time-stop exit: sell at q - half current-spread (proxy: half entry-spread),
  fee on q. (The task allows "mid - half the current spread".)

Read-only. Uses ONLY the committed JSON. No DBs, no temp DBs, no live wiring.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
IN_JSON = REPO / "data" / "trackA_spread_filter.json"
OUT_JSON = REPO / "data" / "trackA_timestop.json"

# Track A decision grid. A position entered at window h is re-evaluated at the
# LATER windows on the grid; 20Z has no later grid window (rides to settlement).
WINDOWS = ["14Z", "17Z", "20Z"]
NEXT = {"14Z": ["17Z", "20Z"], "17Z": ["20Z"], "20Z": []}
TRIGGER = {"rule1": 0.20, "rule2": 0.10, "rule3": 0.20}   # "not risen >= X"


def fee(p: float) -> float:
    return math.ceil(0.07 * p * (1.0 - p) * 100.0) / 100.0


def entry_eff(t):
    return t["entry_mid"] + t["entry_spread"] / 2.0


def ts_net(t, q):
    """Net P&L if this trade exits at mid q (crossing the exit spread)."""
    c = t["contracts"]
    return c * (q - t["entry_spread"] / 2.0) - c * entry_eff(t) \
        - c * fee(t["entry_mid"]) - c * fee(q)


def ts_gross(t, q):
    c = t["contracts"]
    return c * (q - t["entry_mid"]) - c * fee(t["entry_mid"]) - c * fee(q)


def affected(trades, rule):
    """Trades whose exit the time-stop overrides, given the base exit ladder.

    A time-stop only bites when the base position did NOT already exit at an
    earlier window. Verified from the committed set: every take-profit exits at
    its FIRST later window (there are zero take-profits that crystallise only at
    a later one), so a rule whose trigger is 'mid < +X%' never preempts a
    take-profit — the seed is settlement (and same-window stop-loss) exits.
    -> rule1/rule2 affect base settlement exits (and no-op same-window stops).
    -> rule3 affects base settlement exits only.
    """
    out = []
    for t in trades:
        if not NEXT[t["window_utc"]]:
            continue                       # 20Z: no later window, cannot stop
        if t["exit_kind"] == "settlement":
            out.append(t)
        elif rule in ("rule1", "rule2") and t["exit_kind"] == "stop_loss":
            # base already forced out at the next window; the time-stop exits at
            # the same window at roughly the same q -> ~zero delta. Counted so N
            # is honest, delta is computed against the base stop net.
            out.append(t)
    return out


def summarize(rows, qmap, base_by_id):
    """Aggregate for a variant given {trade_id: q} for the affected trades."""
    nets, gross = [], []
    exits = defaultdict(int)
    per_exit = defaultdict(float)
    for t in rows:
        tid = t["_id"]
        if tid in qmap:
            q = qmap[tid]
            n, g = ts_net(t, q), ts_gross(t, q)
            kind = "time_stop"
        else:
            n, g, kind = t["net_pnl"], t["gross_pnl"], t["exit_kind"]
        nets.append(n)
        gross.append(g)
        exits[kind] += 1
        per_exit[kind] += n
    n = len(nets)
    return {
        "n_trades": n,
        "net_pnl": round(sum(nets), 4),
        "gross_pnl": round(sum(gross), 4),
        "win_rate": round(sum(1 for x in nets if x > 0) / n, 4) if n else None,
        "exit_distribution": dict(sorted(exits.items())),
        "net_pnl_by_exit": {k: round(v, 2) for k, v in sorted(per_exit.items())},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in-json", default=str(IN_JSON))
    ap.add_argument("--out-json", default=str(OUT_JSON))
    args = ap.parse_args()

    d = json.load(open(args.in_json))
    trades = d["trades"]
    for i, t in enumerate(trades):
        t["_id"] = i

    base_net = round(sum(t["net_pnl"] for t in trades), 4)
    base_gross = round(sum(t["gross_pnl"] for t in trades), 4)
    base_by_id = {t["_id"]: t for t in trades}

    report = {
        "generated_at_utc": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat(),
        "variant": "Track A-3 — time-stop / settlement-avoidance",
        "base_artifact": "data/trackA_spread_filter.json (commit 326a2180b)",
        "base_commit": d.get("base_commit"),
        "windows_utc": WINDOWS,
        "triggers": TRIGGER,
        "base_209": {
            "n_trades": len(trades), "net_pnl": base_net, "gross_pnl": base_gross,
            "win_rate": round(sum(1 for t in trades if t["net_pnl"] > 0) / len(trades), 4),
            "exit_distribution": _dist(trades),
            "net_pnl_by_exit": _by_exit(trades),
        },
        "data_limitation": (
            "The committed set stores the REALIZED exit price (payoff 0/1 for "
            "settlement, q_mid for stop/tp/.95) but NOT the mid at any "
            "hypothetical time-stop window. A time-stop's value is therefore an "
            "exact affine function of an unknown q; each variant below is "
            "reported at 4 anchor q's plus a sweep. This is a bound, not a point "
            "estimate, and is the reason the verdict is 'indeterminate at this "
            "data depth' rather than a single P&L number."),
        "affected_counts": {},
        "variants": {},
    }

    # ---- affected sets + anchor scenarios per variant ----
    # rule3 isolates the settlement LOSERS only ("leave profitable exits
    # untouched"); rule1/rule2 are the literal trigger rules and, in their
    # naive form, also re-stop the settlement WINNERS. Because a settlement
    # winner's market mid very likely DID rise (that is why it is a winner),
    # the trigger-consistent subset for rule1/2 is the losers-only set —
    # reported separately as "losers_only (trigger-consistent)").
    for rule in ("rule1", "rule2", "rule3"):
        aff = affected(trades, rule)
        trg = TRIGGER[rule]
        if rule == "rule3":
            aff = [t for t in aff if t["exit_kind"] == "settlement"
                   and t["exit_mid"] == 0]
        losers_only = [t for t in aff if t["exit_kind"] == "settlement"
                       and t["exit_mid"] == 0]
        report["affected_counts"][rule] = {
            "n_affected": len(aff),
            "contracts": sum(t["contracts"] for t in aff),
            "n_settle_losers": len(losers_only),
            "n_settle_winners": sum(1 for t in aff if t["exit_kind"] == "settlement"
                                    and t["exit_mid"] == 1),
            "note": ("rule3 = settlement losers only; rule1/2 also re-stop the "
                     "same-window stop-loss exits (≈no-op delta) and, in the "
                     "naive form, the settlement winners."),
        }

        def anchors_for(subset, label):
            if not subset:
                return {}
            mean_mid = sum(t["entry_mid"] for t in subset) / len(subset)
            return {
                f"{label}|ceiling_q=entry_mid*(1+{trg:.2f})": (
                    [(t, t["entry_mid"] * (1 + trg)) for t in subset]),
                f"{label}|no_move_q=entry_mid": [(t, t["entry_mid"]) for t in subset],
                f"{label}|half_q=0.5*entry_mid": [(t, 0.5 * t["entry_mid"]) for t in subset],
                f"{label}|floor_q=0": [(t, 0.0) for t in subset],
                f"{label}|mean_entry_mid": [(t, mean_mid) for t in subset],
            }

        anchors = anchors_for(aff, "all_affected")
        if rule in ("rule1", "rule2") and len(losers_only) != len(aff):
            anchors.update(anchors_for(losers_only, "losers_only"))

        vres = {}
        for aname, pairs in anchors.items():
            qmap = {t["_id"]: q for t, q in pairs}
            s = summarize(trades, qmap, base_by_id)
            s["delta_net_vs_base"] = round(s["net_pnl"] - base_net, 4)
            s["delta_gross_vs_base"] = round(s["gross_pnl"] - base_gross, 4)
            vres[aname] = s
        report["variants"][rule] = vres

    # ---- q sweep + break-even on the settlement-LOSER set (all rules) ----
    # The time-stop's economic effect is fully carried by the settlement losers;
    # preempting settlement winners (naive rule1/2) is a mechanical drag, shown
    # separately below. So the sweep and break-even use the 96 losers for every
    # rule. rule1 vs rule2 differ ONLY in the trigger cap (1.2m vs 1.1m); at the
    # realistic no-move q (=entry_mid) they are identical.
    losers = [t for t in trades if t["exit_kind"] == "settlement"
              and t["exit_mid"] == 0 and NEXT[t["window_utc"]]]

    sweep = {}
    for rule in ("rule1", "rule2", "rule3"):
        trg = TRIGGER[rule]
        pts = []
        fracs = sorted(set([0.0, 0.25, 0.5, 0.64, 0.75, 1.0, 1 + trg]))
        for frac in fracs:
            qmap = {t["_id"]: frac * t["entry_mid"] for t in losers}
            s = summarize(trades, qmap, base_by_id)
            pts.append({"q_frac_of_entry_mid": round(frac, 4),
                        "net_pnl": s["net_pnl"], "gross_pnl": s["gross_pnl"],
                        "win_rate": s["win_rate"],
                        "delta_net_vs_base": round(s["net_pnl"] - base_net, 4)})
        sweep[rule] = pts
    report["q_sweep_settlement_losers"] = sweep

    # ---- break-even q (net == 0) over the settlement-loser set ----
    report["breakeven_q_frac_settlement_losers"] = None
    lo, hi = -1.0, 2.0
    for _ in range(80):
        mid = (lo + hi) / 2
        qmap = {t["_id"]: mid * t["entry_mid"] for t in losers}
        s = summarize(trades, qmap, base_by_id)
        if s["net_pnl"] < 0:
            lo = mid
        else:
            hi = mid
    report["breakeven_q_frac_settlement_losers"] = round((lo + hi) / 2, 4)

    # ---- naive winner-preemption: literal rule1/2 touching ALL affected ----
    naive = {}
    for rule in ("rule1", "rule2"):
        trg = TRIGGER[rule]
        aff = affected(trades, rule)
        qmap = {t["_id"]: t["entry_mid"] * (1 + trg) for t in aff}
        s = summarize(trades, qmap, base_by_id)
        s["delta_net_vs_base"] = round(s["net_pnl"] - base_net, 4)
        naive[rule] = s
    report["naive_winner_preemption"] = naive

    Path(args.out_json).write_text(json.dumps(report, indent=2))
    print("wrote", args.out_json)
    print(f"BASE: n={len(trades)} net={base_net} gross={base_gross}")
    for rule in ("rule1", "rule2", "rule3"):
        ac = report["affected_counts"][rule]
        print(f"\n{rule}: affected={ac['n_affected']} (settle losers="
              f"{ac['n_settle_losers']}, winners={ac['n_settle_winners']})")
        for aname, s in report["variants"][rule].items():
            print(f"   {aname:44s} net={s['net_pnl']:9.2f} "
                  f"gross={s['gross_pnl']:9.2f} win={s['win_rate']} "
                  f"delta={s['delta_net_vs_base']:9.2f}")
    print(f"\nbreakeven q (settlement-losers, as fraction of entry_mid): "
          f"{report['breakeven_q_frac_settlement_losers']}")
    for rule, s in report["naive_winner_preemption"].items():
        print(f"naive {rule} (all affected incl. winners): net={s['net_pnl']:9.2f} "
              f"delta={s['delta_net_vs_base']:9.2f}")
    return report


def _dist(rows):
    ex = defaultdict(int)
    for t in rows:
        ex[t["exit_kind"]] += 1
    return dict(sorted(ex.items()))


def _by_exit(rows):
    pe = defaultdict(float)
    for t in rows:
        pe[t["exit_kind"]] += t["net_pnl"]
    return {k: round(v, 2) for k, v in sorted(pe.items())}


if __name__ == "__main__":
    main()
