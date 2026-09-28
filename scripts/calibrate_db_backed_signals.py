#!/usr/bin/env python3
"""
DB-backed calibration lane for signals that expose `evaluate_for_station()`.

The daily-METAR calibration runner (core/calibrate_all_signals.py) drives signals
through `evaluate(idx, days)` over a daily METAR day-series. Four real signals
cannot fire on that path because they read their own source DB:

  - cloud_cover_index        -> weatherapi_archive.db (hourly.cloud)
  - feels_like_delta         -> weatherapi_archive.db (hourly.temp_c/feelslike_c)
  - cross_model_divergence   -> nwp_forecasts.db (gfs vs ecmwf)
  - frontal_passage_intraday -> metar_backfill.db (intraday metar_observations)

This lane evaluates each of them via `evaluate_for_station(station, date)` across
the canonical 20 stations × a date window, joins to settlement ground truth, and
reports raw accuracy + trade counts. Deterministic; no AI/ML; no model calls.

Usage:
    python3 scripts/calibrate_db_backed_signals.py
    python3 scripts/calibrate_db_backed_signals.py --start 2026-08-01 --end 2026-09-20
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

from core.signals import SignalRegistry  # noqa: E402

METAR_DB = str(REPO_ROOT / "data" / "metar_backfill.db")
KALSHI_DB = str(REPO_ROOT / "data" / "kalshi_settlements.db")
OUTPUT = str(REPO_ROOT / "data" / "calibration_db_backed.json")

STATIONS = [
    "KATL", "KAUS", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU", "KLAS", "KLAX",
    "KMDW", "KMIA", "KMSP", "KMSY", "KNYC", "KOKC", "KPHL", "KPHX", "KSAT",
    "KSEA", "KSFO",
]

SIGNALS = [
    "cloud_cover_index",
    "feels_like_delta",
    "cross_model_divergence",
    "frontal_passage_intraday",
]


def load_daily_high(conn: sqlite3.Connection):
    """date -> high (sanity-banded) for direction ground truth."""
    out = {}
    for dt, hi in conn.execute(
        "SELECT date_utc, max_temp_f FROM daily_stats "
        "WHERE max_temp_f BETWEEN -60 AND 140"
    ):
        out[dt[:10]] = hi
    return out


def load_kalshi_dir(conn: sqlite3.Connection):
    """(station,date) -> 'up'/'down' from consecutive Kalshi temps."""
    by_station = {}
    for st, dt, t in conn.execute(
        "SELECT station, target_date, kalshi_temp FROM kalshi_settlements "
        "WHERE kalshi_temp IS NOT NULL ORDER BY station, target_date"
    ):
        by_station.setdefault(st, []).append((dt[:10], t))
    out = {}
    for st, rows in by_station.items():
        prev = None
        for dt, t in rows:
            if prev is not None:
                if t > prev:
                    out[(st, dt)] = "up"
                elif t < prev:
                    out[(st, dt)] = "down"
            prev = t
    return out


def _is_weatherapi(sig) -> bool:
    """True when the signal reads weatherapi_archive.db via _load_hourly."""
    return hasattr(sig, "_load_hourly") and hasattr(sig, "_build_daily_series")


def main() -> int:
    ap = argparse.ArgumentParser(description="DB-backed signal calibration lane")
    ap.add_argument("--start", default="2026-08-01")
    ap.add_argument("--end", default="2026-09-20")
    ap.add_argument("--output", default=OUTPUT)
    args = ap.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    dates = []
    d = start
    while d <= end:
        dates.append(d.isoformat())
        d += timedelta(days=1)

    mconn = sqlite3.connect(METAR_DB)
    kconn = sqlite3.connect(KALSHI_DB)
    kdir = load_kalshi_dir(kconn)

    registry = SignalRegistry(METAR_DB)
    results = {}

    for name in SIGNALS:
        sig = registry.get_signal(name)
        if sig is None:
            results[name] = {"error": "not in registry"}
            continue
        trades = 0
        correct = 0
        per_station = {}

        # WeatherAPI signals load the ENTIRE hourly table per call, so build the
        # daily series ONCE per station and reuse it (avoids O(dates x 145k) thrash).
        if _is_weatherapi(sig):
            db_path = sig._resolve_db_path()
            series_by_station = {}
            if db_path and os.path.exists(db_path):
                wconn = sqlite3.connect(db_path)
                for st in STATIONS:
                    series_by_station[st] = sig._build_daily_series(
                        sig._load_hourly(st, wconn)
                    )
                wconn.close()
            for st in STATIONS:
                series = series_by_station.get(st) or []
                idx_by_date = {d["date"]: i for i, d in enumerate(series)}
                st_trades = st_correct = 0
                for dt in dates:
                    actual = kdir.get((st, dt))
                    if actual is None or dt not in idx_by_date:
                        continue
                    try:
                        direction, conf = sig.evaluate(idx_by_date[dt] + 1, series)
                    except Exception:  # noqa: BLE001
                        continue
                    if direction is None or conf <= 0:
                        continue
                    st_trades += 1
                    trades += 1
                    if direction == actual:
                        st_correct += 1
                        correct += 1
                if st_trades:
                    per_station[st] = {
                        "trades": st_trades,
                        "correct": st_correct,
                        "accuracy": round(100.0 * st_correct / st_trades, 2),
                    }
        else:
            for st in STATIONS:
                st_trades = st_correct = 0
                for dt in dates:
                    actual = kdir.get((st, dt))
                    if actual is None:
                        continue
                    try:
                        direction, conf = sig.evaluate_for_station(st, dt)
                    except Exception:  # noqa: BLE001
                        continue
                    if direction is None or conf <= 0:
                        continue
                    st_trades += 1
                    trades += 1
                    if direction == actual:
                        st_correct += 1
                        correct += 1
                if st_trades:
                    per_station[st] = {
                        "trades": st_trades,
                        "correct": st_correct,
                        "accuracy": round(100.0 * st_correct / st_trades, 2),
                    }
        results[name] = {
            "trades": trades,
            "correct": correct,
            "accuracy": round(100.0 * correct / trades, 2) if trades else None,
            "stations_with_trades": len(per_station),
            "per_station": per_station,
        }

    mconn.close()
    kconn.close()

    out = {
        "lane": "db_backed_signals",
        "window": [args.start, args.end],
        "stations": len(STATIONS),
        "ground_truth": "kalshi_settlements (consecutive-temp direction)",
        "signals": results,
    }
    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(out, f, indent=2)

    print(f"DB-backed lane — {args.start}..{args.end}, {len(STATIONS)} stations")
    for name in SIGNALS:
        r = results[name]
        if "error" in r:
            print(f"  {name:26} ERROR {r['error']}")
        else:
            print(
                f"  {name:26} trades={r['trades']:5} acc={r['accuracy']} "
                f"stations={r['stations_with_trades']}"
            )
    print(f"Saved: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
