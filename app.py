import os
import logging
from flask import Flask, request, jsonify

# Ensure local 'core' package is importable when running on Render
import sys
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from core.metar_monitor import (
    get_latest_metar,
    set_watchlist,
    get_watchlist,
    get_metrics,
    start_scheduler,
    stop_scheduler,
    fetch_now,        # used by /metar/multi
    get_state,        # for debugging if you need it
)

app = Flask(__name__)
log = app.logger
log.setLevel(logging.INFO)


@app.route("/", methods=["GET"])
def root():
    return jsonify({"status": "ok"}), 200


# -------- METAR endpoints --------

@app.route("/metar/latest", methods=["GET"])
def metar_latest():
    icao = request.args.get("icao", "").strip().upper()
    if not icao:
        return jsonify({"error": "Missing query param: icao"}), 400
    return jsonify(get_latest_metar(icao)), 200


@app.route("/metar/multi", methods=["GET"])
def metar_multi():
    """
    Example: /metar/multi?icaos=KDEN,KLAX,KMDW
    Fetches on-demand (does not require scheduler).
    """
    raw = request.args.get("icaos", "")
    if not raw:
        return jsonify({"error": "Missing query param: icaos"}), 400
    icaos = [x.strip().upper() for x in raw.split(",") if x.strip()]
    if not icaos:
        return jsonify({"error": "No valid ICAOs supplied"}), 400
    res = fetch_now(icaos)
    return jsonify(res), 200


@app.route("/metar/watchlist", methods=["GET", "POST"])
def metar_watchlist():
    if request.method == "GET":
        return jsonify({"watchlist": get_watchlist()}), 200

    # POST
    try:
        data = request.get_json(force=True, silent=True) or {}
        icaos = data.get("icaos")
        result = set_watchlist(icaos)
        if "error" in result:
            return jsonify(result), 400
        return jsonify({"ok": True, "watchlist": result}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/metar/metrics", methods=["GET"])
def metar_metrics():
    return jsonify(get_metrics()), 200


@app.route("/metar/start", methods=["POST"])
def metar_start():
    start_scheduler(log)
    return jsonify({"ok": True, "scheduler": "started"}), 200


@app.route("/metar/stop", methods=["POST"])
def metar_stop():
    stop_scheduler()
    return jsonify({"ok": True, "scheduler": "stopped"}), 200


# Render entry
if __name__ == "__main__":
    # For local dev; Render uses gunicorn
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
