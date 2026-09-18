from datetime import datetime, timezone
from pathlib import Path
import importlib.util

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("geometry_fix", ROOT / "scripts/validate_gefs_temperature_geometry_fix.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_local_day_durations_and_expected_points():
    issue = datetime(2024, 3, 9, tzinfo=timezone.utc)
    spring, spring_points = mod.expected_leads(issue, datetime(2024, 3, 10, tzinfo=timezone.utc).date(), "America/New_York")
    assert spring["elapsed_utc_hours"] == 23.0
    assert spring["expected_point_leads"] == [30, 33, 36, 39, 42, 45, 48, 51]
    issue = datetime(2024, 11, 2, tzinfo=timezone.utc)
    fall, _ = mod.expected_leads(issue, datetime(2024, 11, 3, tzinfo=timezone.utc).date(), "America/New_York")
    assert fall["elapsed_utc_hours"] == 25.0
    assert fall["expected_point_leads"] == [30, 33, 36, 39, 42, 45, 48, 51]


def test_interval_gap_is_exact_and_fail_closed():
    intervals = [
        {"start": 48, "end": 54, "lead": 54, "native_semantics_ok": True},
    ]
    result = mod.cover_window(intervals, 31, 55)
    assert result["covered"] is False
    assert result["gaps"] == [[31, 48], [54, 55]]


def test_interval_metadata_requires_native_semantics():
    intervals = [
        {"start": 0, "end": 6, "lead": 6, "native_semantics_ok": False},
    ]
    result = mod.cover_window(intervals, 0, 6)
    assert result["covered"] is False
    assert result["gaps"] == [[0, 6]]
