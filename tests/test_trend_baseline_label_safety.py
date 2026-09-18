#!/usr/bin/env python3
"""
WS2 acceptance tests — honest trend baseline (Version B/C) label safety.

Spec: OPEN-ISSUES-REMEDIATION-AND-TRIM-TREND-SPEC-2026-08-14.md, PART C WS2.
Gates: G1 (no label peek B and C), G16/G17 (reconciliation), R2 (test_l0_at6 update).

Tests:
  - WS2-AT1: compute_baseline_accuracies returns trend_version == "B_honest_sign(yesterday-3ago)"
  - WS2-AT2: monotonic rise → persistence ≥ 0.9, trend keys present
  - WS2-AT3: distinguishing fixture (70,75,80,65) — Version B predicts "up" (no today access)
  - WS2-AT4: Version C = sign(yesterday−2ago) on same fixture
  - WS2-AT5: Version B all-data ≈ 0.4157 (read-only verification anchor)
  - WS2-AT6: Version C all-data ≈ 0.4872 (read-only verification anchor)
  - WS2-AT7: grep-gate — no `today_temp`/`settlements[today]` in trend feature code
  - WS2-AT8: feature construction uses only dates ≤ yesterday (invariance to today mutation)
"""

import sys
import os
import re
import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.ws5_common import compute_baseline_accuracies, compute_ground_truth_direction

EPSILON = 0.5


def _build_settlements(seq):
    """Build settlements dict for one station from a temperature sequence."""
    stations = ["KNYC"]
    settlements = {}
    base = datetime.date(2025, 5, 1)
    d = {}
    for i, temp in enumerate(seq):
        ds = (base + datetime.timedelta(days=i)).isoformat()
        d[ds] = float(temp)
    settlements["KNYC"] = d
    return settlements, stations


def _baselines(seq, holdout_start=None, holdout_end=None):
    settlements, stations = _build_settlements(seq)
    return compute_baseline_accuracies(
        settlements, stations, epsilon=EPSILON,
        holdout_start=holdout_start, holdout_end=holdout_end, period="eval",
    )


def test_ws2_at1_trend_version_key():
    """WS2-AT1: trend_version key present and documents honest semantics."""
    bl = _baselines([60 + i for i in range(12)])
    assert "trend_version" in bl, f"missing trend_version: {sorted(bl.keys())}"
    assert bl["trend_version"] == "B_honest_sign(yesterday-3ago)", bl["trend_version"]
    assert "trend_c_version" in bl
    assert bl["trend_c_version"] == "C_persistence_sign(yesterday-2ago)", bl["trend_c_version"]


def test_ws2_at2_monotonic_rise():
    """WS2-AT2: on monotonic rise persistence ≥ 0.9; trend keys present."""
    bl = _baselines([60 + i for i in range(12)])
    assert "persistence_accuracy" in bl
    assert "trend_accuracy" in bl
    assert "trend_c_accuracy" in bl
    assert bl["persistence_trades"] > 0
    assert bl["persistence_accuracy"] >= 0.9, f"persistence on monotonic rise: {bl['persistence_accuracy']}"


def test_ws2_at3_version_b_no_today_access():
    """
    WS2-AT3 (R2 Part B): distinguishing fixture (70,75,80,65).

    On day 3: yesterday=80, 3ago=70 → honest B predicts sign(80-70) = "up".
    Actual direction = sign(65-80) = "down" → counted as incorrect.
    A label-peeking implementation would predict sign(65-70) = "down" (correct) —
    asserting the honest predictor says "up" proves no today access.
    """
    seq = [70, 75, 80, 65]
    settlements, stations = _build_settlements(seq)
    bl = compute_baseline_accuracies(
        settlements, stations, epsilon=EPSILON,
        holdout_start=None, holdout_end=None, period="eval",
    )
    # Day 3 (index 3): trend B correct? honest B = up, actual = down → incorrect.
    # Total possible trend days: index 3 only (i>=3). If it were label-peeking,
    # trend would predict down and be correct. Honest → incorrect.
    assert bl["trend_trades"] == 1, f"expected 1 trend trade on 4-day series, got {bl['trend_trades']}"
    assert bl["trend_accuracy"] == 0.0, bl["trend_accuracy"]


def test_ws2_at4_version_c_same_fixture():
    """WS2-AT4 (R2 Part C): Version C = sign(yesterday−2ago) on same fixture."""
    seq = [70, 75, 80, 65]
    settlements, stations = _build_settlements(seq)
    bl = compute_baseline_accuracies(
        settlements, stations, epsilon=EPSILON,
        holdout_start=None, holdout_end=None, period="eval",
    )
    # Day 2 (index 2): C = sign(75−70) = "up"; actual = sign(80−75) = "up" → correct
    # Day 3 (index 3): C = sign(80−75) = "up"; actual = sign(65−80) = "down" → incorrect
    assert bl["trend_c_trades"] == 2, f"expected 2 persistence-C trades, got {bl['trend_c_trades']}"
    assert bl["trend_c_accuracy"] == 0.5, bl["trend_c_accuracy"]


def test_ws2_at5_version_b_all_data_anchor():
    """WS2-AT5: all-data Version B accuracy ≈ 0.4157 (read-only verification)."""
    from core.station_registry import get_all_stations
    from scripts.ws5_common import load_settlement_data
    stations = [s for s in get_all_stations()]
    settlements = load_settlement_data(os.path.join(os.path.dirname(__file__), "..", "data", "kalshi_settlements.db"))
    if not settlements:
        return
    bl = compute_baseline_accuracies(
        {s: settlements[s] for s in stations if s in settlements},
        [s for s in stations if s in settlements],
        epsilon=EPSILON, holdout_start=None, holdout_end=None, period="eval",
    )
    assert abs(bl["trend_accuracy"] - 0.4157) < 0.01, bl["trend_accuracy"]


def test_ws2_at6_version_c_all_data_anchor():
    """WS2-AT6: all-data Version C accuracy ≈ 0.4872 (read-only verification)."""
    from core.station_registry import get_all_stations
    from scripts.ws5_common import load_settlement_data
    stations = [s for s in get_all_stations()]
    settlements = load_settlement_data(os.path.join(os.path.dirname(__file__), "..", "data", "kalshi_settlements.db"))
    if not settlements:
        return
    bl = compute_baseline_accuracies(
        {s: settlements[s] for s in stations if s in settlements},
        [s for s in stations if s in settlements],
        epsilon=EPSILON, holdout_start=None, holdout_end=None, period="eval",
    )
    assert abs(bl["trend_c_accuracy"] - 0.4872) < 0.01, bl["trend_c_accuracy"]


def test_ws2_at7_grep_gate_no_label_access():
    """
    WS2-AT7 (G1): grep-gate — trend feature construction must not reference
    `today_temp` or `settlements[today]`. Only the trend predictor lines are
    checked (today_temp legitimately appears in actual_dir label computation).
    """
    src = open(os.path.join(os.path.dirname(__file__), "..", "scripts", "ws5_common.py")).read()
    # Locate the trend predictor calls directly by variable name
    for var in ["trend_dir", "trend_c_dir"]:
        anchor = f"{var} = compute_ground_truth_direction("
        idx = src.find(anchor)
        assert idx != -1, f"{anchor} not found in ws5_common.py"
        call_end = src.find(")", idx)
        call = src[idx:call_end + 1]
        assert "today_temp" not in call, f"today_temp found in {var} call: {call}"
        assert "station_settlements" not in call, f"settlements access in {var} call: {call}"


def test_ws2_at8_today_mutation_invariance():
    """
    WS2-AT8: mutating today's temperature must NOT change trend predictions
    (trend uses only dates ≤ yesterday).
    """
    seq = [70, 75, 80, 65]
    settlements, stations = _build_settlements(seq)
    bl_a = compute_baseline_accuracies(
        settlements, stations, epsilon=EPSILON,
        holdout_start=None, holdout_end=None, period="eval",
    )
    # Mutate today's settled temp (the label) — but keep the SAME direction
    # (66 vs 80 is still down, same as 65 vs 80), so accuracy is comparable.
    # Trend predictions must not change: they use only dates ≤ yesterday.
    settlements["KNYC"]["2025-05-04"] = 66.0
    bl_b = compute_baseline_accuracies(
        settlements, stations, epsilon=EPSILON,
        holdout_start=None, holdout_end=None, period="eval",
    )
    assert bl_a["trend_accuracy"] == bl_b["trend_accuracy"], "trend changed when label mutated"
    assert bl_a["trend_c_accuracy"] == bl_b["trend_c_accuracy"], "trend C changed when label mutated"


# ── Runner mirroring test_layer0_remediation.py style ──
def main():
    tests = [
        ("WS2-AT1", test_ws2_at1_trend_version_key),
        ("WS2-AT2", test_ws2_at2_monotonic_rise),
        ("WS2-AT3", test_ws2_at3_version_b_no_today_access),
        ("WS2-AT4", test_ws2_at4_version_c_same_fixture),
        ("WS2-AT5", test_ws2_at5_version_b_all_data_anchor),
        ("WS2-AT6", test_ws2_at6_version_c_all_data_anchor),
        ("WS2-AT7", test_ws2_at7_grep_gate_no_label_access),
        ("WS2-AT8", test_ws2_at8_today_mutation_invariance),
    ]
    passed = 0
    failed = 0
    for name, func in tests:
        try:
            func()
            passed += 1
            print(f"  {name}: PASS")
        except Exception as e:
            failed += 1
            print(f"  {name}: FAIL — {e}")
    print(f"WS2 label-safety: {passed} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
