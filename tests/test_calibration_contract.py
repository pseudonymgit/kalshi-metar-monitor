"""Q-CAL-002 contract checks for the calibration producer/consumer boundary."""

import importlib.util
import json
from collections import defaultdict
from pathlib import Path
from unittest.mock import mock_open, patch

import pytest

from core.intraday.calibration import GLOBAL_CALIBRATION, HourlyCalibration
from core.calibration.semantic_adapter import CalibrationSemanticError, adapt_semantic_fixture


_BUILD_CURVES = str(Path(__file__).parents[1] / "scripts" / "build_calibration_curves.py")
_Q_CAL_021_FIXTURE = Path(__file__).parent / "fixtures" / "q-cal-021-hourly-platt-producer.json"
_RUNTIME_CURVES = Path(__file__).parents[1] / "data" / "calibration_curves.json"


def _q_cal_021_record():
    fixture = json.loads(_Q_CAL_021_FIXTURE.read_text())
    return {
        "market_type": fixture["market_type"],
        "direction": fixture["direction"],
        "hour_semantics": fixture["hour_semantics"],
    }


def _load_builder():
    spec = importlib.util.spec_from_file_location("build_calibration_curves", _BUILD_CURVES)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_producer_schema_is_directional_bins_not_hourly_platt(tmp_path):
    """Make the incompatible producer/consumer contract explicit and stable."""
    builder = _load_builder()
    produced = builder.build_direction_curve(
        defaultdict(list, {"KATL": [(0.8, True), (0.8, False)]}),
        ["KATL"],
    )
    artifact = {
        "KATL": {"HIGH": {"up": produced["per_station"]["KATL"], "down": produced["per_station"]["KATL"]}},
        "_global": {"HIGH": {"up": produced["global"], "down": produced["global"]}},
    }
    path = tmp_path / "calibration_curves.json"
    path.write_text(json.dumps(artifact))

    loader = HourlyCalibration(str(path))

    assert set(artifact["KATL"]) == {"HIGH"}
    assert "bins" in artifact["KATL"]["HIGH"]["up"]
    assert loader.loaded_station_count == 0
    assert loader._curves == {}


def test_q_cal_015_consumer_seam_rejects_directional_bins_without_fallback_contamination():
    """The consumer seam must quarantine bins, not reshape them into Platt cells."""
    directional_artifact = {
        "KATL": {
            "HIGH": {
                "up": {"bins": [{"lower": 0.7, "upper": 0.8, "rate": 0.9, "n": 40}]},
                "down": {"bins": [{"lower": 0.7, "upper": 0.8, "rate": 0.1, "n": 40}]},
            }
        },
        "_global": {
            "HIGH": {
                "up": {"bins": [{"lower": 0.7, "upper": 0.8, "rate": 0.9, "n": 80}]},
                "down": {"bins": [{"lower": 0.7, "upper": 0.8, "rate": 0.1, "n": 80}]},
            }
        },
    }

    # Exercise the actual loader/adapter seam, while keeping the artifact
    # entirely synthetic and preventing any filesystem artifact read.
    with (
        patch("core.intraday.calibration.os.path.exists", return_value=True),
        patch("builtins.open", mock_open()),
        patch("core.intraday.calibration.json.load", return_value=directional_artifact),
    ):
        loader = HourlyCalibration("fixture://directional-bins.json")
        calibrated = loader.get_calibrated("KATL", 6, 0.75)

    assert loader.loaded_station_count == 0
    assert loader._curves == {}
    assert loader._get_global_curve(6) == GLOBAL_CALIBRATION[6]
    expected = loader._apply_platt(0.75, GLOBAL_CALIBRATION[6]["alpha"], GLOBAL_CALIBRATION[6]["beta"])
    assert calibrated == expected


@pytest.mark.parametrize(
    "wrapper_fields",
    [
        {"version": 2, "type": "hourly_platt"},
        {
            "version": 2,
            "type": "hourly_platt",
            "market_type": "daily_high_temperature",
            "direction": "up",
            "hour_semantics": {
                "measured_from": "settlement_time",
                "timezone": "America/New_York",
                "bucket_boundaries": ["06:00", "07:00"],
            },
        },
        {
            "version": 2,
            "type": "hourly_platt",
            "market_type": "forged-or-irrelevant",
            "direction": "down",
            "hour_semantics": {"measured_from": "local", "timezone": "UTC"},
        },
    ],
    ids=["wrapper-only", "valid-semantic-fields", "forged-semantic-fields"],
)
def test_q_cal_023_only_hourly_platt_cell_shape_admits_runtime_data(
    wrapper_fields,
):
    """Wrapper/semantic fields cannot authorize an incompatible directional artifact."""
    directional_artifact = {
        **wrapper_fields,
        "curves": {
            "KATL": {
                "6": {
                    "up": {"bins": [{"lower": 0.7, "upper": 0.8, "rate": 0.9, "n": 40}]},
                    "down": {"bins": [{"lower": 0.7, "upper": 0.8, "rate": 0.1, "n": 40}]},
                }
            }
        },
    }

    with (
        patch("core.intraday.calibration.os.path.exists", return_value=True),
        patch("builtins.open", mock_open()),
        patch("core.intraday.calibration.json.load", return_value=directional_artifact),
    ):
        loader = HourlyCalibration("fixture://q-cal-023-directional.json")

    assert loader.loaded_station_count == 0
    assert loader._curves == {}
    assert loader.get_calibrated("KATL", 6, 0.75) == loader._apply_platt(
        0.75, **GLOBAL_CALIBRATION[6]
    )


def test_q_cal_023_semantic_fields_do_not_change_admission_of_valid_cells():
    """Only numeric hour -> alpha/beta/n cells affect consumer admission."""
    curves = {"KATL": {"6": {"alpha": 2.0, "beta": 0.0, "n": 30}}}
    fixtures = [
        {"curves": curves},
        {
            "curves": curves,
            "market_type": "daily_high_temperature",
            "direction": "up",
            "hour_semantics": {
                "measured_from": "settlement_time",
                "timezone": "America/New_York",
                "bucket_boundaries": ["06:00", "07:00"],
            },
        },
        {
            "curves": curves,
            "market_type": "not-used-by-loader",
            "direction": "down",
            "hour_semantics": {"arbitrary": ["semantic", "payload"]},
        },
    ]

    admissions = []
    for index, fixture in enumerate(fixtures):
        with (
            patch("core.intraday.calibration.os.path.exists", return_value=True),
            patch("builtins.open", mock_open()),
            patch("core.intraday.calibration.json.load", return_value=fixture),
        ):
            loader = HourlyCalibration(f"fixture://q-cal-023-{index}.json")
        admissions.append(
            (
                loader.loaded_station_count,
                loader._curves,
                loader.get_calibrated("KATL", 6, 0.75),
            )
        )

    assert admissions[1:] == [admissions[0], admissions[0]]


def test_q_cal_024_decoded_caller_mutation_cannot_change_admitted_runtime_cell():
    """A mocked json decoder must not leak mutable cell aliases into runtime state."""
    decoded = {
        "version": 2,
        "type": "hourly_platt",
        "market_type": "daily_high_temperature",
        "direction": "up",
        "hour_semantics": {
            "measured_from": "settlement_time",
            "timezone": "America/New_York",
            "bucket_boundaries": ["06:00", "07:00"],
        },
        "curves": {
            "KATL": {
                "6": {"alpha": 2.0, "beta": 0.0, "n": 30},
                "7": {"alpha": 1.5, "beta": -0.1, "n": 30},
            }
        },
    }
    with (
        patch("core.intraday.calibration.os.path.exists", return_value=True),
        patch("builtins.open", mock_open()),
        patch("core.intraday.calibration.json.load", return_value=decoded),
    ):
        loader = HourlyCalibration("fixture://q-cal-024-alias.json")

    before = [loader.get_calibrated("KATL", hour, 0.75) for hour in (6, 7)]
    before_neighbor = loader.get_calibrated("KATL", 8, 0.75)
    # Simulate a producer/test fixture retaining and mutating its decoded JSON
    # after construction, including semantic/wrapper metadata.
    decoded["curves"]["KATL"]["6"]["alpha"] = 99.0
    decoded["curves"]["KATL"]["7"]["beta"] = 99.0
    decoded["curves"]["KATL"]["8"] = {"alpha": 100.0, "beta": 0.0, "n": 30}
    decoded["type"] = "directional_bins"
    decoded["direction"] = "down"
    decoded["hour_semantics"]["timezone"] = "UTC"
    decoded["curves"]["KATL"]["6"] = {"bins": {"forged": True}}

    assert loader.loaded_station_count == 1
    assert loader._curves["KATL"]["6"] == {"alpha": 2.0, "beta": 0.0, "n": 30}
    assert loader._curves["KATL"]["7"] == {"alpha": 1.5, "beta": -0.1, "n": 30}
    assert "8" not in loader._curves["KATL"]
    assert [loader.get_calibrated("KATL", hour, 0.75) for hour in (6, 7)] == before
    # Hour 8 intentionally exercises the existing nearest-hour fallback to
    # the admitted hour-7 cell; caller mutation must not change that behavior.
    assert loader.get_calibrated("KATL", 8, 0.75) == before_neighbor


def test_q_cal_003_decision_requires_preregistered_semantics():
    """The disabled artifact cannot be migrated without explicit semantic fields."""
    contract = (Path(__file__).parents[1] / "docs" / "calibration-producer-consumer-contract.md").read_text()

    assert "KEEP-DISABLED" in contract
    for required_field in ("market type", "direction", "hour semantics"):
        assert required_field in contract
    assert "must not infer" in contract
    assert "discard" in contract


def test_q_cal_021_preregistered_fixture_preserves_wrapper_and_semantics():
    """The synthetic producer fixture keeps identity and hour meaning explicit."""
    fixture = json.loads(_Q_CAL_021_FIXTURE.read_text())

    assert fixture["version"] == 2
    assert fixture["type"] == "hourly_platt"
    assert set(fixture) >= {"version", "type", "market_type", "direction", "hour_semantics", "curves"}
    assert fixture["market_type"] == "daily_high_temperature"
    assert fixture["direction"] == "up"
    assert fixture["hour_semantics"] == {
        "measured_from": "settlement_time",
        "timezone": "America/New_York",
        "bucket_boundaries": ["06:00", "07:00"],
    }
    assert set(fixture["curves"]["KATL"]) == {"6", "7"}
    assert all(set(cell) == {"alpha", "beta", "n"} for cell in fixture["curves"]["KATL"].values())


def test_q_cal_021_fixture_is_consumer_shape_without_runtime_enablement(tmp_path):
    """Only the curves section is passed to the read-only consumer seam."""
    fixture = json.loads(_Q_CAL_021_FIXTURE.read_text())
    path = tmp_path / "q-cal-021-fixture.json"
    path.write_text(json.dumps(fixture))

    loader = HourlyCalibration(str(path))

    assert loader.loaded_station_count == 1
    assert loader._curves["KATL"]["6"] == {"alpha": 0.91, "beta": 0.04, "n": 42}
    assert loader._curves["KATL"]["7"] == {"alpha": 0.93, "beta": 0.02, "n": 39}


@pytest.mark.parametrize(
    "field, replacement",
    [
        ("market_type", None),
        ("market_type", "   "),
        ("direction", None),
        ("direction", "   "),
        ("hour_semantics", {"measured_from": "settlement_time", "timezone": "America/New_York", "bucket_boundaries": ["06:00"]}),
        ("hour_semantics", {"measured_from": "settlement_time", "timezone": "local", "bucket_boundaries": ["06:00", "07:00"]}),
        ("hour_semantics", {"measured_from": "", "timezone": "America/New_York", "bucket_boundaries": ["06:00", "07:00"]}),
        ("hour_semantics", {"measured_from": "settlement_time", "timezone": "America/New_York", "bucket_boundaries": ["06:00", "06:00"]}),
    ],
)
def test_q_cal_022_rejects_missing_or_ambiguous_contract_semantics(field, replacement):
    """No semantic field may be omitted, guessed, or accepted in an ambiguous form."""
    record = _q_cal_021_record()
    if replacement is None:
        record.pop(field)
    elif field == "hour_semantics":
        record[field] = replacement
    else:
        record[field] = replacement

    with pytest.raises(CalibrationSemanticError):
        adapt_semantic_fixture(record)


def test_q_cal_022_fixture_is_not_runtime_enabled_or_written(monkeypatch):
    """The review fixture remains read-only and cannot target the runtime artifact."""
    fixture_path = _Q_CAL_021_FIXTURE.resolve()
    runtime_path = _RUNTIME_CURVES.resolve()
    assert fixture_path != runtime_path
    assert str(runtime_path).endswith("data/calibration_curves.json")

    writes = []
    original_write_text = Path.write_text

    def record_write(path, *args, **kwargs):
        writes.append(Path(path).resolve())
        return original_write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", record_write)
    fixture = json.loads(fixture_path.read_text())
    adapted = adapt_semantic_fixture({
        "market_type": fixture["market_type"],
        "direction": fixture["direction"],
        "hour_semantics": fixture["hour_semantics"],
    })

    assert adapted["market_type"] == "daily_high_temperature"
    assert writes == []
    assert _RUNTIME_CURVES.exists()


def test_hourly_consumer_contract_requires_numeric_hour_platt_cells(tmp_path):
    """A future hourly producer must provide numeric-hour alpha/beta/n cells."""
    path = tmp_path / "calibration_curves.json"
    path.write_text(json.dumps({"KATL": {"6": {"alpha": 2.0, "beta": 0.0, "n": 30}}}))

    loader = HourlyCalibration(str(path))

    assert loader.loaded_station_count == 1
    assert loader.get_calibrated("KATL", 6, 0.75) > 0.75
