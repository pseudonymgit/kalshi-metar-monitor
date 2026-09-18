"""Regression coverage for the 2026-08-16 readiness compatibility repairs."""

from unittest.mock import patch

from core.signals.metar_nowcast_signal import MetarNowcastSignal
from core.signals import SignalRegistry
from core import metar_monitor


def test_metar_nowcast_station_api_and_no_data_bucket_contract():
    signal = MetarNowcastSignal()
    result = signal.evaluate(
        station="KXYZ",
        bucket_temp_f=75,
        gefs_max_f=85.0,
        gefs_min_f=65.0,
    )
    assert result["signal"] is False
    assert result["confidence"] == 0.0
    assert result["direction"] is None
    assert result["metar_fresh"] is False

    bucket = signal.evaluate_bucket(
        station="KXYZ",
        bucket_temp_f=75,
        gefs_max_f=85.0,
        gefs_min_f=65.0,
    )
    assert {"confidence", "direction", "metar_temp_f", "metar_fresh", "reason"} <= bucket.keys()
    assert bucket["direction"] is None


def test_cloud_cover_and_feels_like_are_registered():
    registry = SignalRegistry(db_path=None)
    assert registry.get_signal("cloud_cover_index").name == "cloud_cover_index"
    assert registry.get_signal("feels_like_delta").name == "feels_like_delta"


def test_legacy_goldilocks_tracker_keeps_historical_wire_name():
    metar_monitor._SIGNAL_EPOCH_COUNTER["KDEN"] = 1
    metar_monitor._SIGNAL_GOLDILOCKS_EPOCH_TRACKER[("KDEN", 1)] = {
        "settlement_bucket_at_up": 70,
        "max_temp_after_up": 71.3,
        "exceeded_by_one_or_more": True,
        "reverted_below_settlement": False,
        "alert_emitted": False,
        "settlement_up_obs_time": "2025-01-01T12:00:00+00:00",
    }

    state = {
        "last_observed_integer": 69,
        "running_daily_max": 69.9,
        "last_settlement_bucket": 69,
        "last_instant_bucket": 69,
    }
    emitted = []

    with patch("core.metar_monitor._maybe_daily_reset_local"), \
        patch("core.metar_monitor.read_temperature_state", return_value=dict(state)), \
        patch("core.metar_monitor.commit_temperature_state"), \
        patch("core.metar_monitor.emit_transition_if_changed", return_value={}), \
        patch("core.metar_monitor.get_latest_station_market_evaluation_context", return_value={"KDEN": {"market_eligibility_runtime": {"eligible_markets_count": 2}}}), \
        patch("core.kalshi_monitor.get_hydration_prerequisite_state_snapshot", return_value={"KDEN": {"cache_valid": True}}), \
        patch("core.metar_monitor._emit_signal_alert", side_effect=lambda **kwargs: emitted.append(kwargs["signal_context"]["signal_type"])):
        metar_monitor._process_temperature_event(
            icao="KDEN",
            temp_f=69.7,
            obs_time="2025-01-01T12:02:00+00:00",
            cfg={"webhook": ""},
            last_temp_f=69.7,
            allow_alert_delivery=False,
        )

    assert "goldilocks_reversion_alert" in emitted
