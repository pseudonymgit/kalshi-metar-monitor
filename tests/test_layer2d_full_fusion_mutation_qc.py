"""Layer 2D Phase 1 mutation QC for calibration-label separation."""

from copy import deepcopy

from scripts.ws5_common import compute_fusion_accuracy


class _Signal:
    min_lookback = 0

    def evaluate(self, idx, days):
        # Deliberately independent of METAR values: any mutation effect is in
        # the evaluator/calibration path, not in this raw signal.
        return "up", 0.8


class _Registry:
    def get_signal(self, name):
        assert name in {"pressure_delta", "seasonal_regime"}
        return _Signal()


class _SpyFuser:
    def __init__(self):
        self.histories = []
        self._is_calibrated = False

    def fit_calibration(self, history):
        self.histories.append(deepcopy(history))
        self._is_calibrated = True

    def is_calibrated(self):
        return self._is_calibrated

    def fuse(self, signal_predictions, station=None):
        return {
            "direction": "up",
            "confidence": 0.8,
            "verdict": "TRADE",
            "reason": "fixture",
        }


def _fixture(settlement_on_calibration_date):
    # The first two rows create a calibration record at 2026-02-26; the final
    # row is a holdout record. METAR has a target-day row intentionally, which
    # prior_metar_index must not select.
    settlements = {
        "KATL": {
            "2026-02-25": 70.0,
            "2026-02-26": settlement_on_calibration_date,
            "2026-05-14": 74.0,
            "2026-05-15": 75.0,
        }
    }
    metar = {
        "KATL": [
            {"date": "2026-02-24", "pressure": 1000.0},
            {"date": "2026-02-25", "pressure": 1001.0},
            {"date": "2026-02-26", "pressure": 1002.0},
            {"date": "2026-05-14", "pressure": 1003.0},
            {"date": "2026-05-15", "pressure": 9999.0},
        ]
    }
    return settlements, metar


def _run(calibration_temp):
    settlements, metar = _fixture(calibration_temp)
    fuser = _SpyFuser()
    result = compute_fusion_accuracy(
        settlements=settlements,
        metar_batched=metar,
        registry=_Registry(),
        fuser=fuser,
        fusion_method="uwc",
        station_list=["KATL"],
        calibration_cutoff_date="2026-02-28",
        holdout_start="2026-05-15",
        holdout_end="2026-08-13",
        period="holdout",
        eval_signal_names=["pressure_delta", "seasonal_regime"],
    )
    return fuser, result


def test_full_fusion_mutation_qc_blocks_settlement_calibration_leakage():
    baseline, baseline_result = _run(75.0)
    mutated, mutated_result = _run(60.0)

    # Raw METAR-independent signal outputs and holdout key geometry are stable.
    assert baseline_result["predictions"] == mutated_result["predictions"]
    assert [p["date"] for p in baseline_result["predictions"]] == ["2026-05-15"]
    assert baseline_result["predictions"][0]["correct"] == mutated_result["predictions"][0]["correct"]

    # Settlement mutations may affect scored correctness only. Calibration is
    # disabled until an immutable historical label source is supplied.
    assert baseline.histories == mutated.histories == []


def test_target_day_metar_index_is_strictly_prior():
    # Existing helper-level guard, included in the full-fusion QC suite.
    from scripts.ws5_common import prior_metar_index

    days = [{"date": "2026-05-14", "pressure": 1000.0}, {"date": "2026-05-15", "pressure": 9999.0}]
    idx = prior_metar_index(days, "2026-05-15")
    assert idx == 0
    assert days[idx]["date"] < "2026-05-15"
