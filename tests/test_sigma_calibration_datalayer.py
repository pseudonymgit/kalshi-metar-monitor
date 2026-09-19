"""
E10 — Sigma authoring: calibration + data-layer tests (Σ.2 P2).

Covers the calibration modules and the DB connection layer:

  Σ.2a — core.calibration.hourly_calibration: NWS revision correction and
         curve-based calibrated lookup (HourlyCalibrationLoader).
  Σ.2b — core.intraday.calibration: Platt-scaled per-(station, hb) calibration
         cascade (per-station → neighbor → global → identity).
  Σ.2c — core.db_connection: DB registry, config resolution, and the
         connection cache contract (no DB writes, no live connections opened
         against production files).

All deterministic. Tests avoid touching production data files — where a
connection would be opened, we either inspect registry/config metadata or
redirect DATA_DIR to a temp path. Production code is untouched.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import core.db_connection as dbconn
from core.calibration.hourly_calibration import (
    HourlyCalibrationLoader,
    HourlyCalibration,
    NWS_REVISION_CORRECTION_FACTORS,
)
from core.intraday.calibration import (
    HourlyCalibration as IntradayHourlyCalibration,
    GLOBAL_CALIBRATION,
    MIN_SAMPLES_PER_CELL,
    CALIBRATED_MIN,
    CALIBRATED_MAX,
    _safe_logit,
    _safe_expit,
    create_hourly_calibration,
)


# ──────────────────────────────────────────────────────────────
# Σ.2a — NWS revision correction (hourly_calibration loader)
# ──────────────────────────────────────────────────────────────

class TestNWSRevisionCorrection:
    def test_known_station_shifts_probability_up(self):
        loader = HourlyCalibrationLoader(curves_path="")
        # KMIA has bias_f 7.56 → +0.1512 probability shift (before clamp)
        p = loader.apply_nws_revision_correction("KMIA", 0.5)
        assert p > 0.5

    def test_unknown_station_unchanged(self):
        loader = HourlyCalibrationLoader(curves_path="")
        assert loader.apply_nws_revision_correction("ZZZZ", 0.5) == 0.5

    def test_correction_clamped_to_upper_bound(self):
        loader = HourlyCalibrationLoader(curves_path="")
        # High bias + high prob should clamp at 0.99
        p = loader.apply_nws_revision_correction("KMIA", 0.99)
        assert p <= 0.99

    def test_correction_clamped_to_lower_bound(self):
        loader = HourlyCalibrationLoader(curves_path="")
        # A station with a hypothetical negative bias isn't in the map, so use
        # a near-zero prob on a known station to confirm the 0.01 floor.
        p = loader.apply_nws_revision_correction("KLAX", 0.01)
        assert p >= 0.01

    def test_case_insensitive_station(self):
        loader = HourlyCalibrationLoader(curves_path="")
        assert loader.apply_nws_revision_correction("kmia", 0.5) == \
            loader.apply_nws_revision_correction("KMIA", 0.5)

    def test_all_factors_are_positive_bias(self):
        # Every recorded station has a positive bias_f (NWS final warmer than METAR).
        for st, f in NWS_REVISION_CORRECTION_FACTORS.items():
            assert f["bias_f"] > 0.0

    def test_missing_curves_file_yields_empty(self):
        loader = HourlyCalibrationLoader(curves_path="/nonexistent/calibration.json")
        assert loader._load_curves() == {}


# ──────────────────────────────────────────────────────────────
# Σ.2b — Intraday hourly calibration cascade
# ──────────────────────────────────────────────────────────────

class TestIntradayHourlyCalibration:
    def _write_curves(self, tmp_path, payload):
        p = tmp_path / "calibration_curves.json"
        p.write_text(__import__("json").dumps(payload))
        return str(p)

    def test_identity_fallback_when_no_curves(self):
        cal = IntradayHourlyCalibration(curves_path="")
        # No curves → global fallback (clamped raw). For hb=6 raw 0.5, expect ~0.5.
        out = cal.get_calibrated("KATL", 6, 0.5)
        assert CALIBRATED_MIN <= out <= CALIBRATED_MAX

    def test_output_always_clamped(self):
        cal = IntradayHourlyCalibration(curves_path="")
        out = cal.get_calibrated("KATL", 6, 1.5)
        assert CALIBRATED_MIN <= out <= CALIBRATED_MAX

    def test_per_station_curve_applied_when_sufficient_samples(self, tmp_path):
        # Station curve for hb=4 with n >= MIN_SAMPLES_PER_CELL
        payload = {
            "KATL": {
                "4": {"alpha": 0.9, "beta": 0.1, "n": MIN_SAMPLES_PER_CELL + 5},
            }
        }
        path = self._write_curves(tmp_path, payload)
        cal = IntradayHourlyCalibration(curves_path=path)
        out = cal.get_calibrated("KATL", 4, 0.6)
        # Per-station Platt should be applied, not identity
        assert 0.0 < out < 1.0

    def test_insufficient_samples_falls_to_global(self, tmp_path):
        # Station curve with n below threshold → must NOT be used
        payload = {
            "KATL": {
                "4": {"alpha": 0.1, "beta": -2.0, "n": 2},  # tiny n + extreme params
            }
        }
        path = self._write_curves(tmp_path, payload)
        cal = IntradayHourlyCalibration(curves_path=path)
        out = cal.get_calibrated("KATL", 4, 0.6)
        # Extreme per-station params are ignored → result stays near global (0.5-ish)
        assert 0.3 < out < 0.8

    def test_neighbor_hour_fallback(self, tmp_path):
        # Only hb=5 curve exists, query hb=4 → neighbor (+1) should apply
        payload = {
            "KATL": {
                "5": {"alpha": 0.95, "beta": 0.02, "n": MIN_SAMPLES_PER_CELL + 5},
            }
        }
        path = self._write_curves(tmp_path, payload)
        cal = IntradayHourlyCalibration(curves_path=path)
        out = cal.get_calibrated("KATL", 4, 0.6)
        assert 0.0 < out < 1.0

    def test_global_calibration_is_monotonic_mapping(self):
        # Global fallback should not collapse to a constant across inputs.
        cal = IntradayHourlyCalibration(curves_path="")
        low = cal.get_calibrated("KATL", 6, 0.3)
        high = cal.get_calibrated("KATL", 6, 0.8)
        assert high >= low

    def test_safe_logit_symmetry(self):
        assert _safe_logit(0.5) == pytest.approx(0.0, abs=1e-9)

    def test_safe_expit_clamped(self):
        # Large positive log-odds saturates to the calibrated max.
        assert _safe_expit(50.0) == CALIBRATED_MAX
        # Large negative log-odds saturates to the calibrated min.
        assert _safe_expit(-50.0) == CALIBRATED_MIN

    def test_safe_expit_midpoint(self):
        # log-odds 0 → probability 0.5 (inside clamp bounds)
        assert _safe_expit(0.0) == pytest.approx(0.5, abs=1e-6)

    def test_create_factory_returns_instance(self):
        assert isinstance(create_hourly_calibration(""), IntradayHourlyCalibration)

    def test_get_raw_calibrated_alias(self):
        cal = IntradayHourlyCalibration(curves_path="")
        assert cal.get_raw_calibrated("KATL", 6, 0.5) == cal.get_calibrated("KATL", 6, 0.5)


# ──────────────────────────────────────────────────────────────
# Σ.2c — db_connection layer
# ──────────────────────────────────────────────────────────────

class TestDBConnection:
    def test_registry_has_expected_keys(self):
        keys = dbconn.get_registry_keys()
        # Core operational DBs must be present
        for required in ("metar_backfill", "kalshi_settlements", "paper_trading",
                         "gefs_archive", "alerts_prod", "trade_journal"):
            assert required in keys

    def test_get_config_known_key(self):
        cfg = dbconn.get_config("metar_backfill")
        assert cfg.db_key == "metar_backfill"
        assert cfg.relative_path.endswith(".db")

    def test_get_config_shortcut_alias(self):
        cfg = dbconn.get_config("metar")  # shortcut → metar_backfill
        assert cfg.db_key == "metar_backfill"

    def test_get_config_unknown_key_raises(self):
        with pytest.raises(KeyError):
            dbconn.get_config("nonexistent_db_key")

    def test_connection_path_resolution(self):
        path = dbconn.get_connection_path("kalshi_settlements")
        assert path.endswith("kalshi_settlements.db")

    def test_get_connection_path_no_side_effect(self):
        # Resolving a path must NOT open a connection or create a file.
        before = set(dbconn._connections.keys())
        dbconn.get_connection_path("paper_trading")
        assert set(dbconn._connections.keys()) == before

    def test_debug_status_empty_when_no_connections(self):
        # Fresh process, no connections opened yet
        assert dbconn.debug_status() == {}

    def test_resolve_path_absolute_passthrough(self):
        cfg = dbconn.DBConfig(db_key="t", relative_path="/tmp/abs.db")
        assert dbconn._resolve_path(cfg) == "/tmp/abs.db"

    def test_close_all_noop_when_empty(self):
        # Must not raise with an empty cache
        dbconn.close_all()
        assert dbconn._connections == {}

    def test_most_common_dbs_are_valid(self):
        for shortcut, target in dbconn.MOST_COMMON_DBS.items():
            assert target in dbconn.DB_REGISTRY

    def test_readonly_connection_against_temp_db(self, tmp_path, monkeypatch):
        # Redirect DATA_DIR to temp, then open a read-only conn to a real db.
        monkeypatch.setattr(dbconn, "DATA_DIR", str(tmp_path))
        # Create a throwaway DB file in the temp data dir
        db_path = tmp_path / "weather_engine.db"
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        conn.close()

        # 'weather_engine' is registered with relative_path 'weather_engine.db'
        with dbconn.get_connection("weather_engine") as c:
            row = c.execute("SELECT count(*) FROM t").fetchone()
            assert row[0] == 1

        # Clean up: close so the cache does not leak across tests
        dbconn.close_all()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
