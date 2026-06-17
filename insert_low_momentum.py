import re

# Read the original file
with open('/home/node/.openclaw/workspace/prototypes/weather-engine-source/core/metar_monitor.py', 'r') as f:
    content = f.read()

# Find the location to insert - right before "if runtime[\"signal_type\"] is None"
# and "pending_runtime_record = runtime"
insertion_point = '                if runtime["signal_type"] is None and runtime["suppression_reason"] is None:'

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
                        _persist_signal_state(
                            f"near_boundary_momentum_down:{station}:{epoch_id}",
                            {"boundary_key": str(boundary_key), "last_emit": obs_seconds, "cooldown_seconds": _SIGNAL_BOUNDARY_COOLDOWN_SECONDS},
                        )
                        _persist_signal_state(
                            f"station_cooldown:{station}",
                            {"last_emit": obs_seconds, "cooldown_seconds": _SIGNAL_STATION_COOLDOWN_SECONDS},
                        )
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
                                    _persist_signal_state(
                                        f"goldilocks_momentum_down:{station}:{epoch_id}",
                                        {"last_emit": obs_seconds, "cooldown_seconds": _SIGNAL_STATION_COOLDOWN_SECONDS},
                                    )
                                    runtime.update({"signal_type": "goldilocks_momentum_down", "signal_emitted": True, "suppression_reason": None})
                                    runtime["cooldown_state"]["station_active"] = True

                if runtime["signal_type"] is None and runtime["suppression_reason"] is None:
                    runtime["suppression_reason"] = "NO_SIGNAL_CONDITION_MATCH"
                pending_runtime_record = runtime

    if pending_runtime_record is not None:
        _record_signal_runtime(station, pending_runtime_record)

    if early_exit:
        return

    if pending_signal_context is not None:
        _emit_signal_alert(station=station, obs_time=obs_time, temp_f=now_f, signal_context=pending_signal_context, cfg=cfg)


def _process_temperature_event('''

# Split the content at the insertion point
parts = content.split(insertion_point, 1)
if len(parts) == 2:
    # Insert the code before the insertion point
    new_content = parts[0] + '                # LOW momentum detection (downward temperature trend)\n                momentum_down = None\n                distance_from_integer = float(now_f) - float(int(math.floor(now_f)))\n                monotonic_down = False\n                increasing_time = False\n                movement_down = False\n                if len(window) == _SIGNAL_MOMENTUM_WINDOW_SIZE:\n                    x1, x2, x3 = window[0], window[1], window[2]\n                    monotonic_down = x1["temp_f"] >= x2["temp_f"] >= x3["temp_f"]\n                    # Timestamps increase from oldest (x1) to newest (x3)\n                    increasing_time = x1["seconds"] < x2["seconds"] < x3["seconds"]\n                    movement_down = (x1["temp_f"] - x3["temp_f"]) >= 0.05\n                    total_seconds = x3["seconds"] - x1["seconds"]\n                    if increasing_time and total_seconds > 0:\n                        momentum_down = abs((x1["temp_f"] - x3["temp_f"]) / total_seconds)\n\n                # LOW momentum signals for downward transitions\n                if transition_type in ("instant_down", "reversion_after_settlement"):\n                    # Check near_boundary_momentum_down\n                    near_boundary_down_all = False\n                    if 0.0 < distance_from_integer <= 0.10:\n                        near_boundary_down_all = bool(\n                            monotonic_down\n                            and increasing_time\n                            and movement_down\n                            and momentum_down is not None\n                            and momentum_down >= 0.002\n                        )\n                    if near_boundary_down_all and not station_cooldown_active and not boundary_cooldown_active:\n                        boundary_key = (station, int(math.floor(now_f)), int(epoch_id))\n                        pending_signal_context = {\n                            "signal_type": "near_boundary_momentum_down",\n                            "signal_version": 1,\n                            "station": station,\n                            "obs_time": obs_time,\n                            "dedupe_key": f"near_boundary_momentum_down:{station}:{epoch_id}:{int(math.floor(now_f))}",\n                            "cooldown_applied": True,\n                            "cooldown_seconds": _SIGNAL_BOUNDARY_COOLDOWN_SECONDS,\n                            "distance_from_integer": distance_from_integer,\n                            "momentum_f_per_sec": momentum_down,\n                            "momentum_window_size": _SIGNAL_MOMENTUM_WINDOW_SIZE,\n                            "lower_integer_boundary": int(math.floor(now_f)),\n                            "pressure_from_boundary_seconds": distance_from_integer / momentum_down if momentum_down else None,\n                        }\n                        _SIGNAL_STATION_LAST_EMIT[station] = obs_seconds\n                        _SIGNAL_BOUNDARY_LAST_EMIT[boundary_key] = obs_seconds\n                        _persist_signal_state(\n                            f"near_boundary_momentum_down:{station}:{epoch_id}",\n                            {"boundary_key": str(boundary_key), "last_emit": obs_seconds, "cooldown_seconds": _SIGNAL_BOUNDARY_COOLDOWN_SECONDS},\n                        )\n                        _persist_signal_state(\n                            f"station_cooldown:{station}",\n                            {"last_emit": obs_seconds, "cooldown_seconds": _SIGNAL_STATION_COOLDOWN_SECONDS},\n                        )\n                        runtime.update({"signal_type": "near_boundary_momentum_down", "signal_emitted": True, "suppression_reason": None})\n                        runtime["cooldown_state"]["station_active"] = True\n                        runtime["cooldown_state"]["boundary_active"] = True\n\n                    # Check goldilocks_momentum_down\n                    if pending_signal_context is None:\n                        epoch_key = (station, int(epoch_id))\n                        tracker = _SIGNAL_GOLDILOCKS_EPOCH_TRACKER.get(epoch_key)\n                        if isinstance(tracker, dict):\n                            current_goldilocks = tracker.get("momentum_down_observed", False)\n                            if not current_goldilocks and tracker.get("exceeded_by_one_or_more"):\n                                # Check if temperature has dropped below the settlement bucket threshold\n                                if float(now_f) <= float(tracker.get("settlement_bucket_at_up") or settlement_bucket) - 0.2:\n                                    pending_signal_context = {\n                                        "signal_type": "goldilocks_momentum_down",\n                                        "signal_version": 1,\n                                        "station": station,\n                                        "obs_time": obs_time,\n                                        "dedupe_key": f"goldilocks_momentum_down:{station}:{epoch_id}",\n                                        "cooldown_applied": True,\n                                        "cooldown_seconds": _SIGNAL_STATION_COOLDOWN_SECONDS,\n                                        "settlement_bucket_at_up": int(tracker.get("settlement_bucket_at_up") or settlement_bucket),\n                                        "max_temp_after_up": float(tracker.get("max_temp_after_up") or now_f),\n                                        "reverted_temp": float(now_f),\n                                        "momentum_down": momentum_down,\n                                        "epoch_id": int(epoch_id),\n                                    }\n                                    tracker["momentum_down_observed"] = True\n                                    _SIGNAL_STATION_LAST_EMIT[station] = obs_seconds\n                                    _persist_signal_state(\n                                        f"goldilocks_momentum_down:{station}:{epoch_id}",\n                                        {"last_emit": obs_seconds, "cooldown_seconds": _SIGNAL_STATION_COOLDOWN_SECONDS},\n                                    )\n                                    runtime.update({"signal_type": "goldilocks_momentum_down", "signal_emitted": True, "suppression_reason": None})\n                                    runtime["cooldown_state"]["station_active"] = True\n\n                if runtime["signal_type"] is None and runtime["suppression_reason"] is None:\n                    runtime["suppression_reason"] = "NO_SIGNAL_CONDITION_MATCH"\n                pending_runtime_record = runtime\n\n    if pending_runtime_record is not None:\n        _record_signal_runtime(station, pending_runtime_record)\n\n    if early_exit:\n        return\n\n    if pending_signal_context is not None:\n        _emit_signal_alert(station=station, obs_time=obs_time, temp_f=now_f, signal_context=pending_signal_context, cfg=cfg)\n\n\ndef _process_temperature_event(' + parts[1]
    
    # Write the modified content
    with open('/home/node/.openclaw/workspace/prototypes/weather-engine-source/core/metar_monitor.py', 'w') as f:
        f.write(new_content)
    print("File updated successfully")
else:
    print("Insertion point not found")
