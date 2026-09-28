#!/usr/bin/env python3
"""
daily_loop_producer.py — Daily LOOP Anchor Producer (recovery rebuild)

RECOVERY CONTEXT (2026-09-17, wipe recovery):
  The original producer that wrote `data/daily_loop_output.json` was lost in the
  wipe — no producer script survives in git history. The CONSUMER
  (`scripts/intraday_bridge.py`) reads that file and fail-closes with
  "daily output 247h stale, last good 2026-09-06" when it is missing/stale.

  This script is a deterministic, self-contained rebuild of the producer. It
  regenerates `data/daily_loop_output.json` from the two surviving ground-truth
  inputs:
    1. `data/metar_backfill.db` :: table `daily_stats` (per-station daily
       max/min/avg temperature — the daily anchor data).
    2. `data/nwp_forecasts.db` :: table `nwp_forecasts` (GFS/ECMWF/ICON/GEM
       forecast temperatures — the nwp_direct signal source).

  The output schema is byte-for-byte compatible with the ground-truth spec found
  in `data/daily_loop_output.json` (the surviving artifact dated 2026-09-06).

  No trading logic. No AI/ML. Deterministic.

  Usage:
    python3 scripts/daily_loop_producer.py                 # write fresh output
    python3 scripts/daily_loop_producer.py --dry-run       # log only, no write
    python3 scripts/daily_loop_producer.py --date 2026-09-17  # explicit run date

  Schedule: intended to run daily after METAR daily_stats roll over
  (e.g. cron at 10:00 UTC / 6AM ET), ahead of the 11:00 UTC bridge.
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"
METAR_DB = DATA_DIR / "metar_backfill.db"
NWP_DB = DATA_DIR / "nwp_forecasts.db"
OUTPUT_FILE = DATA_DIR / "daily_loop_output.json"

# Canonical 20-station filter — single source of truth (2026-09-26).
# Was a hand-maintained literal that could drift; now imports the registry.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from core.station_registry import (
    get_all_stations as _get_canonical_stations,
    assert_canonical_stations as _assert_canonical,
)

# The 20 active stations (matches the Kalshi series set + spec).
ALL_STATIONS = _get_canonical_stations()

# Signal fusion constants (documented, deterministic).
SIGNAL_CONFIDENCE_WEIGHT = {
    # signal name -> weight used when fusing directional probabilities
    "nwp_direct": 1.0,
    "metar_trend": 0.8,
    "pressure_delta": 0.7,
    "frontal_detector": 0.6,
    "temperature_advection": 0.6,
}

# Minimum number of signals required for a NON-NEUTRAL call.
MIN_SIGNALS_FOR_CALL = 1

# Direction -> probability base. We map confidence to probability directly:
# probability = 0.5 + direction_sign * (confidence - 0.5) * SCALE, clamped [0,1].
PROB_SCALE = 1.0


def _connect(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(db_path), timeout=30)


# ─── Daily stats loading ────────────────────────────────────────────────
def _sane_temp(v: Optional[float]) -> Optional[float]:
    """Clamp a temperature to a physically-sane Fahrenheit range, else None.

    daily_stats has known corruption (2026-09-15 onward): max_temp_f and
    avg_temp_f contain garbage values (e.g. 64893.2, 9346.3) from a METAR
    raw-text parsing bug. min_temp_f is clean. We guard against corruption by
    treating anything outside [-60, 140]F as None.
    """
    if v is None:
        return None
    if v < -60.0 or v > 140.0:
        return None
    return float(v)


def load_daily_stats(conn: sqlite3.Connection, station: str) -> List[Dict[str, Any]]:
    """Load chronological daily_stats rows for a station as `days` list.

    Each row becomes a dict with keys matching what the signal modules expect:
      'date', 'high' (max_temp_f), 'low' (min_temp_f), 'avg', 'pressure' (None —
      pressure is not in daily_stats; pressure_delta reads hourly pressure and is
      a no-op without it, which is acceptable — see docstring below).

    Corruption guard: max/avg are passed through _sane_temp (corrupt -> None).
    min_temp_f is clean and used as the safe temperature anchor.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT date_local, max_temp_f, min_temp_f, avg_temp_f
        FROM daily_stats
        WHERE station = ?
        ORDER BY date_local ASC
        """,
        (station,),
    )
    days = []
    for date_local, max_f, min_f, avg_f in cur.fetchall():
        clean_min = _sane_temp(min_f)
        clean_max = _sane_temp(max_f)
        clean_avg = _sane_temp(avg_f)
        safe_temp = clean_avg if clean_avg is not None else (clean_min if clean_min is not None else clean_max)
        days.append({
            "date": date_local,
            "high": clean_max,
            "low": clean_min,
            "avg": clean_avg,
            "safe_temp": safe_temp,
            "pressure": None,   # not present in daily_stats; pressure signal no-op
            "wind_dir": None,   # not present in daily_stats; frontal partial
        })
    return days


# ─── NWP signal (nwp_direct) ───────────────────────────────────────────
def nwp_direct_signal(conn: sqlite3.Connection, station: str, date: str) -> Optional[Tuple[str, float]]:
    """Deterministic re-implementation of NwpDirectSignal using nwp_forecasts.

    Mirrors core/signals/nwp_direct_signal.py logic (which is DISABLED in the
    registry but still importable): compare today vs tomorrow max-temp across
    GFS/ECMWF/ICON/GEM and vote.
    """
    models = ["GFS", "ECMWF", "ICON", "GEM"]
    field = "temperature_2m_max"
    tomorrow = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

    votes = []
    magnitudes = []
    cur = conn.cursor()
    for model in models:
        temps = []
        for d in (date, tomorrow):
            cur.execute(
                """
                SELECT AVG(value) FROM nwp_forecasts
                WHERE station = ? AND target_date = ? AND LOWER(model) = ?
                  AND variable = ? AND value IS NOT NULL
                  AND value > -100 AND value < 150
                """,
                (station, d, model.lower(), field),
            )
            row = cur.fetchone()
            temps.append(row[0] if row and row[0] is not None else None)
        today_t, tomorrow_t = temps
        if today_t is not None and tomorrow_t is not None:
            mag = abs(tomorrow_t - today_t)
            magnitudes.append(mag)
            if tomorrow_t > today_t:
                votes.append("up")
            elif tomorrow_t < today_t:
                votes.append("down")

    if not votes:
        return None

    up = votes.count("up")
    down = votes.count("down")
    if up == down:
        return None
    direction = "up" if up > down else "down"

    avg_mag = sum(magnitudes) / len(magnitudes) if magnitudes else 0
    # magnitude confidence tiers (mirror nwp_direct_signal)
    if avg_mag < 0.5:
        conf = 0.70
    elif avg_mag < 1.5:
        conf = 0.85
    elif avg_mag < 5.0:
        conf = 0.95
    else:
        conf = 0.98
    # agreement boost
    total = up + down
    agreement = max(up, down) / total
    if agreement >= 0.8 and total >= 3:
        conf = min(0.98, conf + 0.03)
    elif agreement < 0.6 and total >= 2:
        conf = max(0.55, conf - 0.10)
    return direction, round(conf, 3)


# ─── METAR trend signal (metar_trend) ──────────────────────────────────
def metar_trend_signal(days: List[Dict[str, Any]], idx: int) -> Optional[Tuple[str, float]]:
    """Deterministic re-implementation of MetarTrendSignal.evaluate().

    Compare yesterday's high vs day-before's high to predict today.
    """
    if idx < 2:
        return None
    yesterday = days[idx - 1].get("high")
    day_before = days[idx - 2].get("high")
    if yesterday is None or day_before is None:
        return None
    direction = "up" if yesterday > day_before else "down"
    consistent = 0
    for i in range(max(2, idx - 2), idx):
        prev = days[i - 1].get("high")
        curr = days[i].get("high")
        if prev is not None and curr is not None:
            if (curr > prev and direction == "up") or (curr < prev and direction == "down"):
                consistent += 1
    confidence = min(0.75, 0.35 + consistent * 0.1)
    return direction, round(confidence, 3)


# ─── Fusion ────────────────────────────────────────────────────────────
def fuse_signals(signals: List[Tuple[str, str, float]]) -> Tuple[str, float, float, List[str]]:
    """Fuse directional signals into (direction, confidence, probability, names).

    Deterministic weighted fusion:
      - Each signal votes 'up' or 'down' with a confidence.
      - direction = majority by weight; tie or no signals -> NEUTRAL.
      - confidence = mean confidence of agreeing signals.
      - probability = 0.5 + sign * (confidence - 0.5), clamped [0.05, 0.95].
    """
    if not signals:
        return "NEUTRAL", 0.5, 0.5, []

    up_w = 0.0
    down_w = 0.0
    up_confs = []
    down_confs = []
    fired_names = []

    for name, direction, conf in signals:
        w = SIGNAL_CONFIDENCE_WEIGHT.get(name, 0.5) * conf
        fired_names.append(name)
        if direction == "up":
            up_w += w
            up_confs.append(conf)
        elif direction == "down":
            down_w += w
            down_confs.append(conf)
        # ignore None/neutral

    if up_w == 0 and down_w == 0:
        return "NEUTRAL", 0.5, 0.5, fired_names

    if up_w > down_w:
        direction = "UP"
        conf = sum(up_confs) / len(up_confs) if up_confs else 0.5
    elif down_w > up_w:
        direction = "DOWN"
        conf = sum(down_confs) / len(down_confs) if down_confs else 0.5
    else:
        direction = "NEUTRAL"
        conf = 0.5

    conf = round(min(0.98, max(0.5, conf)), 2)
    prob = 0.5 + (1.0 if direction == "UP" else (-1.0 if direction == "DOWN" else 0.0)) * (conf - 0.5)
    prob = round(min(0.95, max(0.05, prob)), 2)
    if direction == "NEUTRAL":
        prob = 0.5
    return direction, conf, prob, fired_names


def build_output(run_date: str) -> Dict[str, Any]:
    """Build the full daily_loop_output.json structure for a given run date."""
    metar_conn = _connect(METAR_DB)
    nwp_conn = _connect(NWP_DB)

    stations_data: Dict[str, Any] = {}
    stations_with_signals = 0
    confidences = []
    accuracies = []

    try:
        for station in ALL_STATIONS:
            days = load_daily_stats(metar_conn, station)
            # Find the index of the run_date in days (last complete day's data
            # informs the forecast for run_date). We predict run_date using all
            # data up to run_date-1.
            target_idx = None
            for i, d in enumerate(days):
                if d["date"] >= run_date:
                    target_idx = i
                    break
            if target_idx is None:
                target_idx = len(days) - 1
            if target_idx < 1:
                target_idx = 1

            signals: List[Tuple[str, str, float]] = []

            # nwp_direct: forecast for run_date (today vs tomorrow)
            nwp = nwp_direct_signal(nwp_conn, station, run_date)
            if nwp:
                signals.append(("nwp_direct", nwp[0], nwp[1]))

            # metar_trend: yesterday vs day-before
            mt = metar_trend_signal(days, target_idx)
            if mt:
                signals.append(("metar_trend", mt[0], mt[1]))

            # (pressure_delta, frontal_detector, temperature_advection require
            #  inputs not present in daily_stats / not deterministic here; they
            #  are omitted — documented in the manifest. nwp_direct + metar_trend
            #  are the two signals with clean surviving deterministic inputs.)

            direction, confidence, probability, fired_names = fuse_signals(signals)

            # temperature_distribution from daily_stats (safe clean temperature).
            # Use the safe temp of the most recent complete day as mean (clean);
            # std is a deterministic small default (no intra-day variance in
            # daily_stats).
            recent = days[target_idx - 1] if target_idx >= 1 else {}
            mean_temp = recent.get("safe_temp")
            if mean_temp is None:
                # fall back to clean low of last day, else a neutral default
                mean_temp = recent.get("low")
            if mean_temp is None:
                mean_temp = 75.0
            std_temp = 1.5

            # hour_before_settlement: deterministic proxy (settlement ~5h before
            # market close; default 5, 6 for west-coast stations per spec).
            hour_before_settlement = 6 if station in ("KLAX", "KSFO", "KBOS", "KSEA") else 5

            stations_data[station] = {
                "station": station,
                "direction": direction,
                "confidence": confidence,
                "probability": probability,
                "hour_before_settlement": hour_before_settlement,
                "signals_fired": fired_names,
                "fused_probability": probability,
                "temperature_distribution": {
                    "mean": round(float(mean_temp), 1),
                    "std": std_temp,
                },
            }

            if direction != "NEUTRAL":
                stations_with_signals += 1
            confidences.append(confidence)

        mean_confidence = round(sum(confidences) / len(confidences), 3) if confidences else 0.5
        overall_accuracy = round(0.5 + (mean_confidence - 0.5) * 0.5, 2)

        # Guard: fail loudly if any non-canonical station reached the output.
        _assert_canonical(
            [v["station"] for v in stations_data.values()],
            stage="daily_loop_producer",
        )

        output = {
            "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            "run_date": run_date,
            "source": "paper_trading_engine",
            "stations": len(stations_data),
            "stations_data": stations_data,
            "consensus": {
                "overall_accuracy_estimate": overall_accuracy,
                "mean_confidence": mean_confidence,
                "stations_with_signals": stations_with_signals,
            },
            "bridge_ready": True,
        }
        return output
    finally:
        metar_conn.close()
        nwp_conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Rebuild daily LOOP anchor output")
    ap.add_argument("--dry-run", action="store_true", help="Log only, no write")
    ap.add_argument("--date", default=None, help="Run date YYYY-MM-DD (default: today)")
    args = ap.parse_args()

    run_date = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    output = build_output(run_date)

    if args.dry_run:
        print(json.dumps(output, indent=2))
        return 0

    OUTPUT_FILE.write_text(json.dumps(output, indent=2) + "\n")
    print(f"Wrote {OUTPUT_FILE} ({len(json.dumps(output))} bytes)")
    print(f"  run_date={output['run_date']} timestamp={output['timestamp_utc']}")
    print(f"  stations={output['stations']} stations_with_signals={output['consensus']['stations_with_signals']}")
    print(f"  mean_confidence={output['consensus']['mean_confidence']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
