import json
import math
import sqlite3
from pathlib import Path

from scripts.gefs_direct_temperature_evaluation import (
    DIRECTION_EPSILON_F,
    panel_gate,
    empirical_crps,
    brier_ece,
    cutoff_for,
    partition_for,
    season_for,
    main,
)


def test_frozen_windows_cutoff_and_season():
    assert cutoff_for("2026-03-01") == "2026-02-28T12:00:00Z"
    assert partition_for("2025-12-31") == "FIT_HISTORY"
    assert partition_for("2026-02-28") == "CALIBRATION"
    assert partition_for("2026-03-01") == "LOCKED_SHADOW"
    assert season_for("2026-01-15") == "DJF"
    assert season_for("2026-07-15") == "JJA"


def test_member_panel_gate_is_explicit_and_fail_closed():
    assert panel_gate(31, 31) == (True, "eligible")
    assert panel_gate(31, 30) == (True, "eligible")
    assert panel_gate(31, 6) == (False, "insufficient_members")
    assert panel_gate(None, 31) == (False, "missing_expected_member_count")


def test_crps_and_brier_ece_are_deterministic():
    assert round(empirical_crps([0.0, 1.0], 0.0), 8) == 0.25
    out = brier_ece([0.1, 0.9], [0, 1])
    assert out["n"] == 2
    assert math.isclose(out["brier"], 0.01)
    assert math.isclose(out["ece"], 0.1)


def test_main_is_read_only_and_emits_blocked_manifest(tmp_path):
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "result.json"
    assert main(["--root", str(root), "--output", str(output)]) == 0
    result = json.loads(output.read_text())
    assert result["status"] == "BLOCKED"
    assert result["eligibility"]["eligible_rows"] == 0
    assert result["eligibility"]["observed_member_count_strata"] == {"30": 129, "31": 36091, "6": 180}
    assert result["metrics"]["n_scored"] == 0
    assert result["gates"]["target_day_leakage_gate"] == "PASS_NO_FORECAST_READ"
    # The evaluator uses read-only SQLite connections; this test also guards against
    # accidental writes by checking the source DB remains queryable after execution.
    conn = sqlite3.connect(root / "data" / "gefs_archive.db")
    assert conn.execute("SELECT COUNT(*) FROM gefs_archive").fetchone()[0] == 365960
    conn.close()
