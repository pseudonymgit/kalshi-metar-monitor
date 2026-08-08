"""
Unit tests for the WeatherAPI P0 signals:
  - P0-1: Cloud Cover Index (CCI)
  - P0-2: Feels-Like Delta (ΔFL)

Verifies:
  - Daily metric computation (6-hour rolling mean for CCI, mean delta for ΔFL)
  - Evaluate() contract: (direction, confidence), direction ∈ {up, down, None}
  - Climatology baseline gating (insufficient samples → (None, 0.0))
  - Direction semantics (cloudier → down, clearer → up; heat index → up,
    wind chill → down)
  - Confidence clamping (0.0-1.0, capped at 0.6)
  - DB-backed evaluate_for_station reads the correct `hourly` tables
  - Registration in the SignalRegistry
"""

import os
import sqlite3
import sys
from pathlib import Path

import pytest

# Ensure repo root is on sys.path
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.signals.cloud_cover_index_signal import CloudCoverIndexSignal
from core.signals.feels_like_delta_signal import FeelsLikeDeltaSignal
from core.signals import SignalRegistry

TEST_DB = str(Path(_REPO_ROOT) / "data" / "weatherapi_archive.db")


# ─── Helpers ─────────────────────────────────────────────────────────────

def make_daily_series(n, month="06", metric_key="cci", base=0.4):
    """Build a daily metric series with a constant climatological baseline.

    All values equal `base` so the baseline std collapses to the 0.01 floor
    and a "normal" target day (== base) yields z == 0 → no signal.
    """
    return [{"date": f"2020-{month}-{(i % 28) + 1:02d}", metric_key: base}
            for i in range(n)]


def make_hour_map_cloud(values):
    """Build a {hour: (cloud, temp_c, feelslike_c)} map from a cloud list."""
    return {h: (c, 20.0, 19.0) for h, c in enumerate(values)}


def make_hour_map_delta(values):
    """Build a {hour: (cloud, temp_c, feelslike_c)} map from a delta list."""
    return {h: (40, 20.0, 20.0 + d) for h, d in enumerate(values)}


# ─── CCI: daily metric computation ───────────────────────────────────────

class TestCloudCoverIndexMetric:
    def test_clear_sky_metric_zero(self):
        sig = CloudCoverIndexSignal()
        m = sig._metric_from_hour_map(make_hour_map_cloud([0, 0, 0, 0, 0, 0]))
        assert m == pytest.approx(0.0)

    def test_overcast_metric_one(self):
        sig = CloudCoverIndexSignal()
        m = sig._metric_from_hour_map(make_hour_map_cloud([100] * 6))
        assert m == pytest.approx(1.0)

    def test_partial_cloud_metric(self):
        sig = CloudCoverIndexSignal()
        # Uniform 50% cloud → all 6-h rolling windows = 0.5
        m = sig._metric_from_hour_map(make_hour_map_cloud([50] * 12))
        assert m == pytest.approx(0.5, abs=1e-6)

    def test_rolling_mean_weights_edge_hours(self):
        # Verify the 6-h rolling mean is used (not a plain mean). With
        # [0,50,100,0,50,100] the plain mean is 0.5 but the mean of all valid
        # 6-h rolling windows is 0.55625.
        sig = CloudCoverIndexSignal()
        m = sig._metric_from_hour_map(make_hour_map_cloud([0, 50, 100, 0, 50, 100]))
        assert m == pytest.approx(0.55625, abs=1e-6)

    def test_null_cloud_skipped(self):
        sig = CloudCoverIndexSignal()
        m = sig._metric_from_hour_map({0: (None, 20.0, 19.0), 1: (50, 20, 19)})
        assert m == pytest.approx(0.5)

    def test_all_null_returns_none(self):
        sig = CloudCoverIndexSignal()
        assert sig._metric_from_hour_map({0: (None, 20, 19)}) is None

    def test_sparse_hours_falls_back_to_plain_mean(self):
        # Only 2 hours -> no valid 6-h window (needs >=3) -> plain mean
        sig = CloudCoverIndexSignal()
        m = sig._metric_from_hour_map(make_hour_map_cloud([100, 0]))
        assert m == pytest.approx(0.5)

    def test_six_hour_rolling_mean(self):
        # 12 hours: 12 x 100 then 0... check rolling windows mix
        sig = CloudCoverIndexSignal()
        vals = [100] * 6 + [0] * 6
        m = sig._metric_from_hour_map(make_hour_map_cloud(vals))
        assert 0.0 < m < 1.0


# ─── CCI: evaluate() ─────────────────────────────────────────────────────

class TestCloudCoverIndexEvaluate:
    def test_insufficient_samples_returns_none(self):
        sig = CloudCoverIndexSignal()
        days = make_daily_series(5, metric_key="cci")
        assert sig.evaluate(4, days) == (None, 0.0)

    def test_cloudier_than_normal_predicts_down(self):
        sig = CloudCoverIndexSignal()
        days = make_daily_series(60, metric_key="cci", base=0.4)
        # make the last day (idx-1) very cloudy
        days[-1]["cci"] = 0.95
        direction, conf = sig.evaluate(len(days), days)
        assert direction == "down"
        assert 0.0 < conf <= 0.6

    def test_clearer_than_normal_predicts_up(self):
        sig = CloudCoverIndexSignal()
        days = make_daily_series(60, metric_key="cci", base=0.4)
        days[-1]["cci"] = 0.02
        direction, conf = sig.evaluate(len(days), days)
        assert direction == "up"
        assert 0.0 < conf <= 0.6

    def test_normal_cloud_no_signal(self):
        sig = CloudCoverIndexSignal()
        days = make_daily_series(60, metric_key="cci", base=0.4)
        direction, conf = sig.evaluate(len(days), days)
        assert direction is None
        assert conf == 0.0

    def test_confidence_clamped(self):
        sig = CloudCoverIndexSignal()
        days = make_daily_series(60, metric_key="cci", base=0.4)
        days[-1]["cci"] = 1.0  # extreme
        direction, conf = sig.evaluate(len(days), days)
        assert conf <= 0.6

    def test_name_and_lookback(self):
        sig = CloudCoverIndexSignal()
        assert sig.name == "cloud_cover_index"
        assert sig.min_lookback > 1


# ─── ΔFL: daily metric computation ───────────────────────────────────────

class TestFeelsLikeDeltaMetric:
    def test_heat_index_positive(self):
        sig = FeelsLikeDeltaSignal()
        m = sig._metric_from_hour_map(make_hour_map_delta([5.0, 6.0, 7.0]))
        assert m == pytest.approx(6.0)

    def test_wind_chill_negative(self):
        sig = FeelsLikeDeltaSignal()
        m = sig._metric_from_hour_map(make_hour_map_delta([-10.0, -12.0, -14.0]))
        assert m == pytest.approx(-12.0)

    def test_null_fields_skipped(self):
        sig = FeelsLikeDeltaSignal()
        m = sig._metric_from_hour_map(
            {0: (40, None, 19.0), 1: (40, 20.0, 25.0), 2: (40, 20.0, None)}
        )
        assert m == pytest.approx(5.0)

    def test_all_null_returns_none(self):
        sig = FeelsLikeDeltaSignal()
        assert sig._metric_from_hour_map({0: (40, None, None)}) is None


# ─── ΔFL: evaluate() ─────────────────────────────────────────────────────

class TestFeelsLikeDeltaEvaluate:
    def test_insufficient_samples_returns_none(self):
        sig = FeelsLikeDeltaSignal()
        days = make_daily_series(5, metric_key="feels_delta", base=2.0)
        assert sig.evaluate(4, days) == (None, 0.0)

    def test_heat_index_anomaly_predicts_up(self):
        sig = FeelsLikeDeltaSignal()
        days = make_daily_series(60, metric_key="feels_delta", base=2.0)
        days[-1]["feels_delta"] = 15.0  # strong heat index
        direction, conf = sig.evaluate(len(days), days)
        assert direction == "up"
        assert 0.0 < conf <= 0.6

    def test_wind_chill_anomaly_predicts_down(self):
        sig = FeelsLikeDeltaSignal()
        days = make_daily_series(60, metric_key="feels_delta", base=2.0)
        days[-1]["feels_delta"] = -18.0  # strong wind chill
        direction, conf = sig.evaluate(len(days), days)
        assert direction == "down"
        assert 0.0 < conf <= 0.6

    def test_normal_delta_no_signal(self):
        sig = FeelsLikeDeltaSignal()
        days = make_daily_series(60, metric_key="feels_delta", base=2.0)
        direction, conf = sig.evaluate(len(days), days)
        assert direction is None
        assert conf == 0.0

    def test_name_and_lookback(self):
        sig = FeelsLikeDeltaSignal()
        assert sig.name == "feels_like_delta"
        assert sig.min_lookback > 1


# ─── DB-backed path (reads correct hourly table) ─────────────────────────

@pytest.fixture()
def tmp_weatherapi_db(tmp_path):
    """Create a temp WeatherAPI-archive DB with the `hourly` + `daily` tables."""
    db = tmp_path / "weatherapi_archive.db"
    conn = sqlite3.connect(db)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE hourly (
            station TEXT NOT NULL,
            date TEXT NOT NULL,
            hour INTEGER NOT NULL,
            temp_c REAL,
            temp_f REAL,
            condition_text TEXT,
            humidity REAL,
            pressure_mb REAL,
            wind_kph REAL,
            precip_mm REAL,
            cloud INTEGER,
            feelslike_c REAL,
            source TEXT,
            PRIMARY KEY (station, date, hour)
        )
    """)
    cur.execute("""
        CREATE TABLE daily (
            station TEXT NOT NULL,
            date TEXT NOT NULL,
            max_temp_c REAL,
            min_temp_c REAL,
            avg_temp_c REAL,
            max_temp_f REAL,
            min_temp_f REAL,
            avg_temp_f REAL,
            condition_text TEXT,
            humidity REAL,
            pressure_mb REAL,
            wind_kph REAL,
            precip_mm REAL,
            source TEXT,
            PRIMARY KEY (station, date)
        )
    """)
    conn.commit()
    yield conn
    conn.close()


def _insert_hourly(conn, station, date, hour, cloud, temp_c, feelslike_c):
    conn.execute(
        """INSERT INTO hourly (station, date, hour, cloud, temp_c, feelslike_c)
           VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(station, date, hour) DO NOTHING""",
        (station, date, hour, cloud, temp_c, feelslike_c),
    )


def _unique_dates(n):
    """Yield `n` unique ISO dates spanning June–July 2020."""
    for i in range(n):
        month = 6 + (i // 30)
        day = (i % 30) + 1
        yield f"2020-{month:02d}-{day:02d}"


class TestDBPath:
    def test_cloud_cover_reads_hourly(self, tmp_weatherapi_db):
        conn = tmp_weatherapi_db
        # 59 unique baseline days spanning June/July 2020, excluding the target
        # day (2020-06-15). June then has 29 baseline samples (>= 20 required).
        target = "2020-06-15"
        for d, date in enumerate(_unique_dates(60)):
            if date == target:
                continue
            for h in range(4):
                _insert_hourly(conn, "KATL", date, h, 40, 20.0, 19.0)
        # target day: fully overcast
        for h in range(4):
            _insert_hourly(conn, "KATL", target, h, 100, 20.0, 19.0)

        conn.commit()
        sig = CloudCoverIndexSignal()
        direction, conf = sig.evaluate_for_station("KATL", target, conn=conn)
        assert direction == "down"
        assert conf > 0.0

    def test_feels_delta_reads_hourly(self, tmp_weatherapi_db):
        conn = tmp_weatherapi_db
        target = "2020-06-15"
        # baseline: negligible delta (feelslike ~ temp)
        for d, date in enumerate(_unique_dates(60)):
            if date == target:
                continue
            for h in range(4):
                _insert_hourly(conn, "KBOS", date, h, 40, 20.0, 20.0)
        # target day: strong heat index (feelslike much warmer)
        for h in range(4):
            _insert_hourly(conn, "KBOS", target, h, 40, 20.0, 30.0)

        conn.commit()
        sig = FeelsLikeDeltaSignal()
        direction, conf = sig.evaluate_for_station("KBOS", target, conn=conn)
        assert direction == "up"
        assert conf > 0.0

    def test_unknown_station_returns_none(self, tmp_weatherapi_db):
        sig = CloudCoverIndexSignal()
        direction, conf = sig.evaluate_for_station(
            "KZZZ", "2020-06-29", conn=tmp_weatherapi_db
        )
        assert direction is None
        assert conf == 0.0

    def test_missing_date_returns_none(self, tmp_weatherapi_db):
        for d, date in enumerate(_unique_dates(60)):
            for h in range(4):
                _insert_hourly(tmp_weatherapi_db, "KATL", date, h, 40, 20.0, 19.0)
        tmp_weatherapi_db.commit()
        sig = CloudCoverIndexSignal()
        # 2020-06-31 does not exist (June has 30 days) → not in the series,
        # but the June month climatology is well-populated.
        direction, conf = sig.evaluate_for_station(
            "KATL", "2020-06-31", conn=tmp_weatherapi_db
        )
        assert direction is None
        assert conf == 0.0


# ─── Registration ────────────────────────────────────────────────────────

class TestRegistration:
    def test_registered_in_registry(self):
        registry = SignalRegistry(db_path=TEST_DB)
        assert "cloud_cover_index" in registry.get_all_signals()
        assert "feels_like_delta" in registry.get_all_signals()
        assert registry.get_signal("cloud_cover_index").name == "cloud_cover_index"
        assert registry.get_signal("feels_like_delta").name == "feels_like_delta"

    def test_import_via_package(self):
        from core.signals import CloudCoverIndexSignal, FeelsLikeDeltaSignal
        assert CloudCoverIndexSignal is not None
        assert FeelsLikeDeltaSignal is not None