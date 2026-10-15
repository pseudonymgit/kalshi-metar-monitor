#!/usr/bin/env python3
"""refit_ceiling_shipped_2026-10-15.py — re-derive the per-station decision-time
ceilings from the SHIPPED signal's own calibration, and emit:
  * artifacts/ceiling-refit/ceiling_refit_omgfs_2026-10-15.json
        per-station caps read by core/intraday/honest_calibration.station_ceilings()
  * tests/fixtures/honest_cohort_2026-10-15.json
        compact (p_raw, ceiling, y) fixture for the no-harm regression test

Why (2026-10-15 calibration repair): the shipped `honest_calibrated_p` clamps to
a per-station ceiling refit from **rate_1h's** Brier resolution over a 0.75 floor
(ceiling_refit_2026-10-13.json). rate_1h has no skill on the crossing axis, so
those ceilings (0.768–0.903) clamp 1898/2927 shipping rows by a mean 0.156 and
degrade Brier from 0.1067 to 0.1234. The shipped signal (om_gfs bias-corrected
per-station, the sole HIGH forecast) is already well-calibrated at the top
(top decile mean_p 0.9839 vs empirical 0.9744), so the honest cap is
near-identity: only bind where the signal is demonstrably overconfident.

Cap per station = min(0.99, top_decile_emp_rate + 2*SE), top decile = p_raw>=0.9.
Pooled fallback uses the same formula on the pooled cohort.

READ-ONLY on every book. Reuses the frozen honest-study caches in /tmp.

usage: python3 scripts/refit_ceiling_shipped_2026-10-15.py
"""
from __future__ import annotations

import gzip
import importlib.util as ilu
import json
import math
import pickle
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

_spec = ilu.spec_from_file_location(
    "hsa", REPO / "scripts" / "honest_signal_accuracy_2026-10-14.py")
HSA = ilu.module_from_spec(_spec)
_spec.loader.exec_module(HSA)
PDS = HSA.PDS

GRID = Path("/tmp/we_grid_2026-10-13.pkl.gz")
RECS = Path("/tmp/we_asos_recs_2026-10-13.pkl.gz")
OM = Path("/tmp/honest_omgfs_cache_2026-10-14.json")
PIN = 810
TOP = 0.90

OUT_JSON = (REPO / "artifacts" / "ceiling-refit"
            / "ceiling_refit_omgfs_2026-10-15.json")
FIXTURE = REPO / "tests" / "fixtures" / "honest_cohort_2026-10-15.json"


def _cap(ps, ys):
    hi = [y for p, y in zip(ps, ys) if p >= TOP]
    n = len(hi)
    if n == 0:
        return 0.99
    r = sum(hi) / n
    se = math.sqrt(max(r * (1 - r), 1e-9) / n)
    return round(min(0.99, max(0.90, r + 2 * se)), 4)


def main():
    grid = pickle.load(gzip.open(GRID, "rb"))
    recs = pickle.load(gzip.open(RECS, "rb"))
    om = {(k.split("|")[0], k.split("|")[1]): v
          for k, v in json.loads(OM.read_text()).items()}
    rows = HSA.build_rows(recs, grid, om)

    res = defaultdict(list)
    for r in rows.values():
        if r["om_raw"] is not None and r["settled_max"] is not None:
            res[r["station"]].append(r["om_raw"] - r["settled_max"])
    allb = [v for lst in res.values() for v in lst]
    omstats = {
        "bias_global": HSA._mean(allb), "sigma_global": HSA._std(allb),
        "bias_st": {st: HSA._mean(v) for st, v in res.items() if len(v) >= 3},
        "sigma_st": {st: HSA._std(v) for st, v in res.items() if len(v) >= 3},
    }

    per = defaultdict(list)
    for (st, ds), row in rows.items():
        if row["y"] is None:
            continue
        p = HSA.signal_p_at(row, PIN, "om_gfs_bc_ps", omstats, {PIN: {}})
        if p is None or row["pins"][PIN]["T"] is None:
            continue
        per[st].append((ds, float(p), float(row["y"])))

    caps = {st: _cap([p for _, p, _ in v], [y for _, _, y in v])
            for st, v in sorted(per.items())}
    pooled = _cap([p for st in per for _, p, _ in per[st]],
                  [y for st in per for _, _, y in per[st]])

    artifact = {
        "generated_by": "scripts/refit_ceiling_shipped_2026-10-15.py",
        "signal": "om_gfs_bc_ps (shipped HIGH sole forecast, 13:30 pin)",
        "cohort_n": sum(len(v) for v in per.values()),
        "method": ("per-station cap = min(0.99, top-decile(p>=0.9) empirical "
                   "crossing rate + 2*SE); near-identity for the shipped signal"),
        "pooled_ceiling": pooled,
        "A2_omgfs_topbin_capped_per_station": {
            st: {"ceiling": caps[st],
                 "n": len(per[st]),
                 "top_decile_n": sum(1 for _, p, _ in per[st] if p >= TOP)}
            for st in caps},
        "legacy_artifact": "ceiling_refit_2026-10-13.json (rate_1h-derived; provenance only)",
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(artifact, indent=2))

    fixture = {
        "generated_by": "scripts/refit_ceiling_shipped_2026-10-15.py",
        "signal": "om_gfs_bc_ps @ 13:30 pin (shipped HIGH sole forecast)",
        "cohort_n": artifact["cohort_n"],
        "rows": [[round(p, 4), caps[st], y] for st in sorted(per)
                 for _, p, y in per[st]],
    }
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(fixture))

    print(json.dumps({"pooled_ceiling": pooled,
                      "n_stations": len(caps),
                      "caps": caps,
                      "cohort_n": artifact["cohort_n"]}, indent=2))
    print("wrote", OUT_JSON)
    print("wrote", FIXTURE)


if __name__ == "__main__":
    main()
