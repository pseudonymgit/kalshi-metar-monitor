#!/usr/bin/env python3
"""brier_datefix_impact_2026-10-15.py — quantify the Brier impact of the 623-row
date-offset repair on data/kalshi_settlements.db.

Reproduces the honest axis from scripts/kalshi_truth_switch_2026-10-15.py, but
scores om_gfs under the **naive** join (`station, target_date`) against:
  * PRE-fix DB  (the private backup taken before the repair)
  * POST-fix DB (current)

So the delta is exactly the spurious penalty the naive join imposed on the 623
mis-dated rows. Read-only. Writes artifacts/brier-datefix-impact-2026-10-15.json.
"""
from __future__ import annotations

import gzip
import importlib.util as ilu
import json
import math
import pickle
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

GRID_PKL = Path("/tmp/we_grid_2026-10-13.pkl.gz")
RECS_PKL = Path("/tmp/we_asos_recs_2026-10-13.pkl.gz")
OM_CACHE = Path("/tmp/honest_omgfs_cache_2026-10-14.json")
DB = REPO / "data" / "kalshi_settlements.db"
PRE = REPO / "data" / "_backup_pre_datefix_20261015T012634Z.db"
OUT = REPO / "artifacts" / "brier-datefix-impact-2026-10-15.json"

PIN = 810

_spec = ilu.spec_from_file_location(
    "hsa", REPO / "scripts" / "honest_signal_accuracy_2026-10-14.py")
HSA = ilu.module_from_spec(_spec)
_spec.loader.exec_module(HSA)


def _mean(xs):
    return sum(xs) / len(xs)


def _std(xs, ddof=1):
    n = len(xs)
    if n <= ddof:
        return float("nan")
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - ddof))


def cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def p_ge(mu, sigma, x):
    if sigma <= 0:
        return 1.0 if mu >= x else 0.0
    return cdf((mu - x) / sigma)


def hsf_at(rec, pin):
    if not rec:
        return None
    vals = [b for b in rec.get("buckets", {}) if b <= pin]
    return max(rec["buckets"][b] for b in vals) if vals else None


def load_kalshi_naive(path):
    """NAIVE join: key on the stored (station, target_date), HIGH series only."""
    out = {}
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for st, td, kt in c.execute(
                "SELECT station, target_date, kalshi_temp FROM kalshi_settlements "
                "WHERE series LIKE '%HIGH%' AND kalshi_temp IS NOT NULL"):
            out.setdefault((st, td), float(kt))
    finally:
        c.close()
    return out


def score(ps, ys):
    n = len(ps)
    if n == 0:
        return {"n": 0}
    brier = sum((p - y) ** 2 for p, y in zip(ps, ys)) / n
    base = sum(ys) / n
    return {"n": n, "brier": round(brier, 4),
            "dir_acc": round(sum(1 for p, y in zip(ps, ys) if (p > 0.5) == bool(y)) / n, 4),
            "base_rate": round(base, 4)}


def main():
    with gzip.open(GRID_PKL, "rb") as f:
        grid = pickle.load(f)
    with gzip.open(RECS_PKL, "rb") as f:
        recs = pickle.load(f)
    om = json.loads(OM_CACHE.read_text())
    om = {(k.split("|")[0], k.split("|")[1]): v for k, v in om.items()}

    # om_gfs bias/sigma (honest recipe)
    res = defaultdict(list)
    for k, g in grid.items():
        if g.get("settled_max") is None or k not in om:
            continue
        res[k[0]].append(om[k] - g["settled_max"])
    allb = [v for vs in res.values() for v in vs]
    bg = _mean(allb)
    sgg = _std(allb)
    bs = {st: _mean(v) for st, v in res.items() if len(v) >= 3}
    sgs = {st: _std(v) for st, v in res.items() if len(v) >= 3}

    pre = load_kalshi_naive(PRE)
    post = load_kalshi_naive(DB)
    print(f"[bdi] pre naive keys={len(pre)} post naive keys={len(post)}")

    # build axis rows
    rows = []
    for (st, ds), g in grid.items():
        rec = recs.get((st, ds))
        h = hsf_at(rec, PIN)
        if h is None or (st, ds) not in om:
            continue
        T = float(math.floor(h) + 1.0)
        rows.append({"st": st, "ds": ds, "T": T,
                     "sigma": sgs.get(st, sgg), "bias": bs.get(st, bg)})

    out = {}
    for lbl, src in (("pre_fix_naive_join", pre), ("post_fix_naive_join", post)):
        got = []
        for r in rows:
            kt = src.get((r["st"], r["ds"]))
            if kt is None:
                continue
            p = p_ge(om[(r["st"], r["ds"])] - r["bias"], r["sigma"], r["T"])
            y = 1 if kt > r["T"] else 0
            got.append((p, y))
        out[lbl] = score([p for p, y in got], [y for p, y in got])
        out[lbl]["rows"] = len(got)

    d = out["post_fix_naive_join"]["brier"] - out["pre_fix_naive_join"]["brier"]
    delta = round(d, 4)

    art = {
        "generated_by": "scripts/brier_datefix_impact_2026-10-15.py",
        "axis": "om_gfs bc_ps (P(settled_high > T) at 13:30 pin), naive (station,target_date) join",
        "pre_db": str(PRE.relative_to(REPO)),
        "post_db": str(DB.relative_to(REPO)),
        "pre_fix_naive_join": out["pre_fix_naive_join"],
        "post_fix_naive_join": out["post_fix_naive_join"],
        "brier_improvement_post_minus_pre": delta,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(art, indent=2))
    print(json.dumps(art, indent=2))
    print(f"\n[bdi] om_gfs (bc_ps) naive-join Brier: PRE {out['pre_fix_naive_join']['brier']} "
          f"-> POST {out['post_fix_naive_join']['brier']}  (Δ {delta:+.4f})")


if __name__ == "__main__":
    main()
