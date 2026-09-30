#!/usr/bin/env python3
"""
R/T (2026-09-29) — Multi-frequency intraday calibrator sweep.

Extends `intraday_sweep.py` (daily-target, GEFS ensemble mean) with an INTRADAY
high-so-far conditioning axis at two frequencies:

  * hourly  — METAR (`metar_backfill.db`, ~hourly obs)
  * 15m     — IEM ASOS 1-min resampled to 15-minute buckets (`iem_asos_1min.db`)

1m is deliberately OUT OF SCOPE: a 1-min model of the *daily* high adds input
noise without a new signal; it needs a short-horizon nowcast truth series first
(see docs/plans/INTRADAY-SUBHOURLY-SCOPING-2026-09-29.md).

Model (same family as P1-B, LEXICOGRAPHIC objective):
    mu    = ens_mean + station_bias + offset + month_bias
    sigma = max(floor, a0 + a1*ens_std) * inflate
  conditioned on `hsf` = high-so-far at the intraday cut (per frequency).

Objective: CRPS (holdout, proper score) primary; PIT uniformity constraint;
Brier at grid secondary. Nested CV (selection folds != eval fold).

Discipline: walk-forward, deterministic, seeded, no AI/ML.

Run:
  .venv-gefs/bin/python scripts/intraday_multifreq_sweep.py --freq hourly --configs 400
  .venv-gefs/bin/python scripts/intraday_multifreq_sweep.py --freq 15m    --configs 400
  .venv-gefs/bin/python scripts/intraday_multifreq_sweep.py --freq both   --configs 400
"""
import argparse
import itertools
import json
import math
import random
import sqlite3
import sys
from collections import defaultdict
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


def _conn(name):
    return sqlite3.connect(f"file:{REPO/'data'/name}?mode=ro", uri=True)


def load_truth() -> Dict[Tuple[str, str], float]:
    """(station, YYYY-MM-DD) -> observed daily high_F (GHCN)."""
    gh = _conn("ghcn_settlements.db")
    out = {}
    try:
        for st, d, hi in gh.execute(
            "SELECT station, date, high_f FROM ghcn_settlements WHERE high_f IS NOT NULL"
        ):
            out[(st, d[:10])] = hi
    except Exception:
        pass
    gh.close()
    return out


def load_gefs_means() -> Dict[Tuple[str, str], float]:
    """(station, YYYY-MM-DD) -> ensemble mean in F (step 24)."""
    g = _conn("gefs_archive.db")
    out = {}
    for st, dt, mean in g.execute(
        "SELECT station, target_date, ensemble_mean FROM gefs_archive WHERE step=24 AND blob_ok=1"
    ):
        if mean is not None:
            out[(st, dt[:10])] = C2F(mean)
    g.close()
    return out


def load_intraday_hsf(freq: str, cut_hour_utc: int = 18) -> Dict[Tuple[str, str], float]:
    """
    (station, YYYY-MM-DD) -> high-so-far_F at the intraday cut, per frequency.

    R/T (2026-09-29) LEAK FIX: the first cut computed the max over the FULL day,
    which is (approximately) the daily high itself — so `w_hsf=1.0` scored 1.45
    CRPS by predicting the target from the target (target leakage). Now truncated
    to observations <= `cut_hour_utc`, leaving genuine afternoon uncertainty for
    the ensemble/forecast component to cover.

    hourly : max METAR temp_F with hour <= cut_hour_utc (UTC)
    15m    : max ASOS tmpf over 15-min buckets with hour <= cut_hour_utc (UTC)
    """
    cut = f"{cut_hour_utc:02d}"
    if freq == "hourly":
        c = _conn("metar_backfill.db")
        rows = c.execute(
            "SELECT station, date_utc, MAX(temp_f) FROM metar_observations "
            "WHERE temp_f IS NOT NULL AND CAST(substr(timestamp_utc,12,2) AS INT) <= ? "
            "GROUP BY station, date_utc",
            (cut_hour_utc,),
        )
    elif freq == "15m":
        c = _conn("iem_asos_1min.db")
        # FIX 2026-09-30: `observations.tmpf` is ALREADY Fahrenheit (IEM ASOS
        # convention; global range -49..122). The prior query applied
        # `tmpf * 9/5 + 32`, double-converting 66F -> ~151F and corrupting every
        # CRPS weighted by w_hsf. Use tmpf directly.
        rows = c.execute(
            "SELECT station, substr(valid,1,10) AS d, MAX(tmpf) "
            "FROM observations "
            "WHERE tmpf IS NOT NULL AND CAST(substr(valid,12,2) AS INT) <= ? "
            "GROUP BY station, d",
            (cut_hour_utc,),
        )
        out = {}
        for st, d, mx in rows:
            if mx is not None:
                out[(st, d[:10])] = mx
        c.close()
        return out
    else:
        raise ValueError(f"unknown freq {freq}")
    out = {}
    for st, d, mx in rows:
        if mx is not None:
            out[(st, d[:10])] = mx
    c.close()
    return out


def build_dataset(freq: str, cut_hour_utc: int = 18) -> List[Tuple[str, str, float, float, float]]:
    """Rows: (station, date, ens_mean_F, high_so_far_F, truth_high_F)."""
    truth = load_truth()
    gefs = load_gefs_means()
    hsf = load_intraday_hsf(freq, cut_hour_utc)
    rows = []
    for k, mean in gefs.items():
        if k in truth and k in hsf:
            rows.append((k[0], k[1], mean, hsf[k], truth[k]))
    rows.sort(key=lambda r: r[1])
    return rows


def month_bias_fit(train, key_idx):
    acc = defaultdict(list)
    for r in train:
        acc[r[1][5:7]].append(r[4] - r[key_idx])
    return {m: sum(v) / len(v) for m, v in acc.items() if v}


def station_bias_fit(train, key_idx):
    acc = defaultdict(list)
    for r in train:
        acc[r[0]].append(r[4] - r[key_idx])
    return {s: sum(v) / len(v) for s, v in acc.items() if v}


def eval_config(cfg, train, test):
    """Return (crps, pit_mean, brier). mu blends ens_mean and high-so-far."""
    sb_e = station_bias_fit(train, 2)
    mb_e = month_bias_fit(train, 2)
    sb_h = station_bias_fit(train, 3)
    off = cfg["offset"]
    a0, a1, inflate, floor = cfg["a0"], cfg["a1"], cfg["inflate"], cfg["floor"]
    w_hsf = cfg["w_hsf"]
    n = 0; crps = 0.0; pits = []; brier = 0.0
    for st, dt, mean, hsf, y in test:
        base = (1.0 - w_hsf) * (mean + sb_e.get(st, 0.0)) + w_hsf * (hsf + sb_h.get(st, 0.0))
        m = base + off + mb_e.get(dt[5:7], 0.0) * cfg["w_month"]
        s = max(floor, a0 + a1 * 2.5) * inflate
        crps += crps_gaussian(m, s, y)
        pits.append(pit(m, s, y))
        p = 1.0 - 0.5 * (1 + math.erf(((round(m) - m) / (s * math.sqrt(2)))))
        brier += (p - (1.0 if y >= round(m) else 0.0)) ** 2
        n += 1
    if n == 0:
        return float("inf"), 0.0, 0.0
    return crps / n, sum(pits) / len(pits), brier / n


def sample_configs(n: int, seed: int) -> List[Dict]:
    rng = random.Random(seed)
    grid = {
        "offset": [-2, -1, 0, 1, 2],
        "a0": [1.0, 2.0, 3.0, 4.0, 5.0],
        "a1": [0.0, 0.5, 1.0, 1.5, 2.0],
        "inflate": [0.8, 1.0, 1.2, 1.5],
        "floor": [0.8, 1.5, 2.5],
        "w_hsf": [0.0, 0.25, 0.5, 0.75, 1.0],
        "w_month": [0.5, 1.0, 1.5],
    }
    keys = list(grid)
    full = list(itertools.product(*[grid[k] for k in keys]))
    rng.shuffle(full)
    out = [dict(zip(keys, combo)) for combo in full[:n]]
    return out


def run_freq(freq: str, n_configs: int, seed: int, split: str, cut_hour_utc: int = 18) -> Dict:
    data = build_dataset(freq, cut_hour_utc)
    if not data:
        return {"freq": freq, "status": "SKIP — no data"}
    train = [r for r in data if r[1] < split]
    test = [r for r in data if r[1] >= split]
    if not train or not test:
        return {"freq": freq, "status": f"SKIP — train={len(train)} test={len(test)}"}

    K = 3
    folds = [test[i::K] for i in range(K)]
    configs = sample_configs(n_configs, seed)

    selected = None
    best = None
    for cfg in configs:
        sel = [r for i, f in enumerate(folds) if i != 0 for r in f]
        c, _, _ = eval_config(cfg, train, sel)
        if best is None or c < best:
            best = c; selected = cfg

    eval_rows = folds[0]
    crps, pit_mean, brier = eval_config(selected, train, eval_rows)

    # PIT KS uniformity
    sb_e = station_bias_fit(train, 2); mb_e = month_bias_fit(train, 2); sb_h = station_bias_fit(train, 3)
    pits = []
    for st, dt, mean, hsf, y in eval_rows:
        base = (1.0 - selected["w_hsf"]) * (mean + sb_e.get(st, 0.0)) + selected["w_hsf"] * (hsf + sb_h.get(st, 0.0))
        m = base + selected["offset"] + mb_e.get(dt[5:7], 0.0) * selected["w_month"]
        s = max(selected["floor"], selected["a0"] + selected["a1"] * 2.5) * selected["inflate"]
        pits.append(pit(m, s, y))
    xs = sorted(pits); nn = len(xs)
    ks = max(max(abs((i + 1) / nn - xs[i]), abs(xs[i] - i / nn)) for i in range(nn)) if xs else 1.0

    return {
        "freq": freq,
        "status": "OK",
        "n_rows": len(data),
        "n_train": len(train),
        "n_eval": len(eval_rows),
        "nested_cv": {"K": K, "selection_folds": K - 1, "eval_folds": 1},
        "split": split,
        "selected_config": selected,
        "eval": {"crps": round(crps, 4), "pit_mean": round(pit_mean, 4),
                 "pit_ks": round(ks, 4), "brier_at_grid": round(brier, 4)},
        "seed": seed,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--freq", choices=["hourly", "15m", "both"], default="both")
    ap.add_argument("--configs", type=int, default=400)
    ap.add_argument("--seed", type=int, default=1337)
    ap.add_argument("--split", default="2026-06-01")
    ap.add_argument("--cut-hour-utc", type=int, default=18,
                    help="high-so-far truncation hour (UTC); default 18 keeps afternoon uncertainty")
    args = ap.parse_args()

    freqs = ["hourly", "15m"] if args.freq == "both" else [args.freq]
    results = {}
    for f in freqs:
        r = run_freq(f, args.configs, args.seed, args.split, args.cut_hour_utc)
        results[f] = r
        print(f"\n=== {f} ===")
        print(json.dumps(r, indent=1))

    out = REPO / "data" / "intraday_multifreq_results.json"
    json.dump({"generated_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
               "results": results}, open(out, "w"), indent=1)
    print(f"\nSaved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
