import os
import sys
import logging
from datetime import datetime
from flask import Flask, request, jsonify

# Make sure local 'core' package is importable on Render
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from core.metar_monitor import _send_alert, get_state  # add to your imports near the top

from core.metar_monitor import (
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
)

# --- create app BEFORE any decorators ---
app = Flask(__name__)
log = app.logger
log.setLevel(logging.INFO)


@app.route("/", methods=["GET"])
def root():
    return jsonify({"status": "ok"}), 200

@app.route("/metar/test-alert", methods=["POST"])
def metar_test_alert():
    """
    Immediately sends a synthetic alert to the configured ALERT_WEBHOOK_URL.
    Useful to verify Discord wiring without waiting for a real temp change.
    """
    cfg = get_default_config()
    payload = {
        "type": "temp_change",
        "station": "KDEN",
        "prev_temp_f": 50.0,
        "temp_f": 51.0,
        "delta_f": 1.0,
        "obs_time": datetime.utcnow().isoformat(),
        "at_utc": datetime.utcnow().isoformat(),
        "source": "synthetic",
    }
    _send_alert(cfg.get("webhook", ""), payload)
    return jsonify({"ok": True, "sent": True}), 200


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
    
@app.route("/metar/test-alert", methods=["POST"])
def metar_test_alert():
    """Sends a synthetic alert to your configured webhook immediately."""
    from core.metar_monitor import get_default_config
    cfg = get_default_config()
    payload = {
        "type": "temp_change",
        "station": "KDEN",
        "prev_temp_f": 50.0,
        "temp_f": 51.0,
        "delta_f": 1.0,
        "obs_time": datetime.utcnow().isoformat(),
        "at_utc": datetime.utcnow().isoformat(),
        "source": "synthetic",
    }
    _send_alert(cfg["webhook"], payload)
    return jsonify({"ok": True, "sent": True}), 200

@app.route("/metar/force-poll", methods=["POST"])
def metar_force_poll():
    """Runs one poll loop immediately (uses default source) and returns state deltas."""
    from core.metar_monitor import _poll_once
    before = get_state()
    _poll_once(app.logger)
    after = get_state()
    return jsonify({
        "ok": True,
        "before_poll_count": before.get("poll_count"),
        "after_poll_count": after.get("poll_count"),
        "last_poll_utc": after.get("last_poll_utc"),
    }), 200

# -------- METAR endpoints --------

@app.route("/metar/latest", methods=["GET"])
def metar_latest():
    """
    Get latest observation for a single ICAO.
    Optional: ?source=nws|tgftp|iem (defaults to METAR_DEFAULT_SOURCE or 'nws')
    """
    icao = request.args.get("icao", "").strip().upper()
    source = request.args.get("source")
    if not icao:
        return jsonify({"error": "Missing query param: icao"}), 400
    return jsonify(get_latest_metar(icao, source=source)), 200


@app.route("/metar/multi", methods=["GET"])
def metar_multi():
    """
    Fetch latest observations for multiple ICAOs.
    Example: /metar/multi?icaos=KDEN,KLAX,KMDW&source=iem
    """
    raw = request.args.get("icaos", "")
    source = request.args.get("source")
    if not raw:
        return jsonify({"error": "Missing query param: icaos"}), 400
    icaos = [x.strip().upper() for x in raw.split(",") if x.strip()]
    if not icaos:
        return jsonify({"error": "No valid ICAOs supplied"}), 400
    res = fetch_now(icaos, source=source)
    return jsonify(res), 200


@app.route("/metar/watchlist", methods=["GET", "POST"])
def metar_watchlist():
    """
    GET  -> current watchlist
    POST -> set watchlist with JSON body: {"icaos": ["KDEN","KLAX",...]}
    """
    if request.method == "GET":
        return jsonify({"watchlist": get_watchlist()}), 200

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
    """
    Returns scheduler + cache stats:
      - last_poll_utc
      - poll_count
      - watchlist_size
    """
    return jsonify(get_metrics()), 200


@app.route("/metar/start", methods=["POST"])
def metar_start():
    """
    Start the background poller (uses METAR_DEFAULT_SOURCE for watchlist).
    """
    start_scheduler(log)
    return jsonify({"ok": True, "scheduler": "started"}), 200


@app.route("/metar/stop", methods=["POST"])
def metar_stop():
    """
    Stop the background poller.
    """
    stop_scheduler()
    return jsonify({"ok": True, "scheduler": "stopped"}), 200


# Optional debug route
@app.route("/metar/state", methods=["GET"])
def metar_state():
    return jsonify(get_state()), 200


# Render entry
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
