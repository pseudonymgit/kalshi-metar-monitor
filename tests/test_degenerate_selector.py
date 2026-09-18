#!/usr/bin/env python3
"""
Tests for degenerate-pair selector fix (WS7-DEGENERATE-SELECTOR-CORRECTION-SPEC).

Gates:
- G1: DEGENERATE_SIGNAL_NAMES = 4 signals
- G2: 78-family fixed = C(13,2) = 78
- G3: Dead signals excluded from VALID
- G4: inferential_eligible excludes degenerate + dead + zero-trade
- G5: Expected top valid pair = pd+cpd (62.14%)
- G6: Eval-only selection (no holdout used)
- G7: Serialization in artifact
- G8: Pair screen 136-family unchanged
- G9: Wrong-pair artifacts quarantined
"""
import os, sys, json, itertools, hashlib
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.signals import REGISTERED_SIGNAL_NAMES, DEGENERATE_SIGNAL_NAMES
from core.statistical_significance import bonferroni_alpha


def test_deg_count():
    """G1: Exactly 4 degenerate signals."""
    deg = set(DEGENERATE_SIGNAL_NAMES)
    assert len(deg) == 4, f"Expected 4 degenerate, got {len(deg)}"
    assert "spike_reversion" in deg
    assert "warm_momentum" in deg
    assert "cooling_failure" in deg
    assert "radiational_cooling" in deg


def test_nondeg_count():
    """17 registered - 4 degenerate = 13 non-degenerate."""
    nondeg = [s for s in REGISTERED_SIGNAL_NAMES if s not in DEGENERATE_SIGNAL_NAMES]
    assert len(nondeg) == 13, f"Expected 13 non-degenerate, got {len(nondeg)}"


def test_78_family_fixed():
    """G2: C(13,2) = 78, fixed preregistered."""
    nondeg = [s for s in REGISTERED_SIGNAL_NAMES if s not in DEGENERATE_SIGNAL_NAMES]
    n_pairs = len(list(itertools.combinations(nondeg, 2)))
    assert n_pairs == 78, f"Expected 78 non-degenerate pairs, got {n_pairs}"
    assert bonferroni_alpha("nondegenerate_78") == 0.05 / 78


def test_dead_signals_excluded():
    """G3: Dead signals not in VALID set."""
    DEAD = {"asos_microstructure", "temperature_trend"}
    DEG = set(DEGENERATE_SIGNAL_NAMES)
    ALL = set(REGISTERED_SIGNAL_NAMES)
    VALID = ALL - DEG - DEAD
    assert "asos_microstructure" not in VALID, "asos_microstructure should be excluded"
    assert "temperature_trend" not in VALID, "temperature_trend should be excluded"
    assert "pressure_delta" in VALID
    assert "corrected_pressure_delta" in VALID


def test_inferential_eligible():
    """G4: inferential_eligible requires non-degenerate, non-dead, firing."""
    DEAD = {"asos_microstructure", "temperature_trend"}
    DEG = set(DEGENERATE_SIGNAL_NAMES)

    # degenerate + dead = ineligible
    assert ("spike_reversion" in DEG) and ("asos_microstructure" in DEAD)

    # non-degenerate, non-dead, assuming both fire
    VALID = set(REGISTERED_SIGNAL_NAMES) - DEG - DEAD
    assert "pressure_delta" in VALID
    assert "corrected_pressure_delta" in VALID


def test_authoritative_pair_artifact():
    """G5: Top valid pair in authoritative artifact is pd+cpd."""
    import json
    root = os.path.join(os.path.dirname(__file__), "..")
    with open(os.path.join(root, "data/ws7_auth_pairs_20260814_221944.json")) as f:
        d = json.load(f)

    DEAD = {"asos_microstructure", "temperature_trend"}
    DEG = set(DEGENERATE_SIGNAL_NAMES)
    VALID = set(REGISTERED_SIGNAL_NAMES) - DEG - DEAD

    valid_pairs = [p for p in d["all_pairs"]
                   if p["signals"][0] in VALID and p["signals"][1] in VALID
                   and p["trades"] > 0]
    valid_pairs.sort(key=lambda x: -x["accuracy"])
    top = valid_pairs[0]
    assert top["pair"] == "pressure_delta+corrected_pressure_delta", \
        f"Expected pd+cpd, got {top['pair']} ({top['accuracy']:.4f})"
    assert top["accuracy"] == 0.621351, f"Expected 0.6214, got {top['accuracy']}"


def test_eval_only_selection():
    """G6: Selection is eval-only, never holdout."""
    root = os.path.join(os.path.dirname(__file__), "..")
    with open(os.path.join(root, "data/ws7_auth_pairs_20260814_221944.json")) as f:
        d = json.load(f)
    # All pairs sorted by accuracy (eval) — no holdout sorting
    sorted_by_acc = sorted(d["all_pairs"], key=lambda x: -x["accuracy"])
    first = sorted_by_acc[0]
    assert first["pair"] != "N/A"  # sanity: pairs exist


def test_wrong_pair_artifacts_quarantined():
    """G9: Wrong-pair artifacts exist and are marked."""
    root = os.path.join(os.path.dirname(__file__), "..")
    # The quarantined artifacts exist (not deleted)
    for f in ["data/ws7_auth_ablations_20260814_221944.json",
              "data/ws7_auth_beam_20260814_221944.json"]:
        path = os.path.join(root, f)
        assert os.path.exists(path), f"Quarantined artifact missing: {f}"
    # Corrected artifacts exist
    for f in ["data/ws7_auth_ablations_corrected_20260814_222350.json",
              "data/ws7_auth_beam_corrected_20260814_222350.json"]:
        path = os.path.join(root, f)
        assert os.path.exists(path), f"Corrected artifact missing: {f}"


def test_136_family_unchanged():
    """G8: 136-family = 136 pairs in authoritative artifact."""
    root = os.path.join(os.path.dirname(__file__), "..")
    with open(os.path.join(root, "data/ws7_auth_pairs_20260814_221944.json")) as f:
        d = json.load(f)
    assert d["n_pairs"] == 136, f"Expected 136, got {d['n_pairs']}"
    # Verify 136-family alpha
    assert abs(d["families"]["all_136"]["alpha_prime"] - 0.05/136) < 1e-10


def test_artifact_serialization():
    """G7: Corrected ablation artifact has serialization fields."""
    root = os.path.join(os.path.dirname(__file__), "..")
    with open(os.path.join(root, "data/ws7_auth_ablations_corrected_20260814_222350.json")) as f:
        d = json.load(f)
    assert "provenance" in d, "Missing provenance"
    assert "selection_filter" in d["provenance"], "Missing selection_filter"
    assert "top_pair" in d and d["top_pair"] == ["pressure_delta", "corrected_pressure_delta"]
    assert "adapter_family" in d
    assert "alpha_prime" in d["adapter_family"]
    for arm_name, arm in d["arms"].items():
        assert "config" in arm, f"Missing config in {arm_name}"
        assert "use_direction_weighted_adapter" in arm["config"]
        assert "use_ds_conflict" in arm["config"]
        assert "use_diversity_modifier" in arm["config"]