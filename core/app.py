import os
import json
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

app = Flask(__name__)

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
