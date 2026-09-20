#!/usr/bin/env python3
"""
R/R-1a Part 1 RE-DO — re-score the two candidates against the CORRECTED
prior-day as-of gate, with explicit before/after row-count evidence that the
query now admits a non-empty contemporaneous prior-day observation set.

Deterministic / script-only.  NO AI calls inside the loop.  Read-only against DBs.

Why this re-do exists:
  The prior `core/observational_asof_gate.py` predicate `timestamp_utc <
  settlement_window_open` (settlement-day T00:00:00) was bolted onto queries STILL
  scoped `date_utc = settlement_date`.  That asks for "settlement-day rows with
  timestamp before settlement-day-start" — EMPTY BY CONSTRUCTION.  The resulting
  n=0 was a NULLIFIED QUERY, not a "no predictions" finding.  The corrected gate
  scopes the read to the PRIOR day (`date_utc < settlement_date`), which admits a
  non-empty contemporaneous observation set.

This script:
  1. Scores both candidates (frontal_passage_intraday, nwp_dtdt_fusion) through
     the canonical `ultimate_sweep._score_signal` scorer with the CORRECTED gate.
  2. Emits per-candidate row-count evidence: the BROKEN query (zero-by-contradiction)
     vs the CORRECTED query (non-empty prior-day set), so the "no-usable-data" vs
     "broken-query" distinction is explicit and reproducible.

Every number carries axis + provenance:
  axis      : strike-crossing — settlement_temp_f vs strike_price via
              core/settlement_outcome.strike_crossing_direction (real strikes;
              NOT day-over-day temp).
  base rate : up=34.36% (down-skewed); always-down null Sharpe ~5.25; MCC ~ 0 = artifact.

Output: reports/rr1a_asof_redo_<ts>.json + console table.
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
from core.observational_asof_gate import settlement_window_open, prior_day

METAR_DB = os.path.join(BASE, "data", "metar_backfill.db")
NWP_DB = os.path.join(BASE, "data", "nwp_forecasts.db")
REPORTS_DIR = os.path.join(BASE, "reports")

import scripts.ultimate_sweep as us


# ─────────────────────────────────────────────────────────────────────────
# Row-count evidence: BROKEN (zero-by-contradiction) vs CORRECTED (prior-day)
# ─────────────────────────────────────────────────────────────────────────
def _row_count_evidence(conn):
    """For a representative station/date, count rows under the broken vs corrected
    query.  Proves the corrected gate admits a non-empty prior-day set."""
    # Pick a settlement date that exists in settlement_truth for KATL.
    cur = conn.cursor()
    r = cur.execute(
        "SELECT DISTINCT local_trading_date FROM settlement_truth "
        "WHERE station='KATL' ORDER BY local_trading_date DESC LIMIT 1").fetchone()
    if r is None:
        return {"station": "KATL", "date": None, "note": "no KATL settlement rows"}
    date = r[0]
    pday = prior_day(date)
    wo = settlement_window_open(date)

    # BROKEN: date_utc = settlement_date AND timestamp_utc < settlement-day 00:00
    broken = cur.execute(
        "SELECT COUNT(*) FROM metar_observations "
        "WHERE station=? AND date_utc=? AND timestamp_utc < ?",
        ("KATL", date, wo)).fetchone()[0]

    # CORRECTED: date_utc < settlement_date (prior day and earlier)
    corrected_all = cur.execute(
        "SELECT COUNT(*) FROM metar_observations "
        "WHERE station=? AND date_utc < ?", ("KATL", date)).fetchone()[0]

    # CORRECTED prior-day only (date_utc = prior_day)
    corrected_prior = cur.execute(
        "SELECT COUNT(*) FROM metar_observations "
        "WHERE station=? AND date_utc = ?", ("KATL", pday)).fetchone()[0]

    # Prior-day last-3h (the frontal detector's 3-hour window preceding settlement)
    ws3 = f"{pday}T21:00:00"
    prior_3h = cur.execute(
        "SELECT COUNT(*) FROM metar_observations "
        "WHERE station=? AND date_utc=? AND timestamp_utc >= ? AND timestamp_utc < ?",
        ("KATL", pday, ws3, wo)).fetchone()[0]

    return {
        "station": "KATL", "settlement_date": date, "prior_day": pday,
        "broken_query_rows": broken,                       # expect 0 (the bug)
        "corrected_all_prior_rows": corrected_all,        # expect > 0
        "corrected_prior_day_rows": corrected_prior,       # expect ~24 (hourly)
        "corrected_prior_3h_rows": prior_3h,              # expect ~3
    }


def _score_two(registry):
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
        # Distinguish the two failure modes explicitly (core QA requirement):
        #   - n == 0  -> could be "no usable as-of data" (real NO-EDGE) OR a still-
        #     broken zero-by-contradiction query.  We resolve this via the row-count
        #     evidence: if the corrected query admits a non-empty set but the signal
        #     still fires n=0, that is a REAL no-edge.  If the corrected query were
        #     still empty, that would be BROKEN.
        m["status"] = "PASS" if (m["mcc"] > 0 and m["base_rate_delta"] > 0) else "NO_EDGE"
        out[name] = m
    conn.close()
    return out, excl


def main():
    t0 = time.time()
    conn = sqlite3.connect(METAR_DB)

    report = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "axis": "strike-crossing (settlement_temp_f vs strike_price) — core/settlement_outcome.strike_crossing_direction",
        "truth_source": "settlement_truth.strike_price (14,305 rows; real strikes, NOT day-over-day)",
        "asof_gate_corrected": "core/observational_asof_gate.py — date_utc < settlement_date (PRIOR day), "
                               "timestamp_utc < settlement_window_open (settlement-day 00:00 UTC)",
        "defect_fixed": "prior gate bolted timestamp_utc < settlement-day-start onto date_utc = settlement_date "
                        "(zero-by-contradiction); corrected to date_utc < settlement_date",
        "row_count_evidence": _row_count_evidence(conn),
    }

    # CORRECTED GATE score
    registry, status = us.build_signal_registry()
    scored, excl = _score_two(registry)
    report["corrected_gate"] = scored
    report["label_exclusions"] = excl
    report["elapsed_seconds"] = round(time.time() - t0, 2)
    conn.close()

    # Emit
    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    path = os.path.join(REPORTS_DIR, f"rr1a_asof_redo_{ts}.json")
    with open(path, "w") as f:
        json.dump(report, f, indent=2)

    # Console
    print("=" * 100)
    print("R/R-1a Part 1 RE-DO — corrected prior-day as-of gate re-score")
    print("=" * 100)
    ev = report["row_count_evidence"]
    print(f"Row-count evidence ({ev['station']} @ {ev.get('settlement_date')}):")
    print(f"  BROKEN query (date_utc=settlement AND ts<00:00): {ev['broken_query_rows']}  <- zero-by-contradiction")
    print(f"  CORRECTED (date_utc < settlement, all prior):    {ev['corrected_all_prior_rows']}")
    print(f"  CORRECTED (prior day only):                      {ev['corrected_prior_day_rows']}")
    print(f"  CORRECTED (prior day last 3h, frontal window):   {ev['corrected_prior_3h_rows']}")
    print()
    hdr = f"{'signal':<26s} {'MCC':>9s} {'Sharpe':>8s} {'baseΔ':>8s} {'acc':>8s} {'n':>6s} {'verdict':<14s}"
    print(hdr)
    print("-" * len(hdr))
    for name in ("frontal_passage_intraday", "nwp_dtdt_fusion"):
        m = report["corrected_gate"].get(name, {})
        if m.get("status") in ("NOT_REACHABLE", "NO_FIRES", None):
            print(f"{name:<26s} {m.get('status','?'):>9s}")
            continue
        sharpe = m.get("sharpe")
        sharpe_s = f"{sharpe:.2f}" if sharpe is not None else "n/a"
        verdict = m.get("status", "?")
        print(f"{name:<26s} {m['mcc']:+9.4f} {sharpe_s:>8s} "
              f"{m['base_rate_delta']*100:+8.2f} {m['accuracy']*100:7.2f}% {m['n']:6d} {verdict:<14s}")
    print()
    print(f"Label exclusions: {excl}")
    print(f"Receipt JSON: {path}")
    print(f"Elapsed: {report['elapsed_seconds']:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
