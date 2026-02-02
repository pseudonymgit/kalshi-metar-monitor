import os
import json
from api.core.metar_monitor import get_latest_metar, set_watchlist, get_watchlist, get_metrics
from flask import Flask, request, jsonify
from api.core.metar_monitor import (
    get_default_config,
    start_scheduler,
    stop_scheduler,
    is_scheduler_running,
    fetch_now,
    get_state,
    ensure_state_loaded,
)

PROXY_ACCESS_KEY = os.getenv("PROXY_ACCESS_KEY", "").strip()

app = Flask(__name__)

def _check_proxy_key():
    """Guard only if PROXY_ACCESS_KEY is set."""
    if PROXY_ACCESS_KEY:
        supplied = request.headers.get("X-Proxy-Key", "")
        if supplied != PROXY_ACCESS_KEY:
            return jsonify({"error": "Unauthorized"}), 401
    return None

@app.route("/health", methods=["GET"])
def health_public():
    return jsonify({"status": "ok"}), 200

@app.route("/metar/latest", methods=["GET"])
def metar_latest():
    guard = _check_proxy_key()
    if guard: return guard
    icao = request.args.get("icao", "").upper().strip()
    if not icao:
        return jsonify({"error": "missing ?icao=KDEN"}), 400
    data = get_latest_metar(icao)
    if not data:
        return jsonify({"error": f"no data for {icao}"}), 404
    return jsonify(data)

@app.route("/metar/watchlist", methods=["GET", "POST"])
def metar_watchlist():
    guard = _check_proxy_key()
    if guard: return guard
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        icaos = payload.get("icaos", [])
        if not isinstance(icaos, list) or not icaos:
            return jsonify({"error": "POST JSON must include non-empty 'icaos' list"}), 400
        set_watchlist([x.upper().strip() for x in icaos])
        return jsonify({"ok": True, "watchlist": get_watchlist()})
    else:
        return jsonify({"watchlist": get_watchlist()})

@app.route("/metrics", methods=["GET"])
def metrics():
    guard = _check_proxy_key()
    if guard: return guard
    return jsonify(get_metrics())
    
@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "metar_monitor"}), 200

@app.route("/state")
def state():
    ensure_state_loaded()
    return jsonify(get_state()), 200

@app.route("/metar/now")
def metar_now():
    stations_q = request.args.get("stations")
    if stations_q:
        stations = [s.strip().upper() for s in stations_q.split(",") if s.strip()]
    else:
        stations = json.loads(os.getenv("METAR_STATIONS_JSON", '["KDEN","KLAX","KNYC","KPHL","KMDW","KMIA","KAUS"]'))
    result = fetch_now(stations)
    return jsonify(result), 200

@app.route("/monitor/start", methods=["POST"])
def monitor_start():
    cfg = get_default_config()
    ok = start_scheduler(app.logger, cfg)
    return jsonify({"started": ok, "running": is_scheduler_running()}), 200

@app.route("/monitor/stop", methods=["POST"])
def monitor_stop():
    ok = stop_scheduler()
    return jsonify({"stopped": ok, "running": is_scheduler_running()}), 200

if __name__ == "__main__":
    autostart = os.getenv("METAR_AUTOSTART", "true").lower() in ("1","true","yes","y")
    if autostart:
        start_scheduler(app.logger, get_default_config())
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), debug=False)

