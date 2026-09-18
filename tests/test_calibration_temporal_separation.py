from copy import deepcopy

import pytest

from scripts.ws5_common import (
    CALIBRATION_CUTOFF_MAX,
    compute_fusion_accuracy,
    load_historical_calibration_labels,
)


class _Signal:
    min_lookback = 0

    def evaluate(self, idx, days):
        return "up", 0.8


class _Registry:
    def get_signal(self, name):
        return _Signal()


class _Fuser:
    def __init__(self):
        self.histories = []
        self._calibrated = False

    def fit_calibration(self, history):
        self.histories.append(deepcopy(history))
        self._calibrated = True

    def is_calibrated(self):
        return self._calibrated

    def fuse(self, signal_predictions, station=None):
        return {"direction": "up", "confidence": 0.8}


def _run(labels):
    fuser = _Fuser()
    result = compute_fusion_accuracy(
        {"KATL": {"2026-02-27": 70.0, "2026-02-28": 71.0, "2026-05-15": 75.0}},
        {"KATL": [{"date": "2026-02-26"}, {"date": "2026-02-27"}, {"date": "2026-02-28"}, {"date": "2026-05-14"}, {"date": "2026-05-15", "x": 9999}]},
        _Registry(), fuser, "uwc", ["KATL"],
        calibration_cutoff_date=CALIBRATION_CUTOFF_MAX,
        holdout_start="2026-05-15", holdout_end="2026-08-13", period="holdout",
        eval_signal_names=["pressure_delta"],
        calibration_label_source="historical_kalshi",
        historical_calibration_labels=labels,
    )
    return fuser, result


def test_historical_label_is_explicit_and_pre_cutoff():
    labels = [{
        "station": "KATL", "target_date": "2026-02-28", "market_direction": "HIGH",
        "event_direction": "up",
    }]
    fuser, result = _run(labels)
    assert fuser.histories == [{("pressure_delta", "KATL"): [(0.8, True)]}]
    assert [p["date"] for p in result["predictions"]] == ["2026-05-15"]


def test_historical_label_after_cutoff_is_rejected():
    labels = [{
        "station": "KATL", "target_date": "2026-03-01", "market_direction": "HIGH",
        "event_direction": "up",
    }]
    with pytest.raises(ValueError, match="outside cutoff"):
        _run(labels)


def test_duplicate_label_identity_is_rejected():
    labels = [{
        "station": "KATL", "target_date": "2026-02-28", "market_direction": "HIGH",
        "event_direction": "up",
    }] * 2
    with pytest.raises(ValueError, match="duplicate"):
        _run(labels)


def test_default_path_stays_fail_closed():
    fuser = _Fuser()
    compute_fusion_accuracy(
        {"KATL": {"2026-02-27": 70.0, "2026-02-28": 71.0, "2026-05-15": 75.0}},
        {"KATL": [{"date": "2026-02-26"}, {"date": "2026-02-27"}, {"date": "2026-02-28"}, {"date": "2026-05-14"}]},
        _Registry(), fuser, "uwc", ["KATL"],
        calibration_cutoff_date=CALIBRATION_CUTOFF_MAX,
        holdout_start="2026-05-15", holdout_end="2026-08-13", period="holdout",
        eval_signal_names=["pressure_delta"],
    )
    assert fuser.histories == []


def test_target_day_and_holdout_settlements_cannot_change_prediction_side_state():
    labels = [{
        "station": "KATL", "target_date": "2026-02-28", "market_direction": "HIGH",
        "event_direction": "up",
    }]
    baseline_fuser, baseline = _run(labels)
    # Only the scored holdout outcome changes; signal output and confidence do not.
    mutated_fuser = _Fuser()
    mutated = compute_fusion_accuracy(
        {"KATL": {"2026-02-27": 70.0, "2026-02-28": 999.0, "2026-05-15": 60.0}},
        {"KATL": [{"date": "2026-02-26"}, {"date": "2026-02-27"}, {"date": "2026-02-28", "x": 123}, {"date": "2026-05-14"}, {"date": "2026-05-15", "x": 9999}]},
        _Registry(), mutated_fuser, "uwc", ["KATL"],
        calibration_cutoff_date=CALIBRATION_CUTOFF_MAX,
        holdout_start="2026-05-15", holdout_end="2026-08-13", period="holdout",
        eval_signal_names=["pressure_delta"],
        calibration_label_source="historical_kalshi",
        historical_calibration_labels=labels,
    )
    assert baseline_fuser.histories == mutated_fuser.histories
    assert baseline["predictions"][0]["confidence"] == mutated["predictions"][0]["confidence"]
    assert baseline["predictions"][0]["pred_dir"] == mutated["predictions"][0]["pred_dir"]
    assert baseline["predictions"][0]["correct"] != mutated["predictions"][0]["correct"]


def test_real_loader_keeps_high_low_identity():
    labels, audit = load_historical_calibration_labels(
        "data/kalshi_settlements.db", return_audit=True
    )
    assert audit["cutoff_date"] == CALIBRATION_CUTOFF_MAX
    assert all(x["market_direction"] in {"HIGH", "LOW"} for x in labels)
    assert len({(x["station"], x["target_date"], x["market_direction"]) for x in labels}) == len(labels)
