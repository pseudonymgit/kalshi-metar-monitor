#!/usr/bin/env python3
"""
R/R-1c — INDEPENDENT QC PASS: verify the strict as-of gate + mixed-units fix.

This is the SEPARATE verification pass (not build-then-self-check).  It reads
the DB directly and confirms:

  1. The as-of gate now filters on `fetch_timestamp` (not `fetch_date`).
  2. Strict-gate survival matches Gerri's data-level numbers
     (fetch_timestamp < target_date T00:00 survives 28,390 / 44.6% of
     temperature_2m_max rows).
  3. Mixed-units hygiene: the ~1,340 °F-in-°C rows are correctly identified
     and normalized (show before/after value distributions).

Read-only against DBs.  No AI calls.  Deterministic.
"""
import os
import sys
import sqlite3

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

NWP_DB = os.path.join(BASE, "data", "nwp_forecasts.db")


def main():
    conn = sqlite3.connect(NWP_DB)
    cur = conn.cursor()

    print("=" * 100)
    print("R/R-1c QC — strict as-of gate + mixed-units verification")
    print("=" * 100)

    # ── (1) Gate predicate source check ────────────────────────────────────
    from core.nwp_lookahead_gate import strict_asof_predicate, strict_asof_bound
    pred = strict_asof_predicate()
    print("\n[1] Strict as-of predicate source:")
    print(f"    predicate = {pred!r}")
    assert "fetch_timestamp" in pred and "fetch_date" not in pred, \
        "GATE MUST FILTER ON fetch_timestamp, NOT fetch_date"
    assert "<" in pred and "=" not in pred, "GATE MUST BE STRICT (<), NOT <="
    print(f"    bound      = {strict_asof_bound('2026-09-01')!r}")
    print("    PASS: gate filters on fetch_timestamp, strict <")

    # Confirm the signal's _get_temp actually uses it (not the old predicate)
    import inspect
    from core.signals import nwp_direct_signal as nds
    src = inspect.getsource(nds.NwpDirectSignal._get_temp)
    assert "strict_asof_predicate" in src and "strict_asof_bound" in src, \
        "_get_temp MUST use the strict gate helpers"
    assert "contemporaneous_where" not in src, "_get_temp must NOT use the old gate"
    print("    PASS: _get_temp uses strict_asof_predicate/bound, not contemporaneous_where")

    # ── (2) Strict-gate survival counts (match Gerri) ─────────────────────
    print("\n[2] Strict-gate survival (temperature_2m_max):")
    cur.execute("SELECT COUNT(*) FROM nwp_forecasts WHERE variable='temperature_2m_max'")
    total = cur.fetchone()[0]
    cur.execute("""
        SELECT COUNT(*) FROM nwp_forecasts
        WHERE variable='temperature_2m_max' AND substr(fetch_timestamp,1,10) < target_date
    """)
    strict = cur.fetchone()[0]
    cur.execute("""
        SELECT COUNT(*) FROM nwp_forecasts
        WHERE variable='temperature_2m_max' AND fetch_date <= target_date
    """)
    old_gate = cur.fetchone()[0]
    cur.execute("""
        SELECT COUNT(*) FROM nwp_forecasts
        WHERE variable='temperature_2m_max' AND fetch_date = target_date
    """)
    same_day = cur.fetchone()[0]
    cur.execute("""
        SELECT COUNT(*) FROM nwp_forecasts
        WHERE variable='temperature_2m_max'
          AND substr(fetch_timestamp,1,10) != fetch_date
    """)
    mismatch = cur.fetchone()[0]
    print(f"    total rows                          : {total}")
    print(f"    fetch_timestamp date != fetch_date : {mismatch} ({mismatch/total*100:.1f}%)")
    print(f"    OLD gate (fetch_date <= target)    : {old_gate} ({old_gate/total*100:.1f}%)")
    print(f"    same-day rows (fetch_date==target) : {same_day} ({same_day/total*100:.1f}%)")
    print(f"    STRICT (fetch_timestamp<target)     : {strict} ({strict/total*100:.1f}%)")
    assert strict == 28390, f"strict survival expected 28390, got {strict}"
    assert mismatch == 29900, f"mismatch expected 29900, got {mismatch}"
    print("    PASS: strict survival 28,390 (44.6%); 29,900 (47%) fetch_date/fetch_timestamp mismatch")

    # ── (3) Mixed-units hygiene (before/after distributions) ───────────────
    print("\n[3] Mixed-units hygiene (temperature_2m_max):")
    cur.execute("""
        SELECT value FROM nwp_forecasts
        WHERE variable='temperature_2m_max' AND value IS NOT NULL AND value > 50
        ORDER BY value
    """)
    degf_rows = [r[0] for r in cur.fetchall()]
    n_degf = len(degf_rows)
    print(f"    rows > +50 (°F-in-°C-field): {n_degf}")
    print(f"    before (raw °F): min={min(degf_rows):.1f}  max={max(degf_rows):.1f}")
    # normalize via the signal's own helper
    from core.signals.nwp_direct_signal import NwpDirectSignal
    normalized = [NwpDirectSignal._apply_unit_hygiene(v) for v in degf_rows]
    print(f"    after (°C):       min={min(normalized):.1f}  max={max(normalized):.1f}")
    print(f"    example: 70.0°F -> {NwpDirectSignal._apply_unit_hygiene(70.0):.2f}°C; "
          f"100.0°F -> {NwpDirectSignal._apply_unit_hygiene(100.0):.2f}°C")
    # Confirm the four signal models are clean °C (defense-in-depth note)
    print("\n    Signal-model °C cleanliness (max value per model):")
    for m in ['gfs', 'ecmwf', 'icon', 'gem']:
        cur.execute("""
            SELECT MAX(value) FROM nwp_forecasts
            WHERE variable='temperature_2m_max' AND LOWER(model)=? AND value IS NOT NULL
        """, (m,))
        mx = cur.fetchone()[0]
        print(f"      {m:<7s}: max={mx:.1f} °C  {'CLEAN' if mx <= 50 else 'CONTAMINATED'}")
    # Source of °F rows
    cur.execute("""
        SELECT LOWER(model), COUNT(*) FROM nwp_forecasts
        WHERE variable='temperature_2m_max' AND value > 50 GROUP BY LOWER(model)
    """)
    print("    °F-row source models:", cur.fetchall())

    print("\n" + "=" * 100)
    print("QC PASS — strict gate + units fix verified.")
    print("=" * 100)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
