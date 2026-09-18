from scripts.ws5_common import compute_fusion_accuracy, prior_metar_index


def test_settlement_labels_do_not_enable_calibration_by_default():
    class Signal:
        min_lookback = 0
        def evaluate(self, idx, days):
            return "up", 0.8

    class Registry:
        def get_signal(self, name):
            return Signal()

    class Fuser:
        def __init__(self):
            self.fit_calls = 0
        def fit_calibration(self, history):
            self.fit_calls += 1
        def fuse(self, signal_predictions, station=None):
            return {"direction": "up", "confidence": 0.8}

    fuser = Fuser()
    compute_fusion_accuracy(
        {"KATL": {"2026-02-25": 70.0, "2026-02-26": 71.0, "2026-05-15": 75.0}},
        {"KATL": [{"date": "2026-02-24"}, {"date": "2026-02-25"}, {"date": "2026-05-14"}]},
        Registry(), fuser, "uwc", ["KATL"],
        calibration_cutoff_date="2026-02-28", holdout_start="2026-05-15",
        holdout_end="2026-08-13", period="holdout",
        eval_signal_names=["pressure_delta"],
    )
    assert fuser.fit_calls == 0


def test_target_day_is_never_selected():
    days = [{"date": d, "temp": i} for i, d in enumerate(("2026-05-14", "2026-05-15", "2026-05-16"))]
    assert prior_metar_index(days, "2026-05-15") == 0
    assert days[prior_metar_index(days, "2026-05-15")]["date"] < "2026-05-15"


def test_missing_prior_is_ineligible():
    assert prior_metar_index([{"date": "2026-05-15"}], "2026-05-15") is None


def test_target_day_mutation_cannot_change_prior_snapshot():
    before = [{"date": "2026-05-14", "temp": 70.0}, {"date": "2026-05-15", "temp": 71.0}]
    after = [{"date": "2026-05-14", "temp": 70.0}, {"date": "2026-05-15", "temp": 999.0}]
    i1 = prior_metar_index(before, "2026-05-15")
    i2 = prior_metar_index(after, "2026-05-15")
    assert before[i1] == after[i2]
