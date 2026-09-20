#!/usr/bin/env python3
"""
R/R-1a re-score — post-gate candidate re-validation on the corrected strike-crossing
axis, with the observational-intraday as-of gate (core/observational_asof_gate.py)
applied at READ TIME.

Deterministic / script-only.  NO AI calls inside the loop.  Read-only against DBs.

Scores the two R/T-2 "genuine edge" candidates:
  - frontal_passage_intraday
  - nwp_dtdt_fusion        (whose METAR leg = metar_dtdt is now as-of gated)

…through the canonical `ultimate_sweep._score_signal` scorer, in TWO modes:

  1. POST-GATE  (the fixed code, as committed) — as-of predicate active.
  2. PRE-GATE   (baseline reproduction of the R/R-1 leak) — the as-of predicate
                is neutralized to reproduce the pre-fix lookahead behavior, so the
                pre-vs-post collapse can be measured directly.

Every emitted number carries axis + provenance:
  axis      : strike-crossing — settlement_temp_f vs strike_price via
              core/settlement_outcome.strike_crossing_direction (real strikes;
              NOT day-over-day temp).
  base rate : up=34.36% (down-skewed); always-down null Sharpe ~5.25; MCC ~ 0 = artifact.

Output: reports/rr1a_rescore_<ts>.json + console table.
"""
import contextlib
import io
import json
import os
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core.settlement_outcome import strike_crossing_direction
from core.observational_asof_gate import settlement_window_open

METAR_DB = os.path.join(BASE, "data", "metar_backfill.db")
NWP_DB = os.path.join(BASE, "data", "nwp_forecasts.db")
REPORTS_DIR = os.path.join(BASE, "reports")


# ─────────────────────────────────────────────────────────────────────────
# Reuse the canonical scorer
# ─────────────────────────────────────────────────────────────────────────
import scripts.ultimate_sweep as us


def _score_two(registry):
    """Score frontal_passage_intraday + nwp_dtdt_fusion via canonical scorer."""
    labels, src_map, meta, excl = us.load_labels()
    conn = sqlite3.connect(METAR_DB)
    out = {}
    for name in ("frontal_passage_intraday", "nwp_dtdt_fusion"):
        sig = registry[name]
        if sig is None:
            out[name] = {"status": "NOT_REACHABLE"}
            continue
        with contextlib.redirect_stderr(io.StringIO()):
            m = us._score_signal(sig, name, labels, meta, conn)
        m["status"] = "PASS" if (m["mcc"] > 0 and m["base_rate_delta"] > 0) else "NO_EDGE"
        out[name] = m
    conn.close()
    return out


def _build_registry():
    """Build the canonical registry (post-gate code, as committed)."""
    registry, status = us.build_signal_registry()
    return registry, status


def _pre_gate_mode():
    """
    Neutralize the as-of gate to reproduce the PRE-FIX (leaky) behavior.

    The gate is enforced two ways:
      1. In `_load_intraday_obs` (frontal) and `metar_dtdt.evaluate_for_station`
         via a `timestamp_utc < ?` SQL predicate bound to `settlement_window_open(date)`.
      2. In `frontal._load_intraday_obs` via a belt-and-suspenders `is_asof_safe` check.

    To reproduce the leak without touching the DB, we monkeypatch the module-level
    `settlement_window_open` import used by those modules so the predicate admits the
    full settlement day (returns '9999-12-31T00:00:00', i.e. never excludes).
    """
    import core.signals.frontal_passage_intraday_signal as fp
    import core.signals.metar_dtdt_signal as md
    # Far-future window open => `timestamp_utc < window_open` admits everything.
    fp.settlement_window_open = lambda d: "9999-12-31T00:00:00"
    md.settlement_window_open = lambda d: "9999-12-31T00:00:00"
    # The belt-and-suspenders `is_asof_safe` also needs neutralizing.
    fp.is_asof_safe = lambda ts, d: True


def main():
    t0 = time.time()
    report = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "axis": "strike-crossing (settlement_temp_f vs strike_price) — core/settlement_outcome.strike_crossing_direction",
        "truth_source": "settlement_truth.strike_price (14,305 rows; real strikes, NOT day-over-day)",
        "asof_gate": "core/observational_asof_gate.py — timestamp_utc < settlement_window_open (settlement day 00:00 UTC)",
    }

    # POST-GATE
    registry, status = _build_registry()
    report["post_gate"] = _score_two(registry)

    # PRE-GATE (leak reproduced)
    _pre_gate_mode()
    registry2, status2 = _build_registry()
    report["pre_gate"] = _score_two(registry2)

    report["elapsed_seconds"] = round(time.time() - t0, 2)

    # Emit
    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    path = os.path.join(REPORTS_DIR, f"rr1a_rescore_{ts}.json")
    with open(path, "w") as f:
        json.dump(report, f, indent=2)

    # Console table
    print("=" * 100)
    print("R/R-1a re-score — candidate edge pre-gate vs post-gate (corrected strike-crossing axis)")
    print("=" * 100)
    print(f"as-of gate: {report['asof_gate']}")
    print()
    hdr = f"{'signal':<26s} {'mode':<9s} {'MCC':>9s} {'Sharpe':>8s} {'baseΔ':>8s} {'acc':>8s} {'n':>6s}"
    print(hdr)
    print("-" * len(hdr))
    for name in ("frontal_passage_intraday", "nwp_dtdt_fusion"):
        for mode in ("pre_gate", "post_gate"):
            m = report[mode].get(name, {})
            if m.get("status") in ("NOT_REACHABLE", "NO_FIRES", None):
                print(f"{name:<26s} {mode:<9s} {m.get('status','?'):>9s}")
                continue
            sharpe = m.get("sharpe")
            sharpe_s = f"{sharpe:.2f}" if sharpe is not None else "n/a"
            print(f"{name:<26s} {mode:<9s} {m['mcc']:+9.4f} {sharpe_s:>8s} "
                  f"{m['base_rate_delta']*100:+8.2f} {m['accuracy']*100:7.2f}% {m['n']:6d}")
    print()
    print(f"Receipt JSON: {path}")
    print(f"Elapsed: {report['elapsed_seconds']:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
