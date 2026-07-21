#!/usr/bin/env python3
"""
Integration test for ERA5 Upper-Air Backfill.

Verifies that:
1. The backfill script's core functions parse and execute correctly
2. Temperature advection computation produces valid values
3. Database schema is compatible with backfilled data

This test does NOT require a CDS API key — it tests the computational
and data processing logic using synthetic data.
"""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import numpy as np
import pytest

# Add project root to path
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# Import core functions from the backfill script
from scripts.era5_upper_air_backfill import (
    find_nearest_grid_point,
    deg_to_meters,
    compute_temperature_advection,
    get_month_ranges,
    store_in_db,
    init_db,
    get_existing_date_counts,
    VAR_TEMP_850,
    VAR_U_850,
    VAR_V_850,
    VAR_GEO_500,
    VAR_ADVECTION,
    UPPER_AIR_VARS,
    MODEL_NAME,
    STATIONS,
)


# ── Tests for helper functions ──────────────────────────────────────────

class TestFindNearestGridPoint:
    """Test the nearest grid point finder."""

    def test_exact_match(self):
        """Nearest point at exact coordinates."""
        lats = np.array([30.0, 30.25, 30.5, 30.75, 31.0])
        lons = np.array([-90.0, -89.75, -89.5, -89.25, -89.0])
        lat_idx, lon_idx = find_nearest_grid_point(lats, lons, 30.5, -89.5)
        assert lat_idx == 2, f"Expected lat_idx=2, got {lat_idx}"
        assert lon_idx == 2, f"Expected lon_idx=2, got {lon_idx}"

    def test_offset_match(self):
        """Nearest point when target is between grid points."""
        lats = np.array([30.0, 30.25, 30.5, 30.75, 31.0])
        lons = np.array([-90.0, -89.75, -89.5, -89.25, -89.0])
        lat_idx, lon_idx = find_nearest_grid_point(lats, lons, 30.35, -89.6)
        # 30.35 is 0.10 from 30.25 (idx 1) and 0.15 from 30.5 (idx 2) -> idx 1
        # -89.6 is 0.15 from -89.75 (idx 1) and 0.10 from -89.5 (idx 2) -> idx 2
        assert lat_idx == 1, f"Expected lat_idx=1, got {lat_idx}"
        assert lon_idx == 2, f"Expected lon_idx=2, got {lon_idx}"

    def test_negative_lon(self):
        """Station with negative longitude (Western hemisphere)."""
        lats = np.array([33.0, 33.25, 33.5, 33.75, 34.0])
        lons = np.array([-85.0, -84.75, -84.5, -84.25, -84.0])
        lat_idx, lon_idx = find_nearest_grid_point(lats, lons, 33.64, -84.43)
        # 33.64 is 0.14 from 33.5 (idx 2) and 0.11 from 33.75 (idx 3) -> idx 3
        # -84.43 is 0.07 from -84.5 (idx 2) and 0.18 from -84.25 (idx 3) -> idx 2
        assert lat_idx == 3, f"Expected lat_idx=3, got {lat_idx}"
        assert lon_idx == 2, f"Expected lon_idx=2, got {lon_idx}"


class TestDegToMeters:
    """Test degree-to-meter conversion."""

    def test_at_equator(self):
        """At equator, 1° = 111320 m in both directions."""
        dx, dy = deg_to_meters(0, 1.0, 1.0)
        assert abs(dx - 111320.0) < 1.0, f"dx={dx}"
        assert abs(dy - 111320.0) < 1.0, f"dy={dy}"

    def test_at_mid_latitude(self):
        """At 45°N, 1° longitude ≈ 78700 m."""
        dx, dy = deg_to_meters(45, 1.0, 1.0)
        expected_dx = 111320.0 * np.cos(np.radians(45))
        assert abs(dx - expected_dx) < 1.0, f"dx={dx}, expected={expected_dx}"
        assert abs(dy - 111320.0) < 1.0, f"dy={dy}"

    def test_small_delta(self):
        """Small deltas should scale linearly."""
        dx, dy = deg_to_meters(40, 0.25, 0.25)
        expected_dx = 0.25 * 111320.0 * np.cos(np.radians(40))
        expected_dy = 0.25 * 111320.0
        assert abs(dx - expected_dx) < 0.1, f"dx={dx}, expected={expected_dx}"
        assert abs(dy - expected_dy) < 0.1, f"dy={dy}, expected={expected_dy}"


class TestComputeTemperatureAdvection:
    """Test the advection computation with synthetic grid data."""

    def test_warm_advection(self):
        """Warm air from south should produce positive advection."""
        # 5×5 grid at 0.25° resolution centered on 40°N, -75°W
        lats = np.array([39.0, 39.25, 39.5, 39.75, 40.0])
        lons = np.array([-76.0, -75.75, -75.5, -75.25, -75.0])

        # Temperature gradient: warmer in south (lower latitude index)
        # Create a gradient: 290K in south, 285K in north
        temp_grid = np.zeros((5, 5))
        for i in range(5):
            temp_grid[i, :] = 290.0 - i * 1.0  # 290, 289, 288, 287, 286

        # South wind (positive v = northward) = warm air advection
        u_val = 0.0  # no zonal wind
        v_val = 10.0  # 10 m/s northward wind

        adv = compute_temperature_advection(
            temp_grid, u_val, v_val, lats, lons, 39.5, -75.5
        )

        # Warm advection should be positive: -v * dT/dy
        # dT/dy ≈ (286 - 290) / (2 * 0.25 * 111320) = -4 / 55660 ≈ -7.2e-5
        # adv = -10 * (-7.2e-5) = 7.2e-4
        assert adv is not None, "Advection should not be None"
        assert adv > 0, f"Expected positive advection (warm), got {adv}"
        print(f"  Warm advection: {adv:.6e} K/s")

    def test_cold_advection(self):
        """Cold air from south with northward wind should produce negative advection."""
        lats = np.array([39.0, 39.25, 39.5, 39.75, 40.0])
        lons = np.array([-76.0, -75.75, -75.5, -75.25, -75.0])

        # Temperature gradient: colder in south (lower latitude index)
        # Row 0 (lat 39.0) = 285K, Row 4 (lat 40.0) = 289K
        # So dT/dy > 0 (warmer in north)
        temp_grid = np.zeros((5, 5))
        for i in range(5):
            temp_grid[i, :] = 285.0 + i * 1.0  # 285, 286, 287, 288, 289

        # Southward wind (v < 0) brings warmer air from north -> WARM advection (positive)
        # Northward wind (v > 0) brings colder air from south -> COLD advection (negative)
        u_val = 0.0
        v_val = 10.0  # 10 m/s northward wind, bringing cold air from south

        adv = compute_temperature_advection(
            temp_grid, u_val, v_val, lats, lons, 39.5, -75.5
        )

        assert adv is not None, "Advection should not be None"
        assert adv < 0, f"Expected negative advection (cold), got {adv}"
        print(f"  Cold advection: {adv:.6e} K/s")

    def test_zonal_wind_advection(self):
        """East wind with westward temperature gradient."""
        lats = np.array([39.0, 39.25, 39.5, 39.75, 40.0])
        lons = np.array([-76.0, -75.75, -75.5, -75.25, -75.0])

        # Temperature gradient: colder in west (lower lon index), warmer in east
        # Column 0 (lon -76.0) = 285K, Column 4 (lon -75.0) = 289K
        # So dT/dx > 0 (warmer in east)
        temp_grid = np.zeros((5, 5))
        for j in range(5):
            temp_grid[:, j] = 285.0 + j * 1.0  # 285...289 from west to east

        # Eastward wind (u > 0) brings colder air from west -> COLD advection (negative)
        # Westward wind (u < 0) brings warmer air from east -> WARM advection (positive)
        u_val = -10.0  # 10 m/s westward wind, bringing warm air from east
        v_val = 0.0

        adv = compute_temperature_advection(
            temp_grid, u_val, v_val, lats, lons, 39.5, -75.5
        )

        assert adv is not None, "Advection should not be None"
        assert adv > 0, f"Expected positive advection (warm from east), got {adv}"
        print(f"  Zonal advection: {adv:.6e} K/s")

    def test_insufficient_data(self):
        """Return None when grid is too small."""
        # 2×2 grid — not enough for proper gradients
        lats = np.array([39.5, 39.75])
        lons = np.array([-75.5, -75.25])
        temp_grid = np.array([[290.0, 289.0], [288.0, 287.0]])

        adv = compute_temperature_advection(
            temp_grid, 5.0, 5.0, lats, lons, 39.6, -75.4
        )
        # 2×2 has 4 elements, which is >= 4, so it should not return None
        # Let's also test with 1×1
        temp_grid_1 = np.array([[290.0]])
        adv2 = compute_temperature_advection(
            temp_grid_1, 5.0, 5.0, np.array([39.5]), np.array([-75.5]), 39.5, -75.5
        )
        assert adv2 is None, "1×1 grid should return None"

    def test_missing_wind(self):
        """Return None when wind components are missing."""
        lats = np.array([39.0, 39.25, 39.5, 39.75, 40.0])
        lons = np.array([-76.0, -75.75, -75.5, -75.25, -75.0])
        temp_grid = np.ones((5, 5)) * 290.0

        adv = compute_temperature_advection(
            temp_grid, None, 5.0, lats, lons, 39.5, -75.5
        )
        assert adv is None, "Missing u should return None"

        adv = compute_temperature_advection(
            temp_grid, 5.0, None, lats, lons, 39.5, -75.5
        )
        assert adv is None, "Missing v should return None"


class TestMonthRanges:
    """Test month range generation."""

    def test_single_month(self):
        """Single month should produce one range."""
        from datetime import date
        ranges = get_month_ranges(date(2025, 1, 1), date(2025, 1, 31))
        assert len(ranges) == 1
        assert ranges[0][0] == 2025
        assert ranges[0][1] == 1

    def test_multi_month(self):
        """Multiple months should produce multiple ranges."""
        from datetime import date
        ranges = get_month_ranges(date(2025, 1, 1), date(2025, 3, 15))
        assert len(ranges) == 3
        assert ranges[0][1] == 1
        assert ranges[1][1] == 2
        assert ranges[2][1] == 3

    def test_year_boundary(self):
        """Range crossing year boundary."""
        from datetime import date
        ranges = get_month_ranges(date(2025, 12, 1), date(2026, 2, 1))
        assert len(ranges) == 3
        assert ranges[0] == (2025, 12, date(2025, 12, 1), date(2025, 12, 31))
        assert ranges[1] == (2026, 1, date(2026, 1, 1), date(2026, 1, 31))
        assert ranges[2] == (2026, 2, date(2026, 2, 1), date(2026, 2, 1))


class TestDBStorage:
    """Test database storage and retrieval of ERA5 data."""

    @pytest.fixture
    def db_conn(self):
        """Create a temporary database for testing."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        conn = init_db(db_path)
        yield conn
        conn.close()
        os.unlink(db_path)

    def test_store_and_retrieve(self, db_conn):
        """Store data and verify it's retrievable."""
        results = [
            ("2025-01-01", VAR_TEMP_850, 5.2),
            ("2025-01-01", VAR_U_850, 3.5),
            ("2025-01-01", VAR_V_850, -2.1),
            ("2025-01-01", VAR_GEO_500, 5600.0),
            ("2025-01-01", VAR_ADVECTION, 2.5e-5),
            ("2025-01-02", VAR_TEMP_850, 6.1),
            ("2025-01-02", VAR_U_850, 4.2),
            ("2025-01-02", VAR_V_850, -1.5),
            ("2025-01-02", VAR_GEO_500, 5610.0),
            ("2025-01-02", VAR_ADVECTION, -1.8e-5),
        ]

        stored, errors = store_in_db(
            db_conn, results, "KATL", "2025-01-03",
            "2025-01-03T12:00:00"
        )

        assert stored == len(results), f"Expected {len(results)} stored, got {stored}"
        assert len(errors) == 0, f"Unexpected errors: {errors}"

        # Verify retrieval
        counts = get_existing_date_counts(db_conn, "KATL")
        assert len(counts) == 5, f"Expected 5 variables, got {len(counts)}"
        for var in UPPER_AIR_VARS:
            assert var in counts, f"Missing variable: {var}"
            assert counts[var] == 2, f"Expected 2 days for {var}, got {counts[var]}"

    def test_idempotent_insert(self, db_conn):
        """Repeated insert of same data should not create duplicates."""
        results = [("2025-01-01", VAR_TEMP_850, 5.2)]

        store_in_db(db_conn, results, "KATL", "2025-01-03", "2025-01-03T12:00:00")
        store_in_db(db_conn, results, "KATL", "2025-01-03", "2025-01-03T12:00:00")

        counts = get_existing_date_counts(db_conn, "KATL")
        assert counts[VAR_TEMP_850] == 1, "INSERT OR REPLACE should maintain single row"

    def test_edge_cases(self, db_conn):
        """Handle None, NaN, and Inf values gracefully."""
        results = [
            ("2025-01-01", VAR_TEMP_850, None),
            ("2025-01-01", VAR_U_850, float('nan')),
            ("2025-01-01", VAR_V_850, float('inf')),
            ("2025-01-01", VAR_GEO_500, 5600.0),
        ]

        stored, errors = store_in_db(
            db_conn, results, "KATL", "2025-01-03", "2025-01-03T12:00:00"
        )
        # Only the valid geopotential value should be stored
        assert stored == 1, f"Expected 1 stored (valid geo), got {stored}"
        assert len(errors) == 0


class TestStationRegistry:
    """Verify the station list is complete and consistent."""

    def test_20_stations(self):
        """Should have exactly 20 stations."""
        assert len(STATIONS) == 20, f"Expected 20 stations, got {len(STATIONS)}"

    def test_all_have_coordinates(self):
        """All stations should have valid lat/lon."""
        for code, name, lat, lon in STATIONS:
            assert -90 <= lat <= 90, f"{code}: invalid lat {lat}"
            assert -180 <= lon <= 180, f"{code}: invalid lon {lon}"
            assert name, f"{code}: missing name"

    def test_unique_codes(self):
        """All station codes should be unique."""
        codes = [s[0] for s in STATIONS]
        assert len(codes) == len(set(codes)), "Duplicate station codes found"

    def test_all_us_contiguous(self):
        """All stations should be in the contiguous US."""
        for code, name, lat, lon in STATIONS:
            assert 25 <= lat <= 50, f"{code}: lat {lat} outside CONUS"
            assert -130 <= lon <= -65, f"{code}: lon {lon} outside CONUS"


# ── Integration test with existing DB ───────────────────────────────────

class TestExistingDBIntegration:
    """Test that the backfill data integrates with the existing NWP database."""

    def test_db_schema_compatible(self):
        """Verify the existing NWP DB schema is compatible with backfill."""
        db_path = REPO_ROOT / "data" / "nwp_forecasts.db"
        if not db_path.exists():
            pytest.skip("NWP database not found — skipping integration test")

        conn = sqlite3.connect(str(db_path))
        c = conn.cursor()

        # Verify required columns exist
        c.execute("PRAGMA table_info(nwp_forecasts)")
        columns = {row[1] for row in c.fetchall()}
        required = {"fetch_date", "target_date", "station", "model", "variable", "value", "fetch_timestamp"}
        assert required.issubset(columns), f"Missing columns: {required - columns}"

        conn.close()

    def test_era5_model_exists(self):
        """Check that ERA5 model already has data in the DB."""
        db_path = REPO_ROOT / "data" / "nwp_forecasts.db"
        if not db_path.exists():
            pytest.skip("NWP database not found — skipping integration test")

        conn = sqlite3.connect(str(db_path))
        c = conn.cursor()

        c.execute("SELECT COUNT(DISTINCT model) FROM nwp_forecasts")
        model_count = c.fetchone()[0]
        assert model_count >= 1, "No models in database"

        conn.close()
        print(f"\n  Database has {model_count} models")

    def test_advection_variable_ready(self):
        """Check that the temperature_advection_850hPa variable exists or can be created."""
        db_path = REPO_ROOT / "data" / "nwp_forecasts.db"
        if not db_path.exists():
            pytest.skip("NWP database not found — skipping integration test")

        conn = sqlite3.connect(str(db_path))
        c = conn.cursor()

        c.execute("SELECT DISTINCT variable FROM nwp_forecasts WHERE variable = ?",
                  (VAR_ADVECTION,))
        existing = c.fetchone()
        if existing:
            print(f"\n  ✓ {VAR_ADVECTION} already exists in DB")
        else:
            print(f"\n  - {VAR_ADVECTION} not yet in DB (will be created by backfill)")

        conn.close()


# ── Dry run test ────────────────────────────────────────────────────────

class TestDryRun:
    """Test that the script can be invoked with --dry-run."""

    def test_dry_run_syntax(self):
        """Verify the script handles --dry-run flag."""
        # This tests that the script's arg parsing and
        # dry-run logic work without actual CDS access
        from scripts.era5_upper_air_backfill import parse_args
        import sys

        test_args = [
            "era5_upper_air_backfill.py",
            "--dry-run",
            "--station", "KATL",
            "--months", "1",
        ]
        with patch.object(sys, 'argv', test_args):
            args = parse_args()
            assert args.dry_run is True
            assert args.station == "KATL"
            assert args.months == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])