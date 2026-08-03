#!/usr/bin/env python3
"""
CLI Settlement Verification — Compare NWS official observations to Kalshi settlement data.

The 66.2% baseline accuracy comes from comparing GEFS predictions to Kalshi settlement
data. The Gray Room flagged that this may not match the authoritative NWS CLI (Climate
Local Data) source.

This script fetches NWS daily observations for all 20 stations via the NWS API
(https://api.weather.gov/stations/{ICAO}/observations), computes daily max temps,
and compares them to the Kalshi settlement database.

If agreement > 95%, proceed to P1. If not, STOP and report.

Usage:
    python3 scripts/verify_cli_settlements.py [--max-observations 2000] [--output-dir ...]

Output:
    docs/weather-engine/backtests/cli_verification_20260803.json

Stop condition:
    If any station has < 95% agreement with Kalshi settlements, flag it.

Author: Gilfoyle (dispatch Aug 3, 2026, B-mode post-Gray-Room)
"""

import argparse
import json
import math
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

# ─── Paths ───────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

from core.station_time import station_timezone_name

SETTLEMENTS_DB = str(REPO_ROOT / "data" / "kalshi_settlements.db")
OUTPUT_DIR = REPO_ROOT / "docs" / "weather-engine" / "backtests"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# 20 canonical stations
STATIONS = [
    "KATL", "KAUS", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU", "KLAS",
    "KLAX", "KMDW", "KMIA", "KMSP", "KMSY", "KNYC", "KOKC", "KPHL",
    "KPHX", "KSAT", "KSEA", "KSFO",
]

# ─── NWS API Client ──────────────────────────────────────────────────────────

NWS_BASE = "https://api.weather.gov"
USER_AGENT = "KalshiMetarMonitor/1.1 (verification@openclaw.ai)"
NWS_DELAY_SEC = 0.25  # 4 req/s rate limit buffer


def fetch_nws_observations(icao: str, max_obs: int = 5000) -> List[dict]:
    """
    Fetch METAR observations from NWS API for a station using pagination.
    Returns list of dicts with fields: timestamp, temperature (Celsius).
    """
    observations = []
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/geo+json",
    }

    url = f"{NWS_BASE}/stations/{icao}/observations?limit=500"

    while url and len(observations) < max_obs:
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"[WARN] NWS API error for {icao}: {e}")
            break

        features = data.get("features", [])
        for feat in features:
            props = feat.get("properties", {})
            timestamp = props.get("timestamp")
            temp_c = props.get("temperature", {}).get("value")
            if timestamp and temp_c is not None:
                observations.append({
                    "timestamp": timestamp,
                    "temp_c": temp_c,
                    "temp_f": temp_c * 9.0 / 5.0 + 32.0,
                })

        # Pagination via cursor
        pagination = data.get("pagination", {})
        next_url = pagination.get("next") if isinstance(pagination, dict) else None
        if next_url:
            url = next_url
        else:
            url = None

        time.sleep(NWS_DELAY_SEC)

    return observations[:max_obs]


def compute_daily_max_from_observations(
    observations: List[dict], station: str
) -> Dict[str, Tuple[float, int]]:
    """
    Group observations by station-local date and compute daily max temp.
    Returns {date_str: (max_temp_f, observation_count)}.
    Uses station timezone to determine the local date.
    """
    from zoneinfo import ZoneInfo
    try:
        tz_name = station_timezone_name(station)
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc

    daily = defaultdict(list)
    for obs in observations:
        ts = obs["timestamp"]
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            local_dt = dt.astimezone(tz)
            date_key = local_dt.strftime("%Y-%m-%d")
        except Exception:
            continue
        daily[date_key].append(obs["temp_f"])

    result = {}
    for date_key, temps in daily.items():
        result[date_key] = (max(temps), len(temps))

    return result


# ─── Kalshi Settlement Data Access ──────────────────────────────────────────

def load_kalshi_settlements() -> Dict[str, Dict[str, float]]:
    """
    Load Kalshi settlement data from the database.
    Returns {station: {target_date: temp_f}}.
    """
    conn = sqlite3.connect(SETTLEMENTS_DB)
    cur = conn.execute(
        "SELECT station, target_date, kalshi_temp FROM kalshi_settlements "
        "WHERE kalshi_temp IS NOT NULL ORDER BY station, target_date"
    )
    settlements: Dict[str, Dict[str, float]] = defaultdict(dict)
    for station, target_date, kalshi_temp in cur.fetchall():
        settlements[station.upper()][target_date] = kalshi_temp
    conn.close()
    return settlements


# ─── Comparison Logic ────────────────────────────────────────────────────────

def compute_agreement(
    settlement_dates: Dict[str, float],
    nws_daily_max: Dict[str, Tuple[float, int]],
    station: str,
) -> dict:
    """
    Compare NWS daily max temps with Kalshi settlement temps for overlapping dates.
    Returns agreement stats.
    """
    matched_dates = []
    for date_str, kalshi_temp in settlement_dates.items():
        if date_str in nws_daily_max:
            nws_temp, n_obs = nws_daily_max[date_str]
            diff = abs(kalshi_temp - nws_temp)
            matched_dates.append({
                "date": date_str,
                "kalshi_temp": kalshi_temp,
                "nws_temp": round(nws_temp, 2),
                "diff": round(diff, 2),
                "n_observations": n_obs,
            })

    if not matched_dates:
        return {
            "station": station,
            "n_matched": 0,
            "n_kalshi_dates": len(settlement_dates),
            "nws_dates_total": len(nws_daily_max),
            "agreement_rate": 0.0,
            "mean_abs_diff": None,
            "max_diff": None,
            "systematic_bias": None,
            "matched_dates": [],
            "status": "NO_MATCHED_DATES",
        }

    diffs = [m["diff"] for m in matched_dates]
    kalshi_values = [m["kalshi_temp"] for m in matched_dates]
    nws_values = [m["nws_temp"] for m in matched_dates]
    mean_bias = sum(nws_values[i] - kalshi_values[i] for i in range(len(matched_dates))) / len(matched_dates)

    # Agreement: diff <= 1.0°F (NWS reported temp to nearest 0.1°C ≈ 0.18°F;
    # Kalshi rounds to 1°F typically; 1°F tolerance accounts for rounding)
    agreement_threshold = 1.0
    n_agree = sum(1 for d in diffs if d <= agreement_threshold)

    # Systematic bias detection: if mean bias > 1°F or < -1°F, flag it
    systematic_bias = mean_bias > 1.0 or mean_bias < -1.0

    return {
        "station": station,
        "n_matched": len(matched_dates),
        "n_kalshi_dates": len(settlement_dates),
        "nws_dates_total": len(nws_daily_max),
        "agreement_rate": round(n_agree / len(matched_dates), 4) if matched_dates else 0.0,
        "mean_abs_diff": round(sum(diffs) / len(diffs), 2),
        "max_diff": round(max(diffs), 2),
        "mean_bias_f": round(mean_bias, 3),
        "systematic_bias": systematic_bias,
        "systematic_bias_flag": "SIGNIFICANT" if systematic_bias else "OK",
        "agreement_threshold_f": agreement_threshold,
        "status": "OK",
    }


def main():
    parser = argparse.ArgumentParser(description="CLI Settlement Verification")
    parser.add_argument("--max-observations", type=int, default=2000,
                        help="Max observations to fetch per station (default: 2000)")
    parser.add_argument("--skip-nws", action="store_true",
                        help="Skip NWS API fetch (use cached results)")
    args = parser.parse_args()

    print("=" * 72)
    print("  CLI Settlement Verification")
    print("=" * 72)
    print(f"  Comparing NWS observations to Kalshi settlements for {len(STATIONS)} stations")
    print(f"  Max observations per station: {args.max_observations}")
    print()

    # Load Kalshi settlements
    print("  Loading Kalshi settlement data...")
    kalshi = load_kalshi_settlements()
    total_kalshi = sum(len(v) for v in kalshi.values())
    print(f"  Loaded {total_kalshi} settlement records across {len(kalshi)} stations")
    print()

    # Fetch NWS observations and compute daily max
    print(f"  Fetching NWS observations for {len(STATIONS)} stations...")
    print()

    all_results = {}
    overall_agreement_diffs = []
    stop_conditions_hit = []

    for station in sorted(STATIONS):
        print(f"  [{station}] Fetching observations...", end=" ", flush=True)

        observations = fetch_nws_observations(station, args.max_observations)
        if not observations:
            print(f"ZERO OBSERVATIONS")
            all_results[station] = {
                "station": station,
                "n_matched": 0,
                "n_kalshi_dates": len(kalshi.get(station, {})),
                "nws_dates_total": 0,
                "agreement_rate": 0.0,
                "mean_abs_diff": None,
                "max_diff": None,
                "systematic_bias": None,
                "status": "NO_NWS_DATA",
                "stop_condition": True,
            }
            stop_conditions_hit.append(f"{station}: No NWS data returned")
            continue

        nws_daily = compute_daily_max_from_observations(observations, station)
        print(f"got {len(observations)} obs, {len(nws_daily)} days", end=" ... ", flush=True)

        settlement_data = kalshi.get(station, {})
        result = compute_agreement(settlement_data, nws_daily, station)
        all_results[station] = result

        if result["status"] == "OK":
            print(f"agree={result['agreement_rate']*100:.1f}% "
                  f"mean|diff|={result['mean_abs_diff']:.2f}°F "
                  f"bias={result['mean_bias_f']:+.3f}°F "
                  f"n={result['n_matched']}",
                  end="")
            overall_agreement_diffs.extend([m["diff"] for m in result.get("matched_dates", [])])

            if result["agreement_rate"] < 0.95:
                print(f" ⚠️ BELOW 95% THRESHOLD", end="")
                stop_conditions_hit.append(
                    f"{station}: Agreement rate {result['agreement_rate']*100:.1f}% < 95%"
                )

            if result["systematic_bias"]:
                print(f" ⚠️ SYSTEMATIC BIAS ({result['mean_bias_f']:+.3f}°F)", end="")
                stop_conditions_hit.append(
                    f"{station}: Systematic bias {result['mean_bias_f']:+.3f}°F"
                )
        else:
            print(f"STATUS={result['status']}", end="")

        print()

    # Aggregate stats
    print()
    print("  " + "=" * 50)
    print("  AGGREGATE RESULTS")
    print("  " + "=" * 50)

    ok_results = [r for r in all_results.values() if r["status"] == "OK"]

    if ok_results:
        overall_agreement = sum(r["agreement_rate"] * r["n_matched"] for r in ok_results)
        total_matched = sum(r["n_matched"] for r in ok_results)
        overall_rate = overall_agreement / total_matched if total_matched > 0 else 0.0

        print(f"  Overall agreement rate: {overall_rate*100:.2f}% ({total_matched} matched dates)")
        if overall_agreement_diffs:
            mean_diff = sum(overall_agreement_diffs) / len(overall_agreement_diffs)
            max_diff = max(overall_agreement_diffs)
            print(f"  Overall mean absolute diff: {mean_diff:.2f}°F")
            print(f"  Overall max diff: {max_diff:.2f}°F")
    else:
        overall_rate = 0.0

    # Check stop conditions
    print()
    if stop_conditions_hit:
        print("  ⚠️ STOP CONDITIONS HIT:")
        for sc in stop_conditions_hit:
            print(f"    - {sc}")
        print()
        print("  ❌ VERIFICATION FAILED — Proceed to P1 halted.")
        print("     Report to Donna: CLI data does not confirm Kalshi settlements.")
    else:
        print("  ✅ All stations pass agreement threshold (>=95%)")
        print("     Proceed to P1.")

    # Determine overall status
    overall_status = "PASS" if not stop_conditions_hit else "FAIL"

    if overall_rate >= 0.95 and not stop_conditions_hit:
        print("  ✅ OVERALL: CLI data CONFIRMS Kalshi settlements. Proceed to P1.")
        overall_status = "PASS"
    elif overall_rate >= 0.90:
        print("  ⚠️ OVERALL: Marginal agreement. Investigate flagged stations.")
        overall_status = "MARGINAL"
    else:
        print("  ❌ OVERALL: CLI data DISAGREES with Kalshi settlements. STOP.")
        overall_status = "FAIL"

    # Build output JSON
    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "config": {
            "stations": STATIONS,
            "max_observations_per_station": args.max_observations,
            "agreement_threshold_f": 1.0,
            "nws_api": f"{NWS_BASE}/stations/{{ICAO}}/observations",
        },
        "overall": {
            "status": overall_status,
            "overall_agreement_rate": round(overall_rate, 4),
            "total_matched_dates": total_matched,
            "overall_mean_abs_diff": round(sum(overall_agreement_diffs) / len(overall_agreement_diffs), 2) if overall_agreement_diffs else None,
            "overall_max_diff": round(max(overall_agreement_diffs), 2) if overall_agreement_diffs else None,
            "total_kalshi_records": total_kalshi,
            "stop_conditions_hit": stop_conditions_hit if stop_conditions_hit else None,
            "proceed_to_p1": overall_status == "PASS",
        },
        "per_station": all_results,
    }

    output_path = OUTPUT_DIR / "cli_verification_20260803.json"
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\n  Results saved to: {output_path}")
    print("  Done.\n")


if __name__ == "__main__":
    main()