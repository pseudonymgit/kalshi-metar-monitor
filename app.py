# ...top unchanged...

@app.route("/metar/latest", methods=["GET"])
def metar_latest():
    icao = request.args.get("icao", "").strip().upper()
    source = request.args.get("source")  # optional: nws | tgftp | iem
    if not icao:
        return jsonify({"error": "Missing query param: icao"}), 400
    # pass source through to metar_monitor
    return jsonify(get_latest_metar(icao, source=source)), 200

@app.route("/metar/multi", methods=["GET"])
def metar_multi():
    """
    Example: /metar/multi?icaos=KDEN,KLAX,KMDW&source=iem
    Fetches on-demand (does not require scheduler).
    """
    raw = request.args.get("icaos", "")
    source = request.args.get("source")  # optional
    if not raw:
        return jsonify({"error": "Missing query param: icaos"}), 400
    icaos = [x.strip().upper() for x in raw.split(",") if x.strip()]
    if not icaos:
        return jsonify({"error": "No valid ICAOs supplied"}), 400
    res = fetch_now(icaos, source=source)
    return jsonify(res), 200
