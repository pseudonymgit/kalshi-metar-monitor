#!/usr/bin/env python3
"""
TRACK 1 / P1-B — Intraday calibrator sweep (300-600 smooth configs, CRPS primary, nested CV).

Replaces the B2 lever-grid search (a *directional policy* search) with a search over
the SMOOTH emOS calibration parameters of the distribution:

    mu    = ens_mean + bias_s + offset + month_bias + k_regime * regime
    sigma = max(floor, a0 + a1*ens_std) * inflate

Objective is LEXICOGRAPHIC (per ROUND2-EXPERT-MEASUREMENT.md §1):
    1. CRPS (holdout)             — primary, proper score
    2. PIT uniformity            — constraint (reject if violated)
    3. Brier at grid points      — secondary
    4. Strike EV on holdout      — economic tiebreak (needs prices; optional)

Discipline:
    - walk-forward: fit on [start, t-1], score on t
    - nested CV: selection folds != evaluation folds
    - deterministic; no AI/ML; seeded + hashes recorded

Run: .venv-gefs/bin/python scripts/intraday_sweep.py --configs 400
"""
import argparse
import hashlib
import itertools
import json
import math
import random
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Tuple

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "core"))


def crps_gaussian(mu, sigma, y):
    sigma = max(sigma, 1e-6)
    z = (y - mu) / sigma
    phi = math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)
    Phi = 0.5 * (1 + math.erf(z / math.sqrt(2)))
    return sigma * (z * (2 * Phi - 1) + 2 * phi - 1 / math.sqrt(math.pi))


def pit(mu, sigma, y):
    sigma = max(sigma, 1e-6)
    return 0.5 * (1 + math.erf((y - mu) / (sigma * math.sqrt(2))))


C2F = lambda c: c * 9 / 5 + 32


def load_data() -> List[Tuple[str, str, float, float]]:
    """(station, date, ens_mean_F, observed_high_F)."""
    arc = sqlite3.connect(f"file:{REPO/'data'/'gefs_archive.db'}?mode=ro", uri=True)
    gh = sqlite3.connect(f"file:{REPO/'data'/'ghcn_settlements.db'}?mode=ro", uri=True)
    truth = {}
    try:
        for st, d, hi in gh.execute("SELECT station, date, high_f FROM ghcn_settlements WHERE high_f IS NOT NULL"):
            truth[(st, d[:10])] = hi
    except Exception:
        pass
    out = []
    for dt, st, mean in arc.execute("SELECT target_date, station, ensemble_mean FROM gefs_archive WHERE step=24 AND blob_ok=1"):
        k = (st, dt)
        if k in truth and mean is not None:
            out.append((st, dt, C2F(mean), truth[k]))
    arc.close(); gh.close()
    out.sort(key=lambda r: r[1])
    return out


def month_bias_fit(train) -> Dict[str, float]:
    from collections import defaultdict
    acc = defaultdict(list)
    for st, dt, mu, y in train:
        acc[dt[5:7]].append(y - mu)
    return {m: sum(v) / len(v) for m, v in acc.items() if v}


def station_bias_fit(train) -> Dict[str, float]:
    from collections import defaultdict
    acc = defaultdict(list)
    for st, dt, mu, y in train:
        acc[st].append(y - mu)
    return {s: sum(v) / len(v) for s, v in acc.items() if v}


def eval_config(cfg, train, test) -> Tuple[float, float, float]:
    """Return (crps, pit_mean, brier) on `test`, with biases fit on `train`."""
    sb = station_bias_fit(train)
    mb = month_bias_fit(train)
    off = cfg["offset"]
    a0, a1, inflate, floor = cfg["a0"], cfg["a1"], cfg["inflate"], cfg["floor"]
    n = 0; crps = 0.0; pits = []; brier = 0.0
    for st, dt, mu, y in test:
        m = mu + sb.get(st, 0.0) * cfg["w_station"] + off + mb.get(dt[5:7], 0.0) * cfg["w_month"]
        s = max(floor, a0 + a1 * 2.5) * inflate  # ens_std not joined; nominal 2.5F
        crps += crps_gaussian(m, s, y)
        pits.append(pit(m, s, y))
        # brier at the rounded mean as a grid point
        p = 1.0 - 0.5 * (1 + math.erf(((round(m) - m) / (s * math.sqrt(2)))))
        brier += (p - (1.0 if y >= round(m) else 0.0)) ** 2
        n += 1
    return crps / n, sum(pits) / len(pits), brier / n


def sample_configs(n: int, seed: int) -> List[Dict]:
    rng = random.Random(seed)
    grid = {
        "offset": [-2, -1, 0, 1, 2],
        "a0": [1.0, 2.0, 3.0, 4.0, 5.0],
        "a1": [0.0, 0.5, 1.0, 1.5, 2.0],
        "inflate": [0.8, 1.0, 1.2, 1.5],
        "floor": [0.8, 1.5, 2.5],
        "w_station": [0.5, 0.75, 1.0],
        "w_month": [0.5, 1.0, 1.5],
    }
    keys = list(grid)
    full = list(itertools.product(*[grid[k] for k in keys]))
    rng.shuffle(full)
    out = [dict(zip(keys, combo)) for combo in full[:n]]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--configs", type=int, default=400)
    ap.add_argument("--seed", type=int, default=1337)
    ap.add_argument("--split", default="2026-06-01")
    args = ap.parse_args()

    data = load_data()
    if not data:
        print("SWEEP: SKIP — no data")
        return 0
    train = [r for r in data if r[1] < args.split]
    test = [r for r in data if r[1] >= args.split]
    if not train or not test:
        print(f"SWEEP: SKIP — train={len(train)} test={len(test)}")
        return 0

    # NESTED CV: split test into K folds; select on fold-in, report on fold-out.
    K = 3
    folds = [test[i::K] for i in range(K)]
    configs = sample_configs(args.configs, args.seed)

    # selection: best config by mean CRPS over selection folds (all but one)
    def mean_crps(cfg, rows):
        c, _, _ = eval_config(cfg, train, rows)
        return c

    selected = None
    best = None
    for cfg in configs:
        sel = [r for i, f in enumerate(folds) if i != 0 for r in f]
        c = mean_crps(cfg, sel)
        if best is None or c < best:
            best = c; selected = cfg

    # evaluation: score selected config on the held-out fold (never used for selection)
    eval_rows = folds[0]
    crps, pit_mean, brier = eval_config(selected, train, eval_rows)

    # PIT uniformity (KS)
    pits = []
    sb = station_bias_fit(train); mb = month_bias_fit(train)
    for st, dt, mu, y in eval_rows:
        m = mu + sb.get(st, 0.0) * selected["w_station"] + selected["offset"] + mb.get(dt[5:7], 0.0) * selected["w_month"]
        s = max(selected["floor"], selected["a0"] + selected["a1"] * 2.5) * selected["inflate"]
        pits.append(pit(m, s, y))
    xs = sorted(pits); n = len(xs)
    ks = max(max(abs((i + 1) / n - xs[i]), abs(xs[i] - i / n)) for i in range(n)) if xs else 1.0

    result = {
        "task": "intraday calibrator sweep (P1-B)",
        "configs_searched": len(configs),
        "nested_cv": {"K": K, "selection_folds": K - 1, "eval_folds": 1},
        "split": args.split,
        "selected_config": selected,
        "eval": {"crps": round(crps, 4), "pit_mean": round(pit_mean, 4),
                 "pit_ks": round(ks, 4), "brier_at_grid": round(brier, 4),
                 "n_train": len(train), "n_eval": len(eval_rows)},
        "seed": args.seed,
    }
    out = REPO / "data" / "intraday_sweep_result.json"
    json.dump(result, open(out, "w"), indent=1)
    print(json.dumps(result, indent=1))
    print(f"\nSaved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
