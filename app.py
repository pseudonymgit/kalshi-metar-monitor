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
    get_latest_station_market_evaluation_context,
    run_replay_for_station_day,
    is_scheduler_running,
)

from core.kalshi_monitor import _kalshi_public_get, ensure_series_discovery_loaded
from core.observability import (
    get_current_day_structure_summaries,
    get_current_settlement_epoch_summaries,
)
from core.station_time import to_station_local

app = Flask(__name__)
log = app.logger
log.setLevel(logging.INFO)

_autostart_fallback_done = False


def _safe_lag_seconds(*, now_utc: datetime, iso_timestamp: str):
    if not iso_timestamp:
        return None
    try:
        return max(0, int((now_utc - datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))).total_seconds()))
    except Exception:
        return None


def _build_ingestion_health_rows(*, station_filter=None):
    state = get_state()
    cfg = get_default_config()
    now_utc = datetime.now(timezone.utc)
    last_poll_utc = state.get("last_poll_utc")

    stale_after_seconds = max(int(cfg.get("poll_seconds", 60)) * 3, 60)
    poll_lag_seconds = _safe_lag_seconds(now_utc=now_utc, iso_timestamp=last_poll_utc)

    configured_stations = state.get("stations") or cfg.get("stations") or []
    if station_filter:
        stations = [station for station in configured_stations if station == station_filter]
    else:
        stations = list(configured_stations)

    per_station = []
    for station in stations:
        latest_observation_utc = state.get("last_seen_iso", {}).get(station)
        freshness_lag_seconds = None
        status = "stale"
        reason = "no_accepted_observation"

        if latest_observation_utc:
            freshness_lag_seconds = _safe_lag_seconds(now_utc=now_utc, iso_timestamp=latest_observation_utc)
            if freshness_lag_seconds is None:
                status = "stale"
                reason = "invalid_observation_timestamp"
            elif freshness_lag_seconds <= stale_after_seconds:
                status = "healthy"
                reason = "freshness_lag_within_threshold"
            else:
                status = "stale"
                reason = "freshness_lag_exceeds_threshold"

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

    return {
        "scheduler_running": is_scheduler_running(),
        "last_poll_utc": last_poll_utc,
        "poll_lag_seconds": poll_lag_seconds,
        "stale_after_seconds": stale_after_seconds,
        "stations": per_station,
    }


def _select_station_epoch_row(epoch_rows):
    if not epoch_rows:
        return None

    open_rows = [row for row in epoch_rows if row.get("epoch_status") == "open"]
    candidate_rows = open_rows or epoch_rows

    return max(
        candidate_rows,
        key=lambda row: (
            row.get("settlement_timestamp_utc") or "",
            row.get("last_transition_timestamp_utc") or "",
            int(row.get("epoch_id") or 0),
            row.get("market_type") or "",
        ),
    )


def _market_has_supported_strike(market):
    strike_type = market.get("strike_type")
    floor = market.get("floor_strike")
    cap = market.get("cap_strike")

    if strike_type == "between" and floor is not None:
        return True
    if strike_type == "less" and cap is not None:
        return True
    if strike_type == "greater" and floor is not None:
        return True

    from core.kalshi_monitor import _extract_strike_from_ticker

    return _extract_strike_from_ticker((market.get("ticker") or "").upper()) is not None


def _build_market_coverage_rows(station_filter=None):
    from core.kalshi_monitor import (
        _STATION_CITY_TOKEN_MAP,
        _filter_structured_markets,
        _get_active_stations,
        _parse_target_market_types,
        _station_local_kalshi_date_token,
    )

    cfg = get_default_config()
    state = get_state()

    configured_stations = set((cfg.get("stations") or []))
    configured_stations.update(state.get("stations") or [])
    stations = sorted(station.strip().upper() for station in configured_stations if station)

    if station_filter:
        stations = [station for station in stations if station == station_filter]

    active_stations = _get_active_stations()
    enabled_market_types = _parse_target_market_types(os.getenv("KALSHI_TARGET_MARKET_TYPE"))
    if not enabled_market_types:
        enabled_market_types = {"HIGH"}

    webhook_configured = bool((os.getenv("ALERT_WEBHOOK_URL") or "").strip())
    series_by_station = ensure_series_discovery_loaded()

    rows = []
    for station in stations:
        station_active = active_stations is None or station in active_stations
        series_ticker = series_by_station.get(station)

        discovered_markets = []
        if series_ticker:
            data = _kalshi_public_get(f"/markets?series_ticker={series_ticker}")
            discovered_markets = data.get("markets") or []

        for market_type in ["HIGH", "LOW"]:
            market_type_enabled = market_type in enabled_market_types
            filtered_markets = _filter_structured_markets(discovered_markets, station, {market_type})
            eligible_markets = [market for market in filtered_markets if _market_has_supported_strike(market)]

            filtered_out_summary = {
                "inactive_status": 0,
                "station_mismatch": 0,
                "date_mismatch": 0,
                "market_type_mismatch": 0,
                "unsupported_strike": 0,
            }

            filtered_market_tickers = []
            filtered_tickers = {m.get("ticker") for m in filtered_markets}
            eligible_tickers = [
                (market.get("ticker") or "").upper()
                for market in eligible_markets
                if market.get("ticker")
            ]
            city_token = _STATION_CITY_TOKEN_MAP.get(station)
            date_token = _station_local_kalshi_date_token(station)
            for market in discovered_markets:
                ticker = (market.get("ticker") or "").upper()
                status = market.get("status")

                if status and status != "active":
                    filtered_out_summary["inactive_status"] += 1
                    filtered_market_tickers.append(ticker)
                    continue

                if ticker not in filtered_tickers:
                    if city_token and city_token not in ticker:
                        filtered_out_summary["station_mismatch"] += 1
                    elif date_token and date_token not in ticker:
                        filtered_out_summary["date_mismatch"] += 1
                    elif market_type not in ticker:
                        filtered_out_summary["market_type_mismatch"] += 1
                    else:
                        filtered_out_summary["date_mismatch"] += 1
                    filtered_market_tickers.append(ticker)
                    continue

                if ticker not in eligible_tickers:
                    filtered_out_summary["unsupported_strike"] += 1
                    filtered_market_tickers.append(ticker)

            evaluation_possible = bool(
                station_active
                and market_type_enabled
                and series_ticker
                and len(eligible_markets) > 0
            )
            alerting_possible = bool(evaluation_possible and webhook_configured)

            if not station_active:
                coverage_status = "not_covered"
                coverage_reason = "station_not_active"
            elif not market_type_enabled:
                coverage_status = "not_covered"
                coverage_reason = "market_type_disabled_by_config"
            elif not series_ticker:
                coverage_status = "not_covered"
                coverage_reason = "no_discovered_series"
            elif len(eligible_markets) == 0:
                coverage_status = "not_covered"
                coverage_reason = "no_eligible_markets_after_filters"
            elif not webhook_configured:
                coverage_status = "evaluation_only"
                coverage_reason = "webhook_missing"
            else:
                coverage_status = "alerting_possible_runtime_gated"
                coverage_reason = "eligible_but_runtime_transition_terminal_rate_limit_gates_apply"

            rows.append(
                {
                    "station": station,
                    "market_type": market_type,
                    "station_active_for_processing": station_active,
                    "market_type_enabled_by_config": market_type_enabled,
                    "discovered_series_ticker": series_ticker,
                    "series_discovered": bool(series_ticker),
                    "discovered_market_count": len(discovered_markets),
                    "eligible_market_count_after_filters": len(eligible_markets),
                    "evaluation_possible": evaluation_possible,
                    "alerting_possible": alerting_possible,
                    "coverage_status": coverage_status,
                    "coverage_reason": coverage_reason,
                    "eligible_market_tickers": eligible_tickers[:10],
                    "filtered_out_market_count": len(filtered_market_tickers),
                    "filtered_out_market_counts_by_reason": filtered_out_summary,
                }
            )

    return {
        "station": station_filter,
        "stations_evaluated": stations,
        "rows": rows,
    }


def _station_universe(station_filter=None):
    cfg = get_default_config()
    state = get_state()

    configured_stations = set((cfg.get("stations") or []))
    configured_stations.update(state.get("stations") or [])
    stations = sorted(station.strip().upper() for station in configured_stations if station)

    if station_filter:
        stations = [station for station in stations if station == station_filter]

    return stations


def _station_today_day_keys(stations):
    now_utc = datetime.now(timezone.utc)
    return {
        station: to_station_local(station, now_utc).date().isoformat()
        for station in stations
    }


def _build_trader_dashboard_rows(station_filter=None):
    ingestion_payload = _build_ingestion_health_rows(station_filter=station_filter)
    current_epochs_payload = get_current_settlement_epoch_summaries(station=station_filter)

    stations = [row.get("station") for row in ingestion_payload.get("stations", []) if row.get("station")]

    epoch_rows_by_station = {}
    for epoch_row in current_epochs_payload.get("epochs", []):
        station_code = epoch_row.get("station")
        if not station_code:
            continue
        epoch_rows_by_station.setdefault(station_code, []).append(epoch_row)

    station_day_keys = _station_today_day_keys(stations)
    day_structure_payload = get_current_day_structure_summaries(
        station_day_keys=station_day_keys,
        station=station_filter,
    )
    day_rows_by_station = {
        row.get("station"): row
        for row in day_structure_payload.get("rows", [])
        if row.get("station")
    }

    market_coverage_payload = _build_market_coverage_rows(station_filter=station_filter)
    market_coverage_by_station = {}
    latest_evaluation_context_by_station = get_latest_station_market_evaluation_context(station=station_filter)

    for coverage_row in market_coverage_payload.get("rows", []):
        station_code = coverage_row.get("station")
        market_type = coverage_row.get("market_type")
        if not station_code or market_type not in {"HIGH", "LOW"}:
            continue
        market_coverage_by_station.setdefault(station_code, {})[market_type] = coverage_row

    rows = []
    for ingestion_row in ingestion_payload.get("stations", []):
        station_code = ingestion_row.get("station")
        selected_epoch = _select_station_epoch_row(epoch_rows_by_station.get(station_code, [])) or {}
        day_row = day_rows_by_station.get(station_code, {})
        station_coverage = market_coverage_by_station.get(station_code, {})
        high_coverage = station_coverage.get("HIGH", {})
        low_coverage = station_coverage.get("LOW", {})
        latest_eval = latest_evaluation_context_by_station.get(station_code, {})

        rows.append(
            {
                "station": station_code,
                "ingestion_status": ingestion_row.get("status"),
                "ingestion_status_reason": ingestion_row.get("status_reason"),
                "latest_accepted_observation_utc": ingestion_row.get("latest_accepted_observation_utc"),
                "freshness_lag_seconds": ingestion_row.get("freshness_lag_seconds"),
                "latest_poll_utc": ingestion_row.get("latest_poll_utc"),
                "current_epoch_selection_source": selected_epoch.get("selection_source"),
                "epoch_status": selected_epoch.get("epoch_status"),
                "local_trading_date": selected_epoch.get("local_trading_date") or day_row.get("local_trading_date"),
                "settlement_bucket": selected_epoch.get("settlement_bucket"),
                "prior_settlement_bucket": selected_epoch.get("prior_settlement_bucket"),
                "settlement_timestamp_utc": selected_epoch.get("settlement_timestamp_utc"),
                "reversion_occurred": selected_epoch.get("reversion_occurred"),
                "first_reversion_timestamp_utc": selected_epoch.get("first_reversion_timestamp_utc"),
                "max_excursion_above_settlement": selected_epoch.get("max_excursion_above_settlement"),
                "duration_at_or_above_settlement_seconds": selected_epoch.get("duration_at_or_above_settlement_seconds"),
                "duration_strictly_above_settlement_seconds": selected_epoch.get("duration_strictly_above_settlement_seconds"),
                "terminal_state_reached": selected_epoch.get("terminal_state_reached"),
                "last_transition_timestamp_utc": selected_epoch.get("last_transition_timestamp_utc"),
                "last_transition_temp_f": selected_epoch.get("last_transition_temp_f"),
                "epoch_count_today": day_row.get("epoch_count_today"),
                "open_epoch_present": day_row.get("open_epoch_present"),
                "closed_epoch_count_today": day_row.get("closed_epoch_count_today"),
                "reverted_epoch_count_today": day_row.get("reverted_epoch_count_today"),
                "terminal_epoch_count_today": day_row.get("terminal_epoch_count_today"),
                "high_alerting_possible": high_coverage.get("alerting_possible"),
                "low_alerting_possible": low_coverage.get("alerting_possible"),
                "high_coverage_status": high_coverage.get("coverage_status"),
                "low_coverage_status": low_coverage.get("coverage_status"),
                "high_coverage_reason": high_coverage.get("coverage_reason"),
                "low_coverage_reason": low_coverage.get("coverage_reason"),
                "high_eligible_market_count": high_coverage.get("eligible_market_count_after_filters"),
                "low_eligible_market_count": low_coverage.get("eligible_market_count_after_filters"),
                "latest_evaluation_timestamp_utc": latest_eval.get("latest_evaluation_timestamp_utc"),
                "latest_market_evaluated": latest_eval.get("latest_market_evaluated"),
                "latest_alerts_sent": latest_eval.get("latest_alerts_sent"),
                "latest_evaluation_outcome": latest_eval.get("latest_evaluation_outcome"),
                "latest_suppression_reason": latest_eval.get("latest_suppression_reason"),
                "latest_transition_type": latest_eval.get("latest_transition_type"),
                "latest_transition_event_id": latest_eval.get("latest_transition_event_id"),
            }
        )

    return {
        "station": station_filter,
        "count": len(rows),
        "rows": rows,
    }

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
    payload = _build_ingestion_health_rows()
    return jsonify({"ok": True, **payload}), 200


@app.route("/observability/station-summary", methods=["GET"])
def observability_station_summary():
    station = (request.args.get("station") or "").strip().upper() or None

    ingestion_payload = _build_ingestion_health_rows(station_filter=station)
    current_epochs_payload = get_current_settlement_epoch_summaries(station=station)

    epoch_rows_by_station = {}
    for row in current_epochs_payload.get("epochs", []):
        row_station = row.get("station")
        if not row_station:
            continue
        epoch_rows_by_station.setdefault(row_station, []).append(row)

    summary_rows = []
    for ingestion_row in ingestion_payload.get("stations", []):
        station_code = ingestion_row.get("station")
        selected_epoch = _select_station_epoch_row(epoch_rows_by_station.get(station_code, [])) or {}
        summary_rows.append(
            {
                "station": station_code,
                "ingestion_status": ingestion_row.get("status"),
                "ingestion_status_reason": ingestion_row.get("status_reason"),
                "latest_accepted_observation_utc": ingestion_row.get("latest_accepted_observation_utc"),
                "freshness_lag_seconds": ingestion_row.get("freshness_lag_seconds"),
                "latest_poll_utc": ingestion_row.get("latest_poll_utc"),
                "current_epoch_selection_source": selected_epoch.get("selection_source"),
                "epoch_status": selected_epoch.get("epoch_status"),
                "local_trading_date": selected_epoch.get("local_trading_date"),
                "settlement_bucket": selected_epoch.get("settlement_bucket"),
                "prior_settlement_bucket": selected_epoch.get("prior_settlement_bucket"),
                "settlement_timestamp_utc": selected_epoch.get("settlement_timestamp_utc"),
                "reversion_occurred": selected_epoch.get("reversion_occurred"),
                "first_reversion_timestamp_utc": selected_epoch.get("first_reversion_timestamp_utc"),
                "max_excursion_above_settlement": selected_epoch.get("max_excursion_above_settlement"),
                "duration_at_or_above_settlement_seconds": selected_epoch.get("duration_at_or_above_settlement_seconds"),
                "duration_strictly_above_settlement_seconds": selected_epoch.get("duration_strictly_above_settlement_seconds"),
                "terminal_state_reached": selected_epoch.get("terminal_state_reached"),
                "last_transition_timestamp_utc": selected_epoch.get("last_transition_timestamp_utc"),
                "last_transition_temp_f": selected_epoch.get("last_transition_temp_f"),
            }
        )

    return jsonify(
        {
            "ok": True,
            "station": station,
            "count": len(summary_rows),
            "rows": summary_rows,
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


@app.route("/observability/day-structure", methods=["GET"])
def observability_day_structure():
    station = (request.args.get("station") or "").strip().upper() or None
    stations = _station_universe(station_filter=station)
    station_day_keys = _station_today_day_keys(stations)

    payload = get_current_day_structure_summaries(
        station_day_keys=station_day_keys,
        station=station,
    )

    response_fields = [
        "station",
        "local_trading_date",
        "epoch_count_today",
        "open_epoch_present",
        "current_or_latest_settlement_bucket",
        "current_or_latest_prior_settlement_bucket",
        "current_or_latest_settlement_timestamp_utc",
        "current_or_latest_settlement_jump_magnitude",
        "current_or_latest_epoch_status",
        "current_or_latest_reversion_occurred",
        "current_or_latest_first_reversion_timestamp_utc",
        "current_or_latest_max_excursion_above_settlement",
        "current_or_latest_duration_at_or_above_settlement_seconds",
        "current_or_latest_duration_strictly_above_settlement_seconds",
        "current_or_latest_terminal_state_reached",
        "latest_transition_timestamp_utc",
        "latest_transition_temp_f",
        "closed_epoch_count_today",
        "reverted_epoch_count_today",
        "terminal_epoch_count_today",
        "latest_selection_source",
    ]
    compact_rows = [
        {field: row.get(field) for field in response_fields}
        for row in payload.get("rows", [])
    ]

    return jsonify({"ok": True, **payload, "rows": compact_rows}), 200


@app.route("/observability/market-coverage", methods=["GET"])
def observability_market_coverage():
    station = (request.args.get("station") or "").strip().upper() or None
    payload = _build_market_coverage_rows(station_filter=station)
    return jsonify({"ok": True, "count": len(payload["rows"]), **payload}), 200


@app.route("/observability/trader-dashboard", methods=["GET"])
def observability_trader_dashboard():
    station = (request.args.get("station") or "").strip().upper() or None
    payload = _build_trader_dashboard_rows(station_filter=station)
    return jsonify({"ok": True, **payload}), 200


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
