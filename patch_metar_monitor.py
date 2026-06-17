import re

# Read the original file
with open('/home/node/.openclaw/workspace/prototypes/weather-engine-source/core/metar_monitor.py', 'r') as f:
    content = f.read()

# The LOW momentum detection code to insert
low_momentum_code = '''                # LOW momentum detection (downward temperature trend)
                momentum_down = None
                distance_from_integer = float(now_f) - float(int(math.floor(now_f)))
                monotonic_down = False
                increasing_time = False
                movement_down = False
                if len(window) == _SIGNAL_MOMENTUM_WINDOW_SIZE:
                    x1, x2, x3 = window[0], window[1], window[2]
                    monotonic_down = x1["temp_f"] >= x2["temp_f"] >= x3["temp_f"]
                    # Timestamps increase from oldest (x1) to newest (x3)
                    increasing_time = x1["seconds"] < x2["seconds"] < x3["seconds"]
                    movement_down = (x1["temp_f"] - x3["temp_f"]) >= 0.05
                    total_seconds = x3["seconds"] - x1["seconds"]
                    if increasing_time and total_seconds > 0:
                        momentum_down = abs((x1["temp_f"] - x3["temp_f"]) / total_seconds)

                # LOW momentum signals for downward transitions
                if transition_type in ("instant_down", "reversion_after_settlement"):
                    # Check near_boundary_momentum_down
                    near_boundary_down_all = False
                    if 0.0 < distance_from_integer <= 0.10:
                        near_boundary_down_all = bool(
                            monotonic_down
                            and increasing_time
                            and movement_down
                            and momentum_down is not None
                            and momentum_down >= 0.002
                        )
                    if near_boundary_down_all and not station_cooldown_active and not boundary_cooldown_active:
                        boundary_key = (station, int(math.floor(now_f)), int(epoch_id))
                        pending_signal_context = {
                            "signal_type": "near_boundary_momentum_down",
                            "signal_version": 1,
                            "station": station,
                            "obs_time": obs_time,
                            "dedupe_key": f"near_boundary_momentum_down:{station}:{epoch_id}:{int(math.floor(now_f))}",
                            "cooldown_applied": True,
                            "cooldown_seconds": _SIGNAL_BOUNDARY_COOLDOWN_SECONDS,
                            "distance_from_integer": distance_from_integer,
                            "momentum_f_per_sec": momentum_down,
                            "momentum_window_size": _SIGNAL_MOMENTUM_WINDOW_SIZE,
                            "lower_integer_boundary": int(math.floor(now_f)),
                            "pressure_from_boundary_seconds": distance_from_integer / momentum_down if momentum_down else None,
                        }
                        _SIGNAL_STATION_LAST_EMIT[station] = obs_seconds
                        _SIGNAL_BOUNDARY_LAST_EMIT[boundary_key] = obs_seconds
                        runtime.update({"signal_type": "near_boundary_momentum_down", "signal_emitted": True, "suppression_reason": None})
                        runtime["cooldown_state"]["station_active"] = True
                        runtime["cooldown_state"]["boundary_active"] = True

                    # Check goldilocks_momentum_down
                    if pending_signal_context is None:
                        epoch_key = (station, int(epoch_id))
                        tracker = _SIGNAL_GOLDILOCKS_EPOCH_TRACKER.get(epoch_key)
                        if isinstance(tracker, dict):
                            current_goldilocks = tracker.get("momentum_down_observed", False)
                            if not current_goldilocks and tracker.get("exceeded_by_one_or_more"):
                                # Check if temperature has dropped below the settlement bucket threshold
                                if float(now_f) <= float(tracker.get("settlement_bucket_at_up") or settlement_bucket) - 0.2:
                                    pending_signal_context = {
                                        "signal_type": "goldilocks_momentum_down",
                                        "signal_version": 1,
                                        "station": station,
                                        "obs_time": obs_time,
                                        "dedupe_key": f"goldilocks_momentum_down:{station}:{epoch_id}",
                                        "cooldown_applied": True,
                                        "cooldown_seconds": _SIGNAL_STATION_COOLDOWN_SECONDS,
                                        "settlement_bucket_at_up": int(tracker.get("settlement_bucket_at_up") or settlement_bucket),
                                        "max_temp_after_up": float(tracker.get("max_temp_after_up") or now_f),
                                        "reverted_temp": float(now_f),
                                        "momentum_down": momentum_down,
                                        "epoch_id": int(epoch_id),
                                    }
                                    tracker["momentum_down_observed"] = True
                                    _SIGNAL_STATION_LAST_EMIT[station] = obs_seconds
                                    runtime.update({"signal_type": "goldilocks_momentum_down", "signal_emitted": True, "suppression_reason": None})
                                    runtime["cooldown_state"]["station_active"] = True

'''

# Find and replace the section
pattern = r'(                runtime\["cooldown_state"\]\["station_active"\] = True\n\n)(                if runtime\["signal_type"\] is None)'
replacement = r'\1' + low_momentum_code + r'\2'

new_content = re.sub(pattern, replacement, content)

# Write the modified content
with open('/home/node/.openclaw/workspace/prototypes/weather-engine-source/core/metar_monitor.py', 'w') as f:
    f.write(new_content)

print("File updated successfully")
