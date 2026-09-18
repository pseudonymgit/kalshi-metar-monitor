#!/usr/bin/env python3
"""
WS7 scope acceptance test: full-20 diagnostic universe, tier labels separate.

Tests:
1. station_list == all 20 canonical stations
2. TIER-1 labels: KDEN, KLAX, KMDW only
3. TIER-2 labels: 11 stations per canonical list
4. Powered labels separated from diagnostic universe
5. v2 artifacts use all 20 stations
"""
import os
import sys
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.sweep.config import STATIONS as SWEEP_STATIONS
from scripts.l2_shared import get_eligible_stations
from scripts.ws5_common import load_settlement_data


def test_diagnostic_universe_full_20():
    """All 20 canonical stations in diagnostic universe."""
    assert len(SWEEP_STATIONS) == 20, f"Expected 20, got {len(SWEEP_STATIONS)}"


def test_tier1_stations():
    """TIER-1: KDEN, KLAX, KMDW (>=50 both eval folds)."""
    settlements = load_settlement_data(
        os.path.join(os.path.dirname(__file__), "..", "data", "kalshi_settlements.db")
    )
    from scripts.l2_shared import L2_FOLDS
    tier1 = []
    for stn in SWEEP_STATIONS:
        if stn not in settlements:
            continue
        dates = sorted(settlements[stn].keys())
        counts = [sum(1 for d in dates if fd["eval_start"] <= d <= fd["eval_end"]) for fd in L2_FOLDS]
        if all(c >= 50 for c in counts):
            tier1.append(stn)
    assert sorted(tier1) == ["KDEN", "KLAX", "KMDW"], f"Got {sorted(tier1)}"


def test_tier2_stations():
    """TIER-2: >=20 both eval folds -> 11 stations."""
    settlements = load_settlement_data(
        os.path.join(os.path.dirname(__file__), "..", "data", "kalshi_settlements.db")
    )
    t2 = get_eligible_stations(settlements, "TIER-2")
    assert len(t2) == 11, f"Expected 11, got {len(t2)}"
    assert "KDEN" in t2 and "KLAX" in t2 and "KMDW" in t2


def test_powered_labels_separate():
    """Powered claims (TIER-1/2) are separate from diagnostic universe."""
    diag = set(SWEEP_STATIONS)
    settlements = load_settlement_data(
        os.path.join(os.path.dirname(__file__), "..", "data", "kalshi_settlements.db")
    )
    t2 = set(get_eligible_stations(settlements, "TIER-2"))
    assert t2.issubset(diag), "TIER-2 must be subset of diagnostic universe"
    assert len(diag - t2) == 9, f"Expected 9 diagnostic-only, got {len(diag - t2)}"


def test_v2_artifacts_all_20():
    """v2 artifacts declare full-20 diagnostic universe."""
    for fname in ["data/ws7_v2_pairs_20260814_213738.json",
                  "data/ws7_v2_ablations2_20260814_213738.json",
                  "data/ws7_v2_beam2_20260814_213738.json"]:
        with open(os.path.join(os.path.dirname(__file__), "..", fname)) as f:
            d = json.load(f)
        assert d.get("diagnostic_universe") == "full_20", f"{fname} missing full_20"


def test_tier_labels_in_artifacts():
    """v2 artifacts contain tier definitions."""
    with open(os.path.join(os.path.dirname(__file__), "..",
                           "data/ws7_v2_ablations2_20260814_213738.json")) as f:
        d = json.load(f)
    assert "tier_definitions" in d, "Missing tier_definitions"
    assert "TIER-1" in d["tier_definitions"]
    assert "TIER-2" in d["tier_definitions"]
    assert d["tier_definitions"]["TIER-1"] == ["KDEN", "KLAX", "KMDW"]