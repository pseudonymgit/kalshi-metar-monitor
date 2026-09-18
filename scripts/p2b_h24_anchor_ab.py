#!/usr/bin/env python3
"""
P2-B A/B — h-24 daily anchor: WITH vs WITHOUT (empirical build-and-test).

Resolves the "is the h-24 daily anchor a real consumer or a dangling writer?"
question EMPIRICALLY, per Dan's directive: "build and test it with and without.
A/B. build and test, then we decide."

WHAT WE MEASURE
===============
The h-24 anchor's contribution to the intraday scorer is a *Bayesian prior* that
gets updated intraday. The update seam is documented in
core/intraday/recalibration_loop.py:

    updated_prob = (prior_belief*prior_weight + new_prob*new_weight)
                 / (prior_weight + new_weight)

Two arms on the SAME truth set (the ultimate suite's correct-axis 1,537 labels —
strike-crossing direction vs date-tiered settlement temp, NOT day-over-day):

  Arm A (WITH anchor):    prior = daily-LOOP per-station probability,
                          prior_weight = daily confidence. Intraday signal then
                          updates it.
  Arm B (WITHOUT anchor): prior = flat 0.5, prior_weight = 0  (pure intraday /
                          empirical base-rate), plus an "always-up" base-rate
                          baseline for reference.

Because the historical intraday signal surface fires ~0 signals on the truth-set
dates (METAR detector thresholds need NWP joins not present for backfill), the
anchor's *prior-only* contribution is the clean, measurable unit. We report BOTH:
  (a) prior-only edge (the anchor as a static station-level prior), and
  (b) the anchor + a synthetic-but-honest intraday update (anchor shifts the
      final prob; the update is the same in both arms, so the DELTA is the anchor).

This isolates the anchor's marginal edge — exactly the "with/without" delta.

METRICS (same definitions as ultimate_suite.py)
  - directional accuracy
  - MCC (imbalance-immune — truth is 82% up-skewed)
  - ECE (calibrated confidence, 10 bins)
  - Sharpe (annualized, unit bets, sqrt(252))

READ-ONLY. No signal changes, no wiring, no live trading. Dan holds wire-in.
"""

import argparse
import json
import math
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.ultimate_suite import load_labels_reconstruct, mcc, _sharpe

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANCHOR_PATH = os.path.join(BASE, "data", "intraday", "anchor_h24.json")
DAILY_LOOP_PATH = os.path.join(BASE, "data", "daily_loop_output.json")
REPORTS_DIR = os.path.join(BASE, "reports")

# Empirical base rate of the truth set (reported for reference, not a prior).
UP_BASE_RATE = 1264 / 1537  # ~0.82


def load_anchor():
    """Load per-station prior (probability + confidence) from the daily-LOOP
    anchor artifact. Prefer anchor_h24.json (aggregated); fall back to
    daily_loop_output.json stations_data (per-station detail)."""
    per_station = {}
    if os.path.exists(ANCHOR_PATH):
        d = json.load(open(ANCHOR_PATH))
        ps = d.get("per_station", {})
        for st, v in ps.items():
            if isinstance(v, dict):
                p = v.get("probability")
                if p is None:
                    td = v.get("temperature_distribution") or {}
                    p = td.get("mean")
                if p is None:
                    # map direction -> prob
                    dr = v.get("direction")
                    if dr == "UP":
                        p = 0.6
                    elif dr == "DOWN":
                        p = 0.4
                    else:
                        p = 0.5
                conf = v.get("confidence", 0.5)
                per_station[st] = (float(p), float(conf))
    if not per_station and os.path.exists(DAILY_LOOP_PATH):
        d = json.load(open(DAILY_LOOP_PATH))
        sd = d.get("stations_data", {})
        for st, v in sd.items():
            p = v.get("probability")
            conf = v.get("confidence", 0.5)
            if p is not None:
                per_station[st] = (float(p), float(conf))
    return per_station


def predict_prior_only(prior_prob, prior_weight):
    """Direction predicted by the prior alone. If prior_weight ~0 (flat), neutral."""
    if prior_weight <= 0.0:
        return "up", 0.5  # neutral — no directional claim
    return ("up" if prior_prob >= 0.5 else "down"), prior_prob


def update(prior_prob, prior_weight, new_prob, new_weight):
    """Documented Bayesian update (recalibration_loop.update_belief formula)."""
    tw = prior_weight + new_weight
    if tw <= 0:
        return 0.5
    return (prior_prob * prior_weight + new_prob * new_weight) / tw


def _ece(preds, n_bins=10):
    """Expected Calibration Error on directional probability (prob of UP).
    preds: list of (prob_up, is_up_bool)."""
    if not preds:
        return 0.0
    preds = sorted(preds, key=lambda x: x[0])
    step = max(1, len(preds) // n_bins)
    ece = 0.0
    for i in range(0, len(preds), step):
        bucket = preds[i:i + step]
        if not bucket:
            continue
        mean_conf = sum(p[0] for p in bucket) / len(bucket)
        mean_acc = sum(1 for p in bucket if p[1]) / len(bucket)
        ece += abs(mean_conf - mean_acc) * len(bucket)
    return ece / len(preds)


def evaluate_arm(name, preds):
    """preds: list of (pred_dir, prob_up, is_up). Return metric dict."""
    tp = fp = tn = fn = 0
    returns = []
    calib = []  # (prob_up, is_up) for ECE
    for pred_dir, prob_up, is_up in preds:
        actual = "up" if is_up else "down"
        # directional confusion
        if pred_dir == "up":
            if actual == "up":
                tp += 1
            else:
                fp += 1
        else:  # down (neutral folds into down here; see caller)
            if actual == "down":
                tn += 1
            else:
                fn += 1
        # calibration: probability assigned to UP
        calib.append((prob_up, is_up))
        # unit-bet return: bet UP if prob_up>0.5, DOWN otherwise; payout +1/-1
        bet_up = prob_up > 0.5
        if bet_up == is_up:
            returns.append(1.0)
        else:
            returns.append(-1.0)
    total = tp + fp + tn + fn
    acc = (tp + tn) / total if total else 0.0
    out = {
        "n": total,
        "accuracy": round(acc, 6),
        "mcc": round(mcc(tp, fp, tn, fn), 6),
        "tp": tp, "fp": fp, "tn": tn, "fn": fn,
        "ece": round(_ece(calib), 6),
        "sharpe": round(_sharpe(returns), 6),
    }
    return out


def build_predictions(labels, anchor, mode):
    """Build (pred_dir, prob_up, is_up) per label for a given arm mode.

    mode in {'A_prior_only', 'A_update', 'B_flat', 'B_empirical'}."""
    preds = []
    for st, v in labels.items():
        ap, aw = anchor.get(st, (0.5, 0.0))  # anchor prior prob + weight
        for (mt, date), truth in v.items():
            is_up = truth == "up"
            if mode == "A_prior_only":
                # anchor as static station prior, no update.
                # anchor `probability` IS P(UP): DOWN stations carry low p (0.05-0.3),
                # UP stations high p (0.7-0.95). So prob_up = ap directly.
                if aw <= 0.0:
                    d, prob_up = "down", 0.5  # neutral
                else:
                    d = "up" if ap >= 0.5 else "down"
                    prob_up = ap
                preds.append((d, prob_up, is_up))
            elif mode == "A_update":
                # anchor prior (P(UP)) + a signal-neutral intraday update
                # (new_prob=0.5, fixed weight) so the prior dominates and the
                # DELTA vs Arm B isolates the anchor's marginal pull.
                final = update(ap, aw, 0.5, 0.3)
                d = "up" if final >= 0.5 else "down"
                preds.append((d, final, is_up))
            elif mode == "B_flat":
                # flat prior, zero weight -> neutral prediction (no edge)
                preds.append(("down", 0.5, is_up))  # neutral folds to down
            elif mode == "B_empirical":
                # empirical base-rate prior (always UP, prob = base rate)
                preds.append(("up", UP_BASE_RATE, is_up))
    return preds


def main():
    ap = argparse.ArgumentParser(description="P2-B h-24 anchor A/B")
    ap.add_argument("--out", default=os.path.join(REPORTS_DIR, "p2b_h24_anchor_ab.json"))
    args = ap.parse_args()

    labels, _ = load_labels_reconstruct()
    anchor = load_anchor()
    n_labels = sum(len(v) for v in labels.values())
    n_anchor = len(anchor)
    print(f"Truth labels: {n_labels} | anchor stations: {n_anchor}")

    arms = {
        "A_prior_only": build_predictions(labels, anchor, "A_prior_only"),
        "A_update": build_predictions(labels, anchor, "A_update"),
        "B_flat": build_predictions(labels, anchor, "B_flat"),
        "B_empirical": build_predictions(labels, anchor, "B_empirical"),
    }

    results = {}
    for name, preds in arms.items():
        m = evaluate_arm(name, preds)
        results[name] = m
        print(f"\n=== {name} ===")
        for k, val in m.items():
            print(f"  {k}: {val}")

    payload = {
        "truth_labels": n_labels,
        "up_base_rate": round(UP_BASE_RATE, 4),
        "anchor_stations": n_anchor,
        "anchor_date": json.load(open(ANCHOR_PATH)).get("daily_date_utc")
        if os.path.exists(ANCHOR_PATH) else None,
        "arms": results,
        "note": (
            "Anchor is a single cross-section (2026-09-17) used as a static "
            "per-station prior across the 43-date truth set. A_update uses a "
            "signal-neutral update (new_prob=0.5) so the prior dominates — "
            "isolates the anchor's marginal directional pull."
        ),
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    json.dump(payload, open(args.out, "w"), indent=2)
    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
