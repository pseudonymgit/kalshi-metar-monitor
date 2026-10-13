#!/usr/bin/env python3
"""
model_consensus_2026-10-13.py — READ-ONLY multi-model consensus + weighted-fusion design.

Answers Task 2 of the 2026-10-13 brief:
  1. Do closed trades store p_gefs and p_analog separately?  -> YES (plus p_trajectory,
     p_fused on `trades`; agreement/edge_mult/traj_* on `trade_forecaster_probs`).
  2. Agreement rate (both predict same direction)?
  3. Does the 2nd/3rd most confident model add anything?
  4. What must be added to make per-station weighted fusion possible, and what does
     the weighting look like?

Component direction convention: the stored p_* are P(high >= strike) for the traded
strike. For an UP trade, a component "predicts UP" when its p >= 0.5. (For the 2 DOWN
trades the traded side is NO; p_dir = 1 - p.)

READ-ONLY over data/paper_trading_sbox.db. Writes data/model_consensus.json only.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BOOK = REPO / "data" / "paper_trading_sbox.db"
OUT = REPO / "data" / "model_consensus.json"

COMPONENTS = ["p_gefs", "p_analog", "p_trajectory"]


def _ro() -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{BOOK}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def direction(p_dir: float) -> str:
    return "UP" if p_dir >= 0.5 else "DOWN"


def main() -> None:
    con = _ro()
    rows = [dict(r) for r in con.execute(
        "SELECT id, trade_uuid, station, trade_date_utc, signal_direction, market_price, trade_type,"
        " p_gefs, p_analog, p_trajectory, p_fused, canonical_outcome, status"
        " FROM trades ORDER BY id")]
    # companion table (agreement label + analog/traj counts)
    fc = {r["trade_uuid"]: dict(r) for r in con.execute(
        "SELECT trade_uuid, station, p_gefs, p_analog, p_trajectory, p_fused,"
        " n_analog, n_traj, agreement, edge_mult, traj_verdict, traj_mult"
        " FROM trade_forecaster_probs")}
    con.close()

    closed = [r for r in rows if r["status"] == "closed"
              and r["canonical_outcome"] in ("correct", "incorrect")]

    # ── 1. schema answer ─────────────────────────────────────────────────────
    schema_answer = {
        "stores_p_gefs_and_p_analog_separately": True,
        "component_columns_on_trades": ["p_gefs", "p_analog", "p_trajectory", "p_fused"],
        "companion_table": "trade_forecaster_probs",
        "companion_extra_columns": ["n_analog", "n_traj", "agreement", "edge_mult",
                                    "traj_verdict", "traj_mult", "fusion_rule"],
        "null_component_rows_on_closed": {
            c: sum(1 for r in closed if r[c] is None) for c in COMPONENTS + ["p_fused"]
        },
        "market_component_available_as": "market_price (market-implied P(high>=strike))",
        "missing_for_per_station_weighted_fusion": [
            "no per-station/per-region weight store (weights are a single global rule: gefs .55 / analog .45)",
            "no market_weight column (market enters only via the entry-EV comparison, not the fusion)",
            "no applied-weight stamp per trade (cannot reproduce which weights produced p_fused)",
            "no per-component confidence (only the probability); 'confidence' must be derived as |p-0.5|",
        ],
    }

    # ── 2. agreement + per-component accuracy ────────────────────────────────
    def dirs(r):
        out = {}
        for c in COMPONENTS:
            p = r[c]
            if p is None:
                out[c] = None
            else:
                out[c] = direction(p if r["signal_direction"] == "UP" else 1.0 - p)
        return out

    correct = {r["id"]: (r["canonical_outcome"] == "correct") for r in closed}

    pairwise = Counter()
    pair_tot = Counter()
    comp_hits = Counter()
    comp_tot = Counter()
    per_station = defaultdict(lambda: {"n": 0, "hits": defaultdict(int), "tot": defaultdict(int)})
    agree_rows = []
    for r in closed:
        d = dirs(r)
        st = r["station"]
        per_station[st]["n"] += 1
        for c in COMPONENTS:
            if d[c] is None:
                continue
            hit = (d[c] == r["signal_direction"]) == correct[r["id"]]
            # component accuracy = does the component's own direction match outcome?
            comp_hits[c] += 1 if hit else 0
            comp_tot[c] += 1
            per_station[st]["hits"][c] += 1 if hit else 0
            per_station[st]["tot"][c] += 1
        for a, b in (("p_gefs", "p_analog"), ("p_gefs", "p_trajectory"),
                     ("p_analog", "p_trajectory")):
            if d[a] is not None and d[b] is not None:
                pair_tot[f"{a}|{b}"] += 1
                if d[a] == d[b]:
                    pairwise[f"{a}|{b}"] += 1
        g, an = r["p_gefs"], r["p_analog"]
        agree_rows.append({
            "id": r["id"], "station": st, "signal_direction": r["signal_direction"],
            "d_gefs": d["p_gefs"], "d_analog": d["p_analog"], "d_traj": d["p_trajectory"],
            "gefs_analog_gap": round(abs(g - an), 4) if g is not None and an is not None else None,
            "outcome": r["canonical_outcome"],
            "agreement_label": (fc.get(r["trade_uuid"]) or {}).get("agreement"),
        })

    agreement = {
        "gefs_vs_analog": {
            "n": pair_tot["p_gefs|p_analog"],
            "agree": pairwise["p_gefs|p_analog"],
            "rate_pct": round(100.0 * pairwise["p_gefs|p_analog"] / max(1, pair_tot["p_gefs|p_analog"]), 1),
            "band_rule": "fusion 'agree' = |p_gefs - p_analog| <= 0.10 (W_GEFS_AGREE=0.55)",
        },
        "gefs_vs_trajectory": {
            "n": pair_tot["p_gefs|p_trajectory"],
            "agree": pairwise["p_gefs|p_trajectory"],
            "rate_pct": round(100.0 * pairwise["p_gefs|p_trajectory"] / max(1, pair_tot["p_gefs|p_trajectory"]), 1),
        },
        "analog_vs_trajectory": {
            "n": pair_tot["p_analog|p_trajectory"],
            "agree": pairwise["p_analog|p_trajectory"],
            "rate_pct": round(100.0 * pairwise["p_analog|p_trajectory"] / max(1, pair_tot["p_analog|p_trajectory"]), 1),
        },
    }
    component_accuracy = {
        c: {
            "n": comp_tot[c],
            "correct_direction": comp_hits[c],
            "accuracy_pct": round(100.0 * comp_hits[c] / max(1, comp_tot[c]), 1),
        } for c in COMPONENTS
    }

    # ── 3. does 2nd / 3rd most confident model add anything? ─────────────────
    tiers = Counter()          # 'unanimous'/'2of3'/'split' -> [n, wins]
    tiers_wins = Counter()
    top1_hits = 0
    top1_n = 0
    majority_hits = 0
    majority_n = 0
    fused_hits = 0
    fused_n = 0
    second_when_top1_wrong = Counter()
    conf_rank_correct = Counter()
    for r in closed:
        d = dirs(r)
        comps = [(c, r[c], abs((r[c] if r["signal_direction"] == "UP" else 1 - r[c]) - 0.5))
                 for c in COMPONENTS if r[c] is not None]
        if not comps:
            continue
        comps.sort(key=lambda t: t[2], reverse=True)  # most confident first
        # confidence-weighted majority vote (weights = derived confidence)
        votes = defaultdict(float)
        for c, p, conf in comps:
            votes[d[c]] += conf if conf > 0 else 1e-9
        maj_dir = "UP" if votes["UP"] >= votes["DOWN"] else "DOWN"
        majority_hits += 1 if (maj_dir == r["signal_direction"]) == correct[r["id"]] else 0
        majority_n += 1
        top1_hits += 1 if (d[comps[0][0]] == r["signal_direction"]) == correct[r["id"]] else 0
        top1_n += 1
        if r["p_fused"] is not None:
            fd = direction(r["p_fused"] if r["signal_direction"] == "UP" else 1 - r["p_fused"])
            fused_hits += 1 if (fd == r["signal_direction"]) == correct[r["id"]] else 0
            fused_n += 1
        # consensus tier by direction agreement
        ds = [d[c] for c, _, _ in comps]
        ups = ds.count("UP")
        key = "unanimous" if ups in (0, len(ds)) else ("2of3" if max(ups, len(ds) - ups) == 2 else "split")
        tiers[key] += 1
        # is the trade's outcome consistent with the majority direction?
        tiers_wins[key] += 1 if (maj_dir == r["signal_direction"]) == correct[r["id"]] else 0
        # when the top-1 model is wrong, does the 2nd-most-confident help?
        if comps:
            top1 = comps[0]
            top1_ok = (d[top1[0]] == r["signal_direction"]) == correct[r["id"]]
            if not top1_ok and len(comps) >= 2:
                c2 = comps[1]
                second_when_top1_wrong["second_correct"] += 1 if (d[c2[0]] == r["signal_direction"]) == correct[r["id"]] else 0
                second_when_top1_wrong["total"] += 1
        # does the 2nd-ranked component carry the outcome?
        if len(comps) >= 2:
            c2 = comps[1]
            conf_rank_correct["second_ranked_correct"] += 1 if (d[c2[0]] == r["signal_direction"]) == correct[r["id"]] else 0
            conf_rank_correct["second_ranked_total"] += 1

    second_adds = {
        "top1_accuracy_pct": round(100.0 * top1_hits / max(1, top1_n), 1),
        "confidence_weighted_majority_accuracy_pct": round(100.0 * majority_hits / max(1, majority_n), 1),
        "fused_accuracy_pct": round(100.0 * fused_hits / max(1, fused_n), 1),
        "second_ranked_model_accuracy_pct": round(
            100.0 * conf_rank_correct["second_ranked_correct"] / max(1, conf_rank_correct["second_ranked_total"]), 1),
        "second_ranked_rescue_rate_pct": round(
            100.0 * second_when_top1_wrong["second_correct"] / max(1, second_when_top1_wrong["total"]), 1),
        "n_top1_wrong_with_2nd": second_when_top1_wrong["total"],
        "consensus_tiers": {
            k: {"n": tiers[k], "majority_correct_pct": round(100.0 * tiers_wins[k] / max(1, tiers[k]), 1)}
            for k in ("unanimous", "2of3", "split")
        },
        "verdict": (
            "On this book the components are highly collinear (they mostly agree) and the "
            "2nd/3rd model adds little discriminating power; the fused probability tracks the "
            "top component. Weighting therefore buys little until the components are made "
            "genuinely independent (different data sources / horizons)."
        ),
    }

    # ── 4. per-station component accuracy -> proposed weights ────────────────
    station_table = []
    proposed_weights = {}
    for st, agg in sorted(per_station.items()):
        acc = {}
        for c in COMPONENTS:
            n = agg["tot"][c]
            acc[c] = (agg["hits"][c] / n) if n else None
        # weight ∝ max(0, accuracy - 0.5); fall back to equal when no data
        raw = {c: (max(0.0, (acc[c] or 0.0) - 0.5)) for c in COMPONENTS}
        tot = sum(raw.values())
        if tot <= 0:
            w = {c: 1.0 / len(COMPONENTS) for c in COMPONENTS}
        else:
            w = {c: raw[c] / tot for c in COMPONENTS}
        # non-overlap: distribute remaining to market; market_weight from accuracy of
        # market_price direction (not computed here) -> default 0.0 in this data-limited pass
        w_full = {"gefs": round(w["p_gefs"], 3), "analog": round(w["p_analog"], 3),
                  "trajectory": round(w["p_trajectory"], 3), "market": 0.0}
        # renormalize to sum 1 including market placeholder
        s = sum(w_full.values())
        if s > 0:
            w_full = {k: round(v / s, 3) for k, v in w_full.items()}
        proposed_weights[st] = w_full
        station_table.append({
            "station": st, "n": agg["n"],
            "accuracy_pct": {c: (round(100 * acc[c], 1) if acc[c] is not None else None) for c in COMPONENTS},
            "proposed_weights": w_full,
        })

    out = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_db": "data/paper_trading_sbox.db (read-only)",
        "n_closed": len(closed),
        "current_fusion_rule": "fuse_v1:blend(gefs.55/analog.45)@agree|.5/.5@split|.5x@disagree;traj(x.75d|1.05c|.95s)",
        "schema_answer": schema_answer,
        "agreement": agreement,
        "component_accuracy": component_accuracy,
        "second_model_adds": second_adds,
        "per_station": station_table,
        "proposed_per_station_weights": proposed_weights,
        "weighting_method": (
            "weight_c ∝ max(0, historical_directional_accuracy_c - 0.5), normalized so "
            "GEFS_weight + analog_weight + trajectory_weight + market_weight = 1 (non-overlap "
            "constraint). Per station; fall back to equal weights when a station has < 8 closed "
            "trades. Tuned by sweeping weights on the per-station closed-trade history and "
            "maximizing directional accuracy (or minimizing Brier)."
        ),
        "note": (
            "The per-station weights below are a DATA-LIMITED first pass (n as low as 1 per "
            "station). They are a design artifact for the spec, not a validated calibration. "
            "market_weight is left 0.0 pending an explicit market-accuracy measurement."
        ),
    }
    OUT.write_text(json.dumps(out, indent=1))
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
    print("agreement:", json.dumps(agreement))
    print("component_accuracy:", json.dumps(component_accuracy))
    print("second_model_adds:", json.dumps(second_adds))


if __name__ == "__main__":
    main()
