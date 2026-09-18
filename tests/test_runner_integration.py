#!/usr/bin/env python3
"""
Runner integration test for matched-outcome correction.

Verifies that ws7_ablation_beam_corrected.py produces artifacts with:
- true_mcnemar_comparison fields (b, c, n_matched, p_value, direction)
- No paired_discordants / _paired fields (removed)
- fold_id, abstained, rejection_reason in prediction records
- T2/T3/T0/T11 capture predictions
"""
import os, sys, json, subprocess

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

WE = os.path.join(os.path.dirname(__file__), "..")


def test_runner_imports():
    """Verify the runner module can be imported and key functions exist."""
    sys.path.insert(0, WE)
    from scripts.ws7_ablation_beam_corrected import run_arm_integer
    # Verify function has capture_preds parameter
    import inspect
    sig = inspect.signature(run_arm_integer)
    assert "capture_preds" in sig.parameters
    assert "capture_predictions" not in sig.parameters  # it's in the config, not the function


def test_no_old_aggregate():
    """Verify the runner no longer contains paired_discordants or _paired fields."""
    with open(os.path.join(WE, "scripts/ws7_ablation_beam_corrected.py")) as f:
        content = f.read()
    assert "paired_discordants" not in content, "Old aggregate function still present"
    assert "_t3_vs_t2_paired" not in content, "Old _paired field still present"
    assert "true_mcnemar_comparison" in content, "True McNemar function not imported"
    assert "_t3_vs_t2_matched" in content, "Matched field not serialized"
    assert "_t11_vs_t0_matched" in content, "T11 matched field not serialized"


def test_all_call_sites_use_3_return():
    """All run_arm_integer call sites must use 3-return (stats, res, preds)."""
    with open(os.path.join(WE, "scripts/ws7_ablation_beam_corrected.py")) as f:
        content = f.read()
    # All call sites should have three-way unpacking
    lines = content.split("\n")
    call_sites = [l for l in lines if "run_arm_integer(" in l and "def " not in l]
    for line in call_sites:
        assert "= run_arm_integer" in line, f"Call site not assigned: {line.strip()}"
        # Check it's three-way unpacking (contains two commas after =)
        rhs = line.split("=")[-1].strip()
        lhs = line.split("=")[0].strip()
        # Count unpacking targets
        # Simple check: stats, res, preds = or stats, _, _ = or stats, _, _ =
        assert ", " in lhs, f"Call site not unpacking: {line.strip()}"
        n_targets = len(lhs.split(","))
        assert n_targets == 3, f"Call site has {n_targets} targets, expected 3: {line.strip()}"


def test_true_mcnemar_fields():
    """Verify the true_mcnemar_comparison function produces correct fields."""
    from core.statistical_significance import true_mcnemar_comparison

    # Build minimal test predictions
    t2_preds = []
    t3_preds = []
    import datetime
    stn = "KMDW"
    for i in range(10):
        d = f"2026-01-{i+1:02d}"
        t2_preds.append({"station": stn, "date": d, "fold_id": 3,
                         "pred_dir": "up", "actual_dir": "up", "correct": True,
                         "abstained": False, "rejection_reason": None})
        t3_preds.append({"station": stn, "date": d, "fold_id": 3,
                         "pred_dir": "up" if i < 7 else "down",
                         "actual_dir": "up", "correct": i < 7,
                         "abstained": False, "rejection_reason": None})

    result = true_mcnemar_comparison(t3_preds, t2_preds, "T3", "T2")
    assert "b" in result
    assert "c" in result
    assert "n_matched" in result
    assert "p_value" in result
    assert "direction" in result
    assert "n_arm1_only" in result
    assert "n_arm2_only" in result
    assert "n_arm1_correct_arm2_abstained" in result
    # 10 matched, T3 correct 7, T2 correct 10 (all)
    # b = T3 correct & T2 wrong = 0 (T2 never wrong since all T2 correct)
    # c = T3 wrong & T2 correct = 3 (T3 wrong on 3 where T2 correct)
    assert result["b"] == 0
    assert result["c"] == 3
    assert result["n_matched"] == 10
    assert result["direction"] == "T2 better"


def test_abstention_fields():
    """Verify abstention records are handled correctly."""
    from core.statistical_significance import true_mcnemar_comparison

    t3_preds = [{"station": "KMDW", "date": "2026-01-01", "fold_id": 3,
                 "pred_dir": "up", "actual_dir": "up", "correct": True,
                 "abstained": False, "rejection_reason": None},
                {"station": "KMDW", "date": "2026-01-02", "fold_id": 3,
                 "pred_dir": "up", "actual_dir": "up", "correct": True,
                 "abstained": False, "rejection_reason": None}]
    t2_preds = [{"station": "KMDW", "date": "2026-01-01", "fold_id": 3,
                 "pred_dir": "up", "actual_dir": "up", "correct": True,
                 "abstained": False, "rejection_reason": None},
                {"station": "KMDW", "date": "2026-01-02", "fold_id": 3,
                 "pred_dir": None, "actual_dir": "up", "correct": False,
                 "abstained": True, "rejection_reason": "no_signal_fired"}]

    result = true_mcnemar_comparison(t3_preds, t2_preds, "T3", "T2")
    assert result["n_matched"] == 2  # both keys present
    assert result["b"] == 0  # 2026-01-01: both correct
    assert result["c"] == 0  # no discordant (2026-01-02: T2 abstained, excluded from b/c)
    assert result["n_arm1_correct_arm2_abstained"] == 1  # T3 correct, T2 abstained


def test_fold_id_in_predictions():
    """Verify the evaluator includes fold_id in prediction records."""
    from scripts.l2_shared import L2Evaluator, L2ExperimentConfig

    eval_ready = L2Evaluator()
    eval_ready.load_data()

    cfg = L2ExperimentConfig(
        signal_names=["pressure_delta"],
        station_names=["KMDW"],
        experiment_name="fold_id_test",
        capture_predictions=True,
    )
    result = eval_ready.evaluate_config(cfg)
    r = result.get("result", result) if isinstance(result, dict) else result
    fr = r.get("fold_results", []) if isinstance(r, dict) else []
    found_fold_id = False
    for fd in fr:
        preds = fd.get("eval", {}).get("predictions", [])
        for pred in preds[:5]:
            assert "fold_id" in pred, f"Missing fold_id in prediction: {pred}"
            assert pred["fold_id"] in (3, 4), f"Unexpected fold_id: {pred['fold_id']}"
            assert "abstained" in pred, f"Missing abstained in prediction"
            found_fold_id = True
    assert found_fold_id, "No predictions found with fold_id"