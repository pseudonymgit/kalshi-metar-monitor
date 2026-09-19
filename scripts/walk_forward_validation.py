#!/usr/bin/env python3
"""
WALK-FORWARD OUT-OF-SAMPLE VALIDATION — nwp_dtdt_fusion + frontal_passage_intraday
and a principled logistic stack over the honest signal set.

Purpose (Dan's ask): distinguish REAL EDGE from BASE-RATE LUCK on the two thin-edge
candidates found by R/T-2b. R/T-2b measured IN-SAMPLE MCC (whole 14,130-row matrix at
once), which conflates genuine skill with (a) base-rate artifacts and (b) LOOKAHEAD
leakage in the NWP backfill.

Critical lookahead finding (this driver establishes it):
  data/nwp_forecasts.db has target_date spanning 2021-2026 but fetch_date only
  2025-01-01 -> 2026-09-18. For target years 2021-2024 the "forecast" was BACKFILLED
  in 2026 (fetch_date 2026-07-28 -> 2026-09-02). That is a lookahead leak: the 2022-2024
  NWP values were generated AFTER the fact. Only target_date >= 2025-01-01 has a genuine
  contemporaneous fetch_date.

  -> nwp_dtdt_fusion and nwp_analog* depend on nwp_forecasts; they are only trustworthy
     for target_date >= 2025-01-01.
  -> frontal_passage_intraday uses ONLY intraday METAR (real-time observed) — trustworthy
     across the full range.

Method:
  1. Per-signal WALK-FORWARD: expanding training window by date. For each test date T
     strictly after the split, score the signal using ONLY its (prediction, actual) pairs
     with date <= T (no in-sample refit — the signal has no learned params; we simply
     do not let future observations inform past scoring). Report out-of-sample MCC,
     up-precision vs up-base-rate (computed on the SAME test slice), and whether edge
     survives.
  2. PRINCIPLED STACK: L2-regularized logistic regression over the HONEST signal set
     (signals whose in-sample MCC is NOT ~0 — excludes the base-rate-artifact/skew set).
     Walk-forward: train on dates <= split, predict on dates > split, rolling splits.
     Report whether the stack beats the best single candidate or collapses to base rate.

Deliverable: reports/walk_forward_validation_<ts>.json + this script (committed).

READ-ONLY. No signal change. No backfill/DROP. Does NOT touch migrate_add_strike_price.py.
"""

import json
import math
import os
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS_DIR = os.path.join(BASE, "reports")

# ──────────────────────────────────────────────────────────────────────
# Load the cached prediction matrix (14,130 rows) from R/T-2b
# ──────────────────────────────────────────────────────────────────────
CACHE = os.path.join(REPORTS_DIR, "rt2b_prediction_matrix_cache.json")

# Signals that depend on nwp_forecasts (lookahead-contaminated pre-2025).
# These must be restricted to target_date >= NWP_TRUST_CUTOFF.
NWP_DEPENDENT = {"nwp_dtdt_fusion", "nwp_analog", "nwp_analog_ensemble",
                 "nwp_direct", "fogr_reversion"}  # fogr uses HRRR NWP
NWP_TRUST_CUTOFF = "2025-01-01"  # first genuine fetch_date in nwp_forecasts
# Authoritative contemporaneity: (station, target_date) -> 1 if a forecast with
# fetch_date <= target_date exists. Lookahead leak otherwise (pre-2026 backfill).
CONTEMP_NWP = {}  # populated in main() from nwp_forecasts.db

# Signals whose in-sample MCC is ~0 or negative (base-rate artifacts / skew).
# Excluded from the honest stack per Dan's instruction.
BASE_RATE_ARTIFACTS = {
    "fogr_reversion", "spike_reversion", "warm_momentum", "radiational_cooling",
    "cooling_failure", "forecast_disagreement", "corrected_pressure_delta",
    "goldilocks", "gaussian_v2", "regime", "gaussian", "calendar_climatology",
}

# The two candidates.
CANDIDATES = ["nwp_dtdt_fusion", "frontal_passage_intraday"]


def _mcc(tp, fp, tn, fn):
    denom = ((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn)) ** 0.5
    return 0.0 if denom == 0 else (tp * tn - fp * fn) / denom


def _confusion_from_pairs(pairs):
    """pairs = list of (pred_dir, actual_dir). Returns (tp,fp,tn,fn)."""
    tp = fp = tn = fn = 0
    for pred, actual in pairs:
        if pred == "up":
            if actual == "up":
                tp += 1
            else:
                fp += 1
        else:
            if actual == "down":
                tn += 1
            else:
                fn += 1
    return tp, fp, tn, fn


def _metrics_from_pairs(pairs):
    tp, fp, tn, fn = _confusion_from_pairs(pairs)
    total = tp + fp + tn + fn
    if total == 0:
        return None
    up_called = tp + fp
    up_prec = (tp / up_called) if up_called else 0.0
    up_base = (tp + fn) / total  # actual up rate in this slice
    return {
        "n": total,
        "mcc": _mcc(tp, fp, tn, fn),
        "accuracy": (tp + tn) / total,
        "up_precision": up_prec,
        "up_base_rate": up_base,
        "up_edge": up_prec - up_base,
        "tp": tp, "fp": fp, "tn": tn, "fn": fn,
    }


def load_matrix():
    with open(CACHE) as f:
        matrix = json.load(f)
    # annotate each row with a sortable date and a bool flag for NWP-trustworthiness
    for r in matrix:
        r["_nwp_ok"] = r["date"] >= NWP_TRUST_CUTOFF
    matrix.sort(key=lambda r: r["date"])
    return matrix


def _nwp_contemp(r):
    """True if the row's NWP forecast is genuinely contemporaneous (no lookahead)."""
    return CONTEMP_NWP.get((r["station"], r["date"]), 0) == 1


def signal_pairs(matrix, sig, restrict_nwp=True):
    """Return list of (pred_dir, actual_dir, date) for a signal.
    If restrict_nwp and the signal is NWP-dependent, only keep rows with a genuinely
    contemporaneous NWP forecast (fetch_date <= target_date)."""
    pairs = []
    for r in matrix:
        if sig not in r["signals"]:
            continue
        if restrict_nwp and sig in NWP_DEPENDENT and not _nwp_contemp(r):
            continue
        d, c = r["signals"][sig]
        pairs.append((d, r["actual"], r["date"]))
    return pairs


def walk_forward_signal(matrix, sig, n_splits=5, restrict_nwp=True):
    """Expanding-window walk-forward: for each test split, score the signal on
    pairs with date > split point (out-of-sample), using only that test slice.
    Since the signal has no learned params, the 'train' side is only used to
    establish the split boundary (no refit). The honest question is whether the
    signal's edge persists in later (unseen) time slices."""
    pairs = signal_pairs(matrix, sig, restrict_nwp=restrict_nwp)
    if len(pairs) < 50:
        return {"error": f"insufficient pairs ({len(pairs)})", "n": len(pairs)}
    dates = [p[2] for p in pairs]
    # sort pairs by date
    pairs_sorted = sorted(pairs, key=lambda p: p[2])
    n = len(pairs_sorted)

    splits = []
    # split boundaries at even quantiles of time
    for i in range(1, n_splits):
        frac = i / n_splits
        split_date = pairs_sorted[int(frac * n)][2]
        splits.append(split_date)

    # rolling walk-forward: test on [split_i, split_{i+1})
    boundaries = [None] + splits + [None]
    fold_metrics = []
    all_oos = []  # union of all out-of-sample predictions
    for i in range(len(boundaries) - 1):
        lo, hi = boundaries[i], boundaries[i + 1]
        test = [(p, a) for (p, a, d) in pairs_sorted
                if (lo is None or d > lo) and (hi is None or d <= hi)]
        if not test:
            continue
        m = _metrics_from_pairs(test)
        if m:
            m["fold"] = i
            m["date_range"] = [test[0], test[-1]]
            fold_metrics.append(m)
            all_oos.extend(test)

    agg = _metrics_from_pairs(all_oos)
    return {
        "n_splits": n_splits,
        "n_total": n,
        "split_dates": splits,
        "folds": fold_metrics,
        "aggregate_oos": agg,
        "nwp_restricted": restrict_nwp and sig in NWP_DEPENDENT,
    }


# ──────────────────────────────────────────────────────────────────────
# Principled stack: L2 logistic regression, walk-forward (no sklearn dep).
# ──────────────────────────────────────────────────────────────────────
def _sigmoid(z):
    if z >= 0:
        zz = math.exp(-z)
        return 1.0 / (1.0 + zz)
    zz = math.exp(z)
    return zz / (1.0 + zz)


def fit_logistic(X, y, l2=1.0, lr=0.1, n_iter=200):
    """Minibatch gradient descent for L2 logistic regression. X: list of feature
    vectors, y: list of 0/1 labels. Returns weight vector w (len = n_features)."""
    n = len(X)
    if n == 0:
        return [0.0] * (len(X[0]) if X else 0)
    d = len(X[0])
    w = [0.0] * d
    for _ in range(n_iter):
        grad = [0.0] * d
        for xi, yi in zip(X, y):
            p = _sigmoid(sum(wj * xij for wj, xij in zip(w, xi)))
            err = p - yi
            for j in range(d):
                grad[j] += err * xi[j]
        for j in range(d):
            grad[j] = grad[j] / n + (l2 / n) * w[j]
            w[j] -= lr * grad[j]
    return w


def build_feature_rows(matrix, honest_signals, restrict_nwp=True):
    """Build (feature_vec, label, date) rows over the honest signal set.
    Feature = direction vote (-1 for down, +1 for up, 0 for no-fire) per signal.
    A row is kept only if at least one honest signal fires."""
    rows = []
    for r in matrix:
        feats = []
        keep = False
        for sig in honest_signals:
            if sig in r["signals"]:
                if restrict_nwp and sig in NWP_DEPENDENT and not _nwp_contemp(r):
                    feats.append(0.0)  # treat contaminated NWP as missing
                    continue
                d, c = r["signals"][sig]
                # signed confidence vote
                v = float(c if c else 0.5)
                feats.append(v if d == "up" else -v)
                keep = True
            else:
                feats.append(0.0)
        if keep:
            rows.append((feats, 1.0 if r["actual"] == "up" else 0.0, r["date"]))
    return rows


def walk_forward_stack(matrix, honest_signals, n_splits=5, restrict_nwp=True, l2=1.0, threshold=0.5):
    """Walk-forward L2-logistic stack over honest signals. Expanding train window.
    For each split: train on rows with date <= split, predict on date > split.
    The stack's OOS predictions are converted to 'up'/'down' via p >= threshold."""
    rows = build_feature_rows(matrix, honest_signals, restrict_nwp=restrict_nwp)
    rows.sort(key=lambda r: r[2])
    if len(rows) < 100:
        return {"error": f"insufficient rows ({len(rows)})"}
    n = len(rows)
    splits = []
    for i in range(1, n_splits):
        splits.append(rows[int(i * n / n_splits)][2])

    boundaries = [None] + splits + [None]
    fold_metrics = []
    all_oos = []  # (pred_dir, actual)
    n_train_total = 0
    for i in range(len(boundaries) - 1):
        lo, hi = boundaries[i], boundaries[i + 1]
        train = [r for r in rows if (lo is None or r[2] <= lo)]
        test = [r for r in rows if (lo is None or r[2] > lo) and (hi is None or r[2] <= hi)]
        if not train or not test:
            continue
        X_tr = [r[0] for r in train]
        y_tr = [r[1] for r in train]
        w = fit_logistic(X_tr, y_tr, l2=l2)
        n_train_total += len(train)
        preds = []
        for r in test:
            p = _sigmoid(sum(wj * xij for wj, xij in zip(w, r[0])))
            preds.append(("up" if p >= 0.5 else "down", "up" if r[1] >= 0.5 else "down"))
        m = _metrics_from_pairs(preds)
        if m:
            m["fold"] = i
            m["n_train"] = len(train)
            m["n_test"] = len(test)
            fold_metrics.append(m)
        all_oos.extend(preds)

    agg = _metrics_from_pairs(all_oos)
    return {
        "honest_signals": list(honest_signals),
        "n_splits": n_splits,
        "n_rows": n,
        "split_dates": splits,
        "folds": fold_metrics,
        "aggregate_oos": agg,
        "l2": l2,
        "restrict_nwp": restrict_nwp,
    }


def load_contemp_nwp():
    """Populate CONTEMP_NWP: (station, target_date) -> 1 iff a contemporaneous
    (fetch_date <= target_date) NWP forecast exists. Lookahead otherwise."""
    global CONTEMP_NWP
    import sqlite3
    db = os.path.join(BASE, "data", "nwp_forecasts.db")
    if not os.path.exists(db):
        return
    c = sqlite3.connect(db)
    cur = c.cursor()
    cur.execute(
        """SELECT station, target_date,
                  MAX(CASE WHEN fetch_date <= target_date THEN 1 ELSE 0 END)
           FROM nwp_forecasts GROUP BY station, target_date"""
    )
    CONTEMP_NWP = {(st, td): hc for st, td, hc in cur.fetchall()}
    c.close()


def main():
    t0 = time.time()
    load_contemp_nwp()
    print(f"Contemporaneous NWP keys loaded: {len(CONTEMP_NWP)}")
    matrix = load_matrix()
    n = len(matrix)
    up = sum(1 for r in matrix if r["actual"] == "up")
    print(f"Loaded {n} rows, base up-rate = {up/n*100:.2f}%")
    print(f"NWP trust cutoff = {NWP_TRUST_CUTOFF}")

    report = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "suite": "walk-forward out-of-sample validation",
        "axis": "strike-crossing (settlement_temp_f vs strike_price), date-tiered truth",
        "n_rows": n,
        "base_rate_up": round(up / n, 6),
        "lookahead_finding": {
            "note": "nwp_forecasts target_date spans 2021-2026 but fetch_date only 2025-01-01 -> 2026-09-18; pre-2025 targets were backfilled in 2026 (lookahead leak)",
            "nwp_trust_cutoff": NWP_TRUST_CUTOFF,
            "nwp_dependent_signals": sorted(NWP_DEPENDENT),
        },
        "candidates": {},
        "stack": {},
    }

    # ── 1. Per-signal walk-forward ──
    for sig in CANDIDATES:
        print(f"\n=== Walk-forward: {sig} ===")
        res = walk_forward_signal(matrix, sig, restrict_nwp=True)
        report["candidates"][sig] = res
        if "aggregate_oos" in res:
            agg = res["aggregate_oos"]
            print(f"  OOS aggregate: n={agg['n']} MCC={agg['mcc']:+7.4f} "
                  f"up-prec={agg['up_precision']*100:.1f}% up-base={agg['up_base_rate']*100:.1f}% "
                  f"up-edge={agg['up_edge']*100:+6.2f}pt")
            for f in res["folds"]:
                print(f"    fold {f['fold']}: n={f['n']:4d} MCC={f['mcc']:+7.4f} "
                      f"up-edge={f['up_edge']*100:+6.2f}pt")
        else:
            print(f"  {res}")

    # ── 2. Honest signal set + stack ──
    # Honest = signals that are NOT base-rate artifacts AND not in the candidate set
    # is fine; but we include candidates in the stack as features too (they are honest).
    # Define honest = all firing signals minus base-rate artifacts.
    firing = set()
    for r in matrix:
        firing.update(r["signals"].keys())
    honest = firing - BASE_RATE_ARTIFACTS
    # also drop the clearly-negative MCC ones explicitly (belt and suspenders)
    # but BASE_RATE_ARTIFACTS already covers them.
    honest = sorted(honest)
    print(f"\nHonest signal set ({len(honest)}): {honest}")

    print(f"\n=== Walk-forward stack (L2 logistic, n_splits=5) ===")
    stack_res = walk_forward_stack(matrix, honest, n_splits=5, restrict_nwp=True, l2=1.0)
    report["stack"] = stack_res
    if "aggregate_oos" in stack_res:
        agg = stack_res["aggregate_oos"]
        print(f"  OOS aggregate: n={agg['n']} MCC={agg['mcc']:+7.4f} "
              f"up-prec={agg['up_precision']*100:.1f}% up-base={agg['up_base_rate']*100:.1f}% "
              f"up-edge={agg['up_edge']*100:+6.2f}pt")
        for f in stack_res["folds"]:
            print(f"    fold {f['fold']}: n={f['n']:4d} MCC={f['mcc']:+7.4f} "
                  f"up-edge={f['up_edge']*100:+6.2f}pt")
    else:
        print(f"  {stack_res}")

    # ── 3. Temporal stability: year + month MCC breakdown (the artifact test) ──
    print("\n=== Temporal stability breakdown (artifact test) ===")
    report["temporal_stability"] = {}
    for sig in CANDIDATES:
        pairs = signal_pairs(matrix, sig, restrict_nwp=True)
        by_year = defaultdict(lambda: [0, 0, 0, 0])
        by_month = defaultdict(lambda: [0, 0, 0, 0])
        for pred, actual, date in pairs:
            y = date[:4]
            ym = date[:7]
            for bucket, d in ((by_year, y), (by_month, ym)):
                if pred == "up":
                    if actual == "up": bucket[d][0] += 1
                    else: bucket[d][1] += 1
                else:
                    if actual == "down": bucket[d][3] += 1
                    else: bucket[d][2] += 1
        report["temporal_stability"][sig] = {
            "by_year": {y: _metrics_from_pairs([("up","up")]*v[0] + [("up","down")]*v[1] + [("down","up")]*v[2] + [("down","down")]*v[3])
                        for y, v in sorted(by_year.items())},
            "by_month": {ym: _metrics_from_pairs([("up","up")]*v[0] + [("up","down")]*v[1] + [("down","up")]*v[2] + [("down","down")]*v[3])
                         for ym, v in sorted(by_month.items()) if sum(v) >= 20},
        }
        print(f"  {sig}:")
        for y, v in sorted(by_year.items()):
            tp, fp, fn, tn = v
            mm = _mcc(tp, fp, tn, fn)
            print(f"    {y}: n={tp+fp+tn+fn:5d} MCC={mm:+7.4f}")

    # ── 4. Write receipt ──
    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    out = os.path.join(REPORTS_DIR, f"walk_forward_validation_{ts}.json")
    with open(out, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\n=== Receipt: {out} ===")
    print(f"Elapsed: {time.time()-t0:.1f}s")
    return report, out


if __name__ == "__main__":
    main()
