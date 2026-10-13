#!/usr/bin/env python3
"""ceiling_refit_2026-10-13.py — Item 1: what SHOULD the confidence ceiling be?

Two-part rigor:

(A) The published rate_1h number (Brier 0.0921, resolution 0.0871) is a WINDOW-MAX
    statistic: build_grid scores the BEST p over the intraday scan 13:00..16:45,
    i.e. it looks past the 13:30 pin. We test how much of the resolution is
    look-ahead by bounding the scan to the pin.

(B) rate_1h is NOT a bridge fusion member (the live book uses p_gefs / p_analog /
    p_trajectory / p_market). So we also derive the honest, pipeline-faithful
    resolution for the NWP member (gefs pressure) and report what the ceiling
    mechanism should be fed.

Feeds each resolution into `calibrated_ceiling()` and reports the ceiling per
station + pooled. READ-ONLY on every book. Writes ONLY
`artifacts/ceiling-refit/ceiling_refit_2026-10-13.json`.
"""
from __future__ import annotations

import importlib.util as ilu
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

spec = ilu.spec_from_file_location(
    "pds", REPO / "scripts" / "predictor_diversity_study_2026-10-13.py")
PDS = ilu.module_from_spec(spec)
spec.loader.exec_module(PDS)

from core.intraday.multi_forecaster_fusion import (  # noqa: E402
    calibrated_ceiling, CALIBRATION_FLOOR, MAX_CEILING, RESOLUTION_FULL_SKILL,
    HOLDOUT_RESOLUTION,
)

OUT = REPO / "artifacts" / "ceiling-refit" / "ceiling_refit_2026-10-13.json"
CACHE = Path("/tmp/we_grid_2026-10-13.pkl.gz")
RECS = Path("/tmp/we_asos_recs_2026-10-13.pkl.gz")


def load_grid():
    import gzip, pickle
    with gzip.open(CACHE, "rb") as f:
        return pickle.load(f)


def load_recs():
    import gzip, pickle
    with gzip.open(RECS, "rb") as f:
        return pickle.load(f)


def ceiling_of(res):
    return calibrated_ceiling(res)


def scan_p(rec, strike, lo, hi, sigma=PDS.SIGMA_RATE):
    """Best (largest) rate_1h p over mods in [lo, hi]."""
    b = rec["buckets"]
    if not b:
        return None
    mods = sorted(b)
    best = None
    for mod in range(lo, hi + 1, 15):
        if mod not in b:
            continue
        h = max(b[x] for x in mods if x <= mod)
        if h <= 0:
            continue
        rate, roll = PDS._rate_and_roll(b, mod)
        d = PDS.deflate(rate, roll)
        if d is None:
            continue
        p = PDS.p_ge(h + d, sigma, strike)
        if best is None or p > best:
            best = p
    return best


def rate_scan_scores(recs, truth, lo, hi, stations=None):
    """{(st, ds): (p, y)} for the rate_1h window scan, T anchored at the pin."""
    out = {}
    for (st, ds), rec in recs.items():
        if stations is not None and st not in stations:
            continue
        b = rec["buckets"]
        if not b:
            continue
        hsf_a = max([b[x] for x in b if x <= PDS.DECISION_PIN_MOD], default=None)
        if hsf_a is None:
            continue
        T = float(int(hsf_a) + 1.0)
        p = scan_p(rec, T, lo, hi)
        if p is None:
            continue
        settled = truth.get((st, ds))
        if settled is None:
            continue
        out[(st, ds)] = (p, 1 if settled > T else 0, T)
    return out


def decomp(scores):
    ps = [v[0] for v in scores.values()]
    ys = [v[1] for v in scores.values()]
    d = PDS.calibration_metrics(ps, ys)
    return {"n": len(ps), "brier": round(PDS.brier(ps, ys), 4),
            "reliability": d["reliability"], "resolution": d["resolution"],
            "uncertainty": d["uncertainty"], "ece": d["ece"],
            "ceiling": ceiling_of(d["resolution"])}


def main():
    grid = load_grid()
    recs = load_recs()
    truth = PDS.load_truth()

    # ---- (A) look-ahead test: bounded vs study scan -------------------------
    lookahead = {}
    for label, lo, hi in (("study_window_max_780_1005", 780, 1005),
                          ("to_15:00_scan_780_900", 780, 900),
                          ("bounded_<=pin_780_810", 780, 810),
                          ("single_point_810", 810, 810)):
        s = rate_scan_scores(recs, truth, lo, hi)
        lookahead[label] = decomp(s)

    # ---- (A2) per-station bounded (<= pin) resolution -----------------------
    per_station_rate = {}
    for st in PDS.STATIONS:
        s = rate_scan_scores(recs, truth, 780, 810, stations={st})
        if s:
            per_station_rate[st] = decomp(s)

    # ---- (B) the bridge's ACTUAL decision signal: fused member resolution ---
    # The live book fuses p_gefs / p_analog / p_trajectory / p_market, none of
    # which rate_1h feeds. The NWP member (gefs pressure) is the only one with a
    # full-population p in the grid; report its honest resolution + ceiling.
    rows = [r for r in grid.values()
            if r.get("gefs_p") is not None and r.get("y") is not None]
    gefs_all = decomp({i: (r["gefs_p"], r["y"]) for i, r in enumerate(rows)})
    per_station_gefs = {}
    for st in PDS.STATIONS:
        srows = [r for r in rows if r["station"] == st]
        if srows:
            per_station_gefs[st] = decomp(
                {i: (r["gefs_p"], r["y"]) for i, r in enumerate(srows)})

    # ---- pooled sigma sensitivity of the BOUNDED signal ---------------------
    sigma_sweep = {}
    for s in (0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 4.0):
        ps, ys = [], []
        for (st, ds), rec in recs.items():
            b = rec["buckets"]
            if not b:
                continue
            hsf_a = max([b[x] for x in b if x <= PDS.DECISION_PIN_MOD],
                        default=None)
            if hsf_a is None:
                continue
            T = float(int(hsf_a) + 1.0)
            p = scan_p(rec, T, 780, 810, sigma=s)
            settled = truth.get((st, ds))
            if p is None or settled is None:
                continue
            ps.append(p)
            ys.append(1 if settled > T else 0)
        d = PDS.calibration_metrics(ps, ys)
        sigma_sweep[s] = {"n": len(ps), "brier": round(PDS.brier(ps, ys), 4),
                          "reliability": d["reliability"],
                          "resolution": d["resolution"],
                          "ceiling": ceiling_of(d["resolution"])}

    res = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "signal": "rate_1h nowcast (window scan) + gefs_p (bridge NWP member)",
        "axis": "strike-crossing: T=floor(hsf@13:30 pin)+1, crossed = settled_max > T",
        "window": [PDS.MIGRATION_START, PDS.MIGRATION_END],
        "formula": {"CALIBRATION_FLOOR": CALIBRATION_FLOOR,
                    "MAX_CEILING": MAX_CEILING,
                    "RESOLUTION_FULL_SKILL": RESOLUTION_FULL_SKILL,
                    "expr": "0.75 + 0.24*min(1, resolution/0.05)"},
        "holdout_reference": {"resolution": HOLDOUT_RESOLUTION,
                              "ceiling": ceiling_of(HOLDOUT_RESOLUTION)},
        "A_lookahead_rate1h": lookahead,
        "A2_rate1h_bounded_per_station": per_station_rate,
        "bounded_sigma_sweep": sigma_sweep,
        "B_bridge_gefs_member": {
            "pooled": gefs_all, "per_station": per_station_gefs},
        "bridge_decision_members": ["p_gefs", "p_analog", "p_trajectory", "p_market"],
        "rate_1h_is_bridge_member": False,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, indent=2))

    print("(A) rate_1h look-ahead test (T anchored at pin):")
    for k, v in lookahead.items():
        print(f"  {k:28} n={v['n']:5d} brier={v['brier']:.4f} "
              f"res={v['resolution']:.4f} ceil={v['ceiling']}")
    print("\n(A2) per-station rate_1h resolution, BOUNDED to pin (780-810):")
    for st, v in per_station_rate.items():
        print(f"  {st}: res={v['resolution']:.4f} ceil={v['ceiling']:.4f} n={v['n']}")
    print("\n(bounded sigma sweep):")
    for s, v in sigma_sweep.items():
        print(f"  sigma={s}: brier={v['brier']} res={v['resolution']} ceil={v['ceiling']}")
    print("\n(B) bridge NWP member (gefs_p):")
    print(f"  pooled: {gefs_all}")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
