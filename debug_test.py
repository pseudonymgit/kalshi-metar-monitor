import sys
sys.path.insert(0, '.')

from core import metar_monitor

# Clear state
metar_monitor._SIGNAL_OBSERVATION_WINDOWS.clear()
metar_monitor._SIGNAL_STATION_LAST_EMIT.clear()
metar_monitor._SIGNAL_BOUNDARY_LAST_EMIT.clear()
metar_monitor._SIGNAL_EPOCH_COUNTER.clear()
metar_monitor._SIGNAL_GOLDILOCKS_EPOCH_TRACKER.clear()
metar_monitor._LATEST_SIGNAL_RUNTIME.clear()
metar_monitor._LAST_SETTLEMENT_UP_TS.clear()

# Set up state
state = {
    "last_observed_integer": 69,
    "running_daily_max": 69.9,
    "last_settlement_bucket": 69,
    "last_instant_bucket": 69,
}

# Run sequence
observations = [
    (69.05, "2025-01-01T12:00:00+00:00"),  # floor=69
    (69.02, "2025-01-01T12:00:10+00:00"),  # floor=69
    (68.98, "2025-01-01T12:00:20+00:00"),  # floor=68, reversion_after_settlement
]

def fake_read_temperature_state(_icao):
    return dict(state)

def fake_commit_temperature_state(*, icao, curr_floor, running_daily_max, settlement_bucket, instant_bucket):
    del icao
    state["last_observed_integer"] = curr_floor
    state["running_daily_max"] = running_daily_max
    state["last_settlement_bucket"] = settlement_bucket
    state["last_instant_bucket"] = instant_bucket

def fake_emit_transition_if_changed(**kwargs):
    result = {
        "station": kwargs.get("station"),
        "timestamp_utc": kwargs.get("metadata", {}).get("obs_time"),
        "transition_event_id": 1,
    }
    return result

emitted = []

def fake_emit_signal_alert(*, station, obs_time, temp_f, signal_context, cfg):
    del cfg
    emitted.append({
        "station": station,
        "obs_time": obs_time,
        "temp_f": temp_f,
        "signal_type": signal_context.get("signal_type"),
    })
    print(f"emit_signal_alert: {signal_context.get('signal_type')}")

from unittest.mock import patch

# Patch the _persist_signal_state to see if it's being called
persist_calls = []

def fake_persist_signal_state(name, state_dict):
    persist_calls.append((name, state_dict))
    print(f"_persist_signal_state: {name}")

with patch("core.metar_monitor._maybe_daily_reset_local", return_value=None), \
    patch("core.metar_monitor.read_temperature_state", side_effect=fake_read_temperature_state), \
    patch("core.metar_monitor.commit_temperature_state", side_effect=fake_commit_temperature_state), \
    patch("core.metar_monitor.emit_transition_if_changed", side_effect=fake_emit_transition_if_changed), \
    patch("core.metar_monitor.get_latest_station_market_evaluation_context", return_value={"KDEN": {"market_eligibility_runtime": {"eligible_markets_count": 2}}}), \
    patch("core.kalshi_monitor.get_hydration_prerequisite_state_snapshot", return_value={"KDEN": {"cache_valid": True}}), \
    patch("core.metar_monitor._emit_signal_alert", side_effect=fake_emit_signal_alert), \
    patch("core.metar_monitor._persist_signal_state", side_effect=fake_persist_signal_state):
    for temp_f, obs_time in observations:
        result = metar_monitor._process_temperature_event(
            icao="KDEN",
            temp_f=temp_f,
            obs_time=obs_time,
            cfg={"webhook": ""},
            last_temp_f=temp_f,
            allow_alert_delivery=False,
        )

print(f"\nEmitted signals: {[e['signal_type'] for e in emitted]}")
print(f"Persist calls: {persist_calls}")

# Check the signal runtime for the last observation
runtime = metar_monitor.get_latest_station_signal_runtime("KDEN")
print(f"Signal runtime: {runtime}")
