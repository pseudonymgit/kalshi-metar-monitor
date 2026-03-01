import os
import sys
import logging
from datetime import datetime, timezone
from flask import Flask, request, jsonify
# Make local 'core' importable on Render
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from core.metar_monitor import (
    ensure_scheduler_started,
    get_latest_metar,
    set_watchlist,
    get_watchlist,
    get_metrics,
    start_scheduler,
    stop_scheduler,
    fetch_now,
    get_state,
    _send_alert,
    get_default_config,
    _poll_once,
    _simulate_temperature_for_testing,
    get_recent_alerts,
    get_transition_history,
    run_replay_for_station_day,
    is_scheduler_running,
)

from core.kalshi_monitor import _kalshi_public_get, ensure_series_discovery_loaded
from core.observability import get_current_settlement_epoch_summaries

app = Flask(__name__)
log = app.logger
log.setLevel(logging.INFO)

_autostart_fallback_done = False

if hasattr(app, "before_first_request"):
    @app.before_first_request
    def _autostart_scheduler_once():
        if os.getenv("METAR_AUTOSTART", "true").lower() == "true":
            ensure_scheduler_started(log)
        ensure_series_discovery_loaded()
else:
    @app.before_request
    def _autostart_scheduler_fallback_once():
        global _autostart_fallback_done
        if _autostart_fallback_done:
            return None
        _autostart_fallback_done = True
        if os.getenv("METAR_AUTOSTART", "true").lower() == "true":
            ensure_scheduler_started(log)
        ensure_series_discovery_loaded()
        return None

@app.route("/", methods=["GET"])
def root():
    return jsonify({"status": "ok"}), 200


@app.route("/kalshi/ping", methods=["GET"])
def kalshi_ping():
    try:
        _kalshi_public_get("/markets?limit=1")
        return jsonify({"ok": True}), 200
    except Exception:
        return jsonify({"ok": False}), 200


@app.route("/kalshi/markets", methods=["GET"])
def kalshi_markets():
    try:
        limit = int(request.args.get("limit", "5"))
        from core.kalshi_monitor import get_public_markets

        data = get_public_markets(limit=limit)
        return jsonify({"ok": True, **data}), 200
    except Exception:
        return jsonify({"ok": False}), 200


@app.route("/kalshi/check", methods=["POST"])
def kalshi_check():
    try:
        limit = int(request.args.get("limit", "5"))
        from core.kalshi_monitor import check_public_market_changes

        summary = check_public_market_changes(limit=limit)
        return jsonify(summary), 200
    except Exception:
        return jsonify({"ok": False}), 200


@app.route("/kalshi/snapshot", methods=["GET"])
def kalshi_snapshot():
    station = (request.args.get("station") or "").strip().upper()
    if not station:
        return jsonify({"error": "Missing query param: station"}), 400

    raw_types = request.args.get("type", "")
    market_types = {
        token
        for token in (part.strip().upper() for part in raw_types.split(","))
        if token in {"HIGH", "LOW"}
    }

    from core.kalshi_monitor import build_structured_snapshot

    return jsonify(build_structured_snapshot(station=station, market_types=market_types)), 200


@app.route("/kalshi/composed", methods=["POST"])
def kalshi_composed():
    station = (request.args.get("station") or "").strip().upper()
    if not station:
        return jsonify({"error": "Missing query param: station"}), 400

    raw_types = request.args.get("type", "")
    market_types = {
        token
        for token in (part.strip().upper() for part in raw_types.split(","))
        if token in {"HIGH", "LOW"}
    }

    from core.kalshi_monitor import send_composed_weather_market_alert

    return jsonify(send_composed_weather_market_alert(station=station, market_types=market_types)), 200


@app.route("/kalshi/health", methods=["GET"])
def kalshi_health():
    from core.kalshi_monitor import (
        _get_active_stations,
        _last_composed_sent,
        _last_market_check_summary,
    )

    active = _get_active_stations()

    return jsonify({
        "active_stations": sorted(active) if active else [],
        "last_composed_sent": _last_composed_sent,
        "last_market_check_summary": _last_market_check_summary,
    }), 200

        
# --- Debug helpers ---

@app.route("/debug/version", methods=["GET"])
def debug_version():
    import core.metar_monitor as mm
    return jsonify({
        "metar_monitor_attrs": sorted(dir(mm)),
        "default_cfg": mm.get_default_config(),
        "has_fetch_window": hasattr(mm, "fetch_window"),
        "has__fetch_range_strict": hasattr(mm, "_fetch_range_strict"),
    }), 200


@app.route("/debug/alerts", methods=["GET"])
def debug_alerts():
    raw_limit = request.args.get("limit", "100")
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        limit = 100

    alerts = get_recent_alerts(limit)
    return jsonify({
        "ok": True,
        "count": len(alerts),
        "limit": limit,
        "alerts": alerts,
    }), 200


@app.route("/debug/ladder-state", methods=["GET"])
def debug_ladder_state():
    from core.kalshi_monitor import get_ladder_state_snapshot

    snapshot = get_ladder_state_snapshot()
    return jsonify({
        "ok": True,
        **snapshot
    }), 200

@app.errorhandler(500)
def err_500(e):
    import traceback
    return jsonify({"error": "internal", "trace": traceback.format_exc()}), 500

# -------- METAR endpoints --------

@app.route("/metar/window", methods=["GET"])
def metar_window():
    """
    Example:
      /metar/window?icao=KDEN&minutes=3&source=nws
    Strict source (no fallback). Ingests any obs in the window,
    then returns latest-known obs + counts.
    """
    icao = (request.args.get("icao") or "").strip().upper()
    minutes = int(request.args.get("minutes", "3"))
    source = (request.args.get("source") or "").strip().lower() or None

    if not icao:
        return jsonify({"error": "Missing query param: icao"}), 400
    if minutes <= 0:
        return jsonify({"error": "minutes must be > 0"}), 400

    from core.metar_monitor import fetch_window
    res = fetch_window(icao, minutes, source=source)
    return jsonify(res), 200

@app.route("/metar/latest", methods=["GET"])
def metar_latest():
    icao = (request.args.get("icao") or "").strip().upper()
    source = (request.args.get("source") or "").strip().lower() or None
    if not icao:
        return jsonify({"error": "Missing query param: icao"}), 400
    return jsonify(get_latest_metar(icao, source=source)), 200

@app.route("/metar/multi", methods=["GET"])
def metar_multi():
    raw = request.args.get("icaos", "")
    source = (request.args.get("source") or "").strip().lower() or None
    if not raw:
        return jsonify({"error": "Missing query param: icaos"}), 400
    icaos = [x.strip().upper() for x in raw.split(",") if x.strip()]
    if not icaos:
        return jsonify({"error": "No valid ICAOs supplied"}), 400
    res = fetch_now(icaos, source=source)   # <-- forward the source
    return jsonify(res), 200


@app.route("/metar/watchlist", methods=["GET", "POST"])
def metar_watchlist():
    if request.method == "GET":
        return jsonify({"watchlist": get_watchlist()}), 200
    data = request.get_json(force=True, silent=True) or {}
    icaos = data.get("icaos")
    result = set_watchlist(icaos)
    if "error" in result:
        return jsonify(result), 400
    return jsonify({"ok": True, "watchlist": result}), 200

@app.route("/metar/metrics", methods=["GET"])
def metar_metrics():
    return jsonify(get_metrics()), 200


@app.route("/metrics/retention", methods=["GET"])
def retention_metrics():
    from core.metar_monitor import get_retention_metrics

    return jsonify(get_retention_metrics()), 200


@app.route("/metrics/prune", methods=["POST"])
def retention_prune():
    from core.metar_monitor import prune_old_alerts

    return jsonify(prune_old_alerts()), 200


@app.route("/observability/transitions", methods=["GET"])
def observability_transitions():
    station = (request.args.get("station") or "").strip().upper() or None
    raw_limit = request.args.get("limit", "50")
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        limit = 50

    transitions = get_transition_history(station=station, limit=limit)
    return jsonify({
        "ok": True,
        "count": len(transitions),
        "transitions": transitions,
    }), 200


@app.route("/observability/ingestion-health", methods=["GET"])
def observability_ingestion_health():
    """
    Deterministic per-station ingestion freshness visibility.

    Classification rules:
      - healthy: station has accepted observation and age <= stale_after_seconds
      - stale: station has accepted observation and age > stale_after_seconds
      - stale: station has no accepted observation
    """
    state = get_state()
    cfg = get_default_config()
    now_utc = datetime.now(timezone.utc)
    last_poll_utc = state.get("last_poll_utc")

    stale_after_seconds = max(int(cfg.get("poll_seconds", 60)) * 3, 60)

    poll_lag_seconds = None
    if last_poll_utc:
        try:
            poll_lag_seconds = max(
                0,
                int((now_utc - datetime.fromisoformat(last_poll_utc.replace("Z", "+00:00"))).total_seconds()),
            )
        except Exception:
            poll_lag_seconds = None

    stations = state.get("stations") or cfg.get("stations") or []
    per_station = []

    for station in stations:
        latest_observation_utc = state.get("last_seen_iso", {}).get(station)
        freshness_lag_seconds = None
        status = "stale"
        reason = "no_accepted_observation"

        if latest_observation_utc:
            try:
                freshness_lag_seconds = max(
                    0,
                    int(
                        (now_utc - datetime.fromisoformat(latest_observation_utc.replace("Z", "+00:00"))).total_seconds()
                    ),
                )
                if freshness_lag_seconds <= stale_after_seconds:
                    status = "healthy"
                    reason = "freshness_lag_within_threshold"
                else:
                    status = "stale"
                    reason = "freshness_lag_exceeds_threshold"
            except Exception:
                status = "stale"
                reason = "invalid_observation_timestamp"

        per_station.append(
            {
                "station": station,
                "latest_accepted_observation_utc": latest_observation_utc,
                "latest_poll_utc": last_poll_utc,
                "freshness_lag_seconds": freshness_lag_seconds,
                "poll_lag_seconds": poll_lag_seconds,
                "status": status,
                "status_reason": reason,
            }
        )

    return jsonify(
        {
            "ok": True,
            "scheduler_running": is_scheduler_running(),
            "last_poll_utc": last_poll_utc,
            "poll_lag_seconds": poll_lag_seconds,
            "stale_after_seconds": stale_after_seconds,
            "stations": per_station,
        }
    ), 200


@app.route("/observability/current-epochs", methods=["GET"])
def observability_current_epochs():
    station = (request.args.get("station") or "").strip().upper() or None
    payload = get_current_settlement_epoch_summaries(station=station)
    response_fields = [
        "station",
        "market_type",
        "local_trading_date",
        "settlement_bucket",
        "prior_settlement_bucket",
        "settlement_timestamp_utc",
        "settlement_jump_magnitude",
        "epoch_status",
        "epoch_close_reason",
        "epoch_close_timestamp_utc",
        "reversion_occurred",
        "first_reversion_timestamp_utc",
        "max_excursion_above_settlement",
        "duration_at_or_above_settlement_seconds",
        "duration_strictly_above_settlement_seconds",
        "terminal_state_reached",
        "settlement_transition_event_id",
        "last_transition_event_id",
        "last_transition_timestamp_utc",
        "last_transition_temp_f",
        "selection_source",
    ]
    compact_epochs = [
        {field: row.get(field) for field in response_fields}
        for row in payload.get("epochs", [])
    ]
    return jsonify({"ok": True, **payload, "epochs": compact_epochs}), 200


@app.route("/metar/status", methods=["GET"])
def metar_status():
    """
    Scheduler lifecycle status.

    Includes poll counters plus `last_loop_utc`, which represents the
    scheduler thread's most recent completed loop timestamp.
    """
    from core.metar_monitor import _SCHEDULER_THREAD

    running = (
        _SCHEDULER_THREAD is not None and
        _SCHEDULER_THREAD.is_alive()
    )
    state = get_metrics()
    return jsonify({
        "scheduler_running": running,
        "poll_count": state.get("poll_count"),
        "last_poll_utc": state.get("last_poll_utc"),
        "last_loop_utc": state.get("last_loop_utc"),
        "timeout_count": state.get("timeout_count"),
        "last_timeout_station": state.get("last_timeout_station"),
        "last_timeout_utc": state.get("last_timeout_utc"),
    }), 200


@app.route("/metar/simulate-ladder", methods=["POST"])
def metar_simulate_ladder():
    """
    Simulate a station temperature update for ladder transition testing.

    JSON body:
      - icao (required)
      - temp_f (required, numeric)
      - deliver (optional; true/1 enables webhook delivery attempt)

    Notes:
      - Bypasses live alert window gating.
      - Returns crossing and delivery-attempt metadata for test sequencing.
    """
    data = request.get_json(force=True, silent=True) or {}
    icao = (data.get("icao") or "").strip().upper()
    temp_f = data.get("temp_f")
    deliver_raw = data.get("deliver", False)
    deliver = (
        deliver_raw is True
        or deliver_raw == 1
        or (isinstance(deliver_raw, str) and deliver_raw.strip().lower() in {"true", "1"})
    )

    if not icao:
        return jsonify({"error": "Missing JSON field: icao"}), 400
    if temp_f is None:
        return jsonify({"error": "Missing JSON field: temp_f"}), 400

    try:
        temp_f = float(temp_f)
    except Exception:
        return jsonify({"error": "temp_f must be numeric"}), 400

    return jsonify(
        _simulate_temperature_for_testing(
            icao,
            temp_f,
            logger=app.logger,
            allow_alert_delivery=deliver,
        )
    ), 200

@app.route("/metar/start", methods=["POST"])
def metar_start():
    start_scheduler(log)
    return jsonify({"ok": True, "scheduler": "started"}), 200

@app.route("/metar/stop", methods=["POST"])
def metar_stop():
    stop_scheduler()
    return jsonify({"ok": True, "scheduler": "stopped"}), 200

# -------- Test + Ops helpers (single definitions) --------

@app.route("/metar/test-alert", methods=["POST"])
def metar_test_alert():
    data = request.get_json(force=True, silent=True) or {}
    station = (data.get("station") or "").strip().upper()
    temp_f = data.get("temp_f")

    if not station:
        return jsonify({"error": "Missing JSON field: station"}), 400

    try:
        temp_f = float(temp_f)
    except (TypeError, ValueError):
        return jsonify({"error": "temp_f must be float"}), 400

    return jsonify(
        _simulate_temperature_for_testing(
            icao=station,
            temp_f=float(temp_f),
            allow_alert_delivery=True,
        )
    ), 200

@app.route("/metar/force-poll", methods=["POST"])
def metar_force_poll():
    """
    Runs one poll loop immediately (uses current default source) and returns counters.
    """
    before = get_state()
    _poll_once(app.logger)
    after = get_state()
    return jsonify({
        "ok": True,
        "before_poll_count": before.get("poll_count"),
        "after_poll_count": after.get("poll_count"),
        "last_poll_utc": after.get("last_poll_utc"),
    }), 200

@app.route("/debug/replay", methods=["POST"])
def debug_replay():
    data = request.get_json(force=True, silent=True) or {}
    station = (data.get("station") or "").strip().upper()
    date_local = (data.get("date_local") or "").strip()

    if not station:
        return jsonify({"error": "Missing JSON field: station"}), 400
    if not date_local:
        return jsonify({"error": "Missing JSON field: date_local"}), 400

    try:
        result = run_replay_for_station_day(station=station, date_local=date_local)
        return jsonify(result), 200
    except ValueError:
        return jsonify({"error": "date_local must be YYYY-MM-DD"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/debug/state", methods=["GET"])
def debug_state():
    from core.metar_monitor import get_state
    return jsonify(get_state()), 200

# Render entry
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
