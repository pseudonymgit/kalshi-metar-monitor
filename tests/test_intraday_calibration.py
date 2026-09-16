import json

import pytest

from core.intraday.calibration import GLOBAL_CALIBRATION, HourlyCalibration


def test_directional_bin_fixture_is_not_counted_as_hourly_station(tmp_path):
    """The legacy directional fixture must not masquerade as hourly curves."""
    fixture = {
        "KATL": {
            "up": {"bins": {}, "global": {"n": 10, "win_rate": 0.6}},
            "down": {"bins": {}, "global": {"n": 10, "win_rate": 0.4}},
        },
        "_global": {"up": {}, "down": {}},
    }
    path = tmp_path / "calibration_curves.json"
    path.write_text(json.dumps(fixture))

    calibrator = HourlyCalibration(str(path))

    assert calibrator._loaded is True
    assert calibrator.loaded_station_count == 0
    assert calibrator._curves == {}


def test_hourly_platt_fixture_is_counted_and_applied(tmp_path):
    fixture = {
        "KATL": {"6": {"alpha": 2.0, "beta": 0.0, "n": 30}},
    }
    path = tmp_path / "calibration_curves.json"
    path.write_text(json.dumps(fixture))

    calibrator = HourlyCalibration(str(path))

    assert calibrator.loaded_station_count == 1
    assert calibrator.get_calibrated("katl", 6, 0.75) > 0.75


def test_valid_wrapper_metadata_is_optional_and_semantically_inert(tmp_path):
    """Absent or valid wrapper metadata must not change cells or fallback behavior."""
    station_map = {
        "KATL": {
            "6": {"alpha": 2.0, "beta": 0.0, "n": 30},
            "7": {"alpha": 1.5, "beta": -0.1, "n": 30},
        }
    }
    fixtures = [
        station_map,
        {"curves": station_map},
        {"curves": station_map, "version": 2, "type": "hourly_platt"},
    ]
    loaded = []
    for index, fixture in enumerate(fixtures):
        path = tmp_path / f"wrapped-{index}.json"
        path.write_text(json.dumps(fixture))
        calibrator = HourlyCalibration(str(path))
        loaded.append(
            (
                calibrator.loaded_station_count,
                calibrator._curves,
                [calibrator.get_calibrated("KATL", hour, 0.75) for hour in (6, 7, 8)],
                [calibrator.get_calibrated("KDEN", hour, 0.75) for hour in (6, 8)],
            )
        )

    assert loaded[1:] == [loaded[0], loaded[0]]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("alpha", None),
        ("alpha", "not-a-number"),
        ("beta", None),
        ("beta", "not-a-number"),
        ("n", None),
        ("n", "30"),
    ],
)
def test_malformed_numeric_hour_cell_is_quarantined_without_fallback_change(
    tmp_path, field, value
):
    """A malformed cell cannot partially load or mutate global fallback state."""
    cell = {"alpha": 2.0, "beta": 0.0, "n": 30}
    cell[field] = value
    fixture = {"KATL": {"6": cell, "7": {"alpha": 2.0, "beta": 0.0, "n": 30}}}
    path = tmp_path / "calibration_curves.json"
    path.write_text(json.dumps(fixture))

    calibrator = HourlyCalibration(str(path))
    raw = 0.75

    assert calibrator.loaded_station_count == 0
    assert calibrator._curves == {}
    expected = calibrator._apply_platt(raw, **GLOBAL_CALIBRATION[6])
    assert calibrator.get_calibrated("KATL", 6, raw) == expected
    assert calibrator.get_calibrated("KATL", 7, raw) == calibrator._apply_platt(
        raw, **GLOBAL_CALIBRATION[7]
    )


@pytest.mark.parametrize("field", ["alpha", "beta", "n"])
def test_missing_numeric_hour_field_rejects_entire_station_map(tmp_path, field):
    cell = {"alpha": 2.0, "beta": 0.0, "n": 30}
    del cell[field]
    fixture = {"KATL": {"6": cell, "8": {"alpha": 2.0, "beta": 0.0, "n": 30}}}
    path = tmp_path / "calibration_curves.json"
    path.write_text(json.dumps(fixture))

    calibrator = HourlyCalibration(str(path))

    assert calibrator.loaded_station_count == 0
    assert calibrator._curves == {}
    assert calibrator.get_calibrated("KATL", 8, 0.75) == calibrator._apply_platt(
        0.75, **GLOBAL_CALIBRATION[8]
    )


@pytest.mark.parametrize(
    "hour_key",
    ["-1", "13", "06"],
    ids=["negative", "above-supported-range", "duplicate-equivalent-canonical-key"],
)
def test_invalid_numeric_hour_keys_quarantine_atomically_and_preserve_global_snapshot(
    tmp_path, hour_key
):
    """Boundary keys cannot admit a partial station map or alter fallback state."""
    fixture = {
        "KATL": {
            "6": {"alpha": 2.0, "beta": 0.0, "n": 30},
            hour_key: {"alpha": 3.0, "beta": 0.0, "n": 31},
            "7": {"alpha": 2.0, "beta": 0.0, "n": 30},
        }
    }
    path = tmp_path / "calibration_curves.json"
    path.write_text(json.dumps(fixture))

    calibrator = HourlyCalibration(str(path))

    assert calibrator.loaded_station_count == 0
    assert calibrator._curves == {}
    assert calibrator.get_calibrated("KATL", 6, 0.75) == calibrator._apply_platt(
        0.75, **GLOBAL_CALIBRATION[6]
    )
    assert calibrator.get_calibrated("KATL", 7, 0.75) == calibrator._apply_platt(
        0.75, **GLOBAL_CALIBRATION[7]
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("alpha", float("nan")),
        ("alpha", float("inf")),
        ("beta", float("-inf")),
        ("n", float("nan")),
        ("alpha", True),
        ("beta", False),
        ("n", True),
    ],
)
def test_nonfinite_and_boolean_scalars_quarantine_atomically_and_preserve_global_snapshot(
    tmp_path, field, value
):
    cell = {"alpha": 2.0, "beta": 0.0, "n": 30}
    cell[field] = value
    fixture = {
        "KATL": {
            "6": cell,
            "7": {"alpha": 2.0, "beta": 0.0, "n": 30},
        }
    }
    path = tmp_path / "calibration_curves.json"
    path.write_text(json.dumps(fixture, allow_nan=True))

    calibrator = HourlyCalibration(str(path))

    assert calibrator.loaded_station_count == 0
    assert calibrator._curves == {}
    expected_snapshot = {
        hour: dict(params) for hour, params in GLOBAL_CALIBRATION.items()
    }
    assert GLOBAL_CALIBRATION == expected_snapshot
    for hour in expected_snapshot:
        assert calibrator.get_calibrated("KATL", hour, 0.75) == calibrator._apply_platt(
            0.75, **expected_snapshot[hour]
        )


@pytest.mark.parametrize(
    "hour_key",
    ["06", "6.0", "6e0", "hb6", "+6", " 6", "６"],
)
def test_unsupported_hour_key_encodings_quarantine_whole_station_and_preserve_fallback(
    tmp_path, hour_key
):
    """Only canonical JSON decimal keys are valid at the producer boundary."""
    fixture = {
        "KATL": {
            "6": {"alpha": 2.0, "beta": 0.0, "n": 30},
            hour_key: {"alpha": 3.0, "beta": 0.0, "n": 31},
        }
    }
    before = json.dumps(GLOBAL_CALIBRATION, sort_keys=True, separators=(",", ":")).encode()
    path = tmp_path / "calibration_curves.json"
    path.write_text(json.dumps(fixture, ensure_ascii=False))

    calibrator = HourlyCalibration(str(path))

    assert calibrator.loaded_station_count == 0
    assert calibrator._curves == {}
    assert json.dumps(GLOBAL_CALIBRATION, sort_keys=True, separators=(",", ":")).encode() == before
    assert calibrator.get_calibrated("KATL", 6, 0.75) == calibrator._apply_platt(
        0.75, **GLOBAL_CALIBRATION[6]
    )


@pytest.mark.parametrize("metadata", [{"version": 1}, {"version": "2"}, {"type": "directional_bins"}, {"type": None}])
def test_wrapper_version_or_type_conflict_quarantines_without_fallback_change(tmp_path, metadata):
    fixture = {
        **metadata,
        "curves": {"KATL": {"6": {"alpha": 2.0, "beta": 0.0, "n": 30}}},
    }
    before = json.dumps(GLOBAL_CALIBRATION, sort_keys=True, separators=(",", ":")).encode()
    path = tmp_path / "calibration_curves.json"
    path.write_text(json.dumps(fixture))

    calibrator = HourlyCalibration(str(path))

    assert calibrator.loaded_station_count == 0
    assert calibrator._curves == {}
    assert json.dumps(GLOBAL_CALIBRATION, sort_keys=True, separators=(",", ":")).encode() == before
    assert calibrator.get_calibrated("KATL", 6, 0.75) == calibrator._apply_platt(
        0.75, **GLOBAL_CALIBRATION[6]
    )


def _bounded_json_values():
    """Deterministic bounded arbitrary JSON-shaped station payloads."""
    leaves = [None, True, False, 0, 1.5, "6", "hb6", [], {}]
    for index, leaf in enumerate(leaves):
        yield {"KATL": {"6": {"alpha": 2.0, "beta": 0.0, "n": 30}, str(index): leaf}}
    yield {"KATL": ["not-a-station-map"]}
    yield {"KATL": {"6": {"alpha": 2.0, "beta": 0.0}}}
    yield {"KATL": {"6": {"alpha": 2.0, "beta": 0.0, "n": 30}, "13": {"alpha": 2.0, "beta": 0.0, "n": 30}}}


def test_bounded_arbitrary_json_station_maps_preserve_quarantine_invariant(tmp_path):
    """Every generated invalid shape quarantines atomically and preserves bytes."""
    before = json.dumps(GLOBAL_CALIBRATION, sort_keys=True, separators=(",", ":")).encode()
    for index, fixture in enumerate(_bounded_json_values()):
        path = tmp_path / f"generated-{index}.json"
        path.write_text(json.dumps(fixture))
        calibrator = HourlyCalibration(str(path))
        assert calibrator.loaded_station_count == 0, fixture
        assert calibrator._curves == {}, fixture
        after = json.dumps(GLOBAL_CALIBRATION, sort_keys=True, separators=(",", ":")).encode()
        assert after == before, fixture
        assert calibrator.get_calibrated("KATL", 6, 0.75) == calibrator._apply_platt(
            0.75, **GLOBAL_CALIBRATION[6]
        )


@pytest.mark.parametrize(
    "fixture",
    [
        {"curves": ["not-a-mapping"]},
        {"stations": None},
        {
            "curves": {"KATL": {"6": {"alpha": 2.0, "beta": 0.0, "n": 30}}},
            "stations": "malformed-sibling-wrapper",
        },
        ["top-level-wrapper-is-not-a-mapping"],
    ],
    ids=["curves-list", "stations-null", "malformed-sibling", "top-level-list"],
)
def test_malformed_top_level_wrappers_quarantine_atomically_and_preserve_fallback(
    tmp_path, fixture
):
    path = tmp_path / "calibration_curves.json"
    path.write_text(json.dumps(fixture))

    calibrator = HourlyCalibration(str(path))

    assert calibrator.loaded_station_count == 0
    assert calibrator._curves == {}
    assert calibrator.get_calibrated("KATL", 6, 0.75) == calibrator._apply_platt(
        0.75, **GLOBAL_CALIBRATION[6]
    )
