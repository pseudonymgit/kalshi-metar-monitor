#!/usr/bin/env python3
"""
NWP May-June 2026 Short-Lead Re-fetch — standalone deterministic backfill.

Closes the Weather Engine lead-time gap: zero real short-lead (0-3 day)
forecast rows exist in data/nwp_forecasts.db for fetch dates in
2026-05-01..2026-06-30 (existing May/June rows are hindsight or 28-70 day
long-lead fetches from 2026-04-24). Blocks held-out validation of
NWP-dependent Gamma signals.

Source: Open-Meteo Single Runs API (https://single-runs-api.open-meteo.com/v1/forecast)
        with run=<init_date>T00 — archives individual historical model runs
        from 2026-04-02 onward. Verified 2026-09-14: returns the real
        historical forecast issued at the requested init time (hourly series
        starts exactly at the run timestamp). Do NOT use past_days= (returns
        hindsight, not real forecasts).

Verified pre-flight (2026-09-14):
  - gfs_seamless, ecmwf_ifs025, icon_seamless: batched 20-station calls OK,
    all 11 variables present, day-0 daily precipitation_sum is None in the
    archive (expected; matches collector's None-skip behavior).
  - gem_seamless (cmc_gem_gdps_15km_upper_level): archive starts 2026-05-28
    (runs 2026-04-02..2026-05-27 NOT archived, any cycle time). Backend is
    burst-sensitive: batched multi-location calls reliably return empty
    bodies -> GEM is fetched per-station with longer spacing.
  - gefs_ens: runs exist on noaa-gefs-pds S3 (GRIB/NetCDF, 31 members) but
    are outside the Single-Runs API scope of this backfill — NOT fetched.
  - nws_mos: no retrievable archive. era5: reanalysis, already present.
  - No unique-key collisions: DB has no rows with fetch_date 2026-04-28..30
    for the 4 operational models (April fetches were 2026-04-24 only), and
    no May/June fetch dates exist. All inserts are purely additive; existing
    750,840 rows are not modified.

Storage semantics mirror scripts/nwp_collect.py parse_and_store():
  daily vars stored as-is; hourly vars aggregated to <var>_daily_mean by UTC
  date; None values skipped; INSERT OR REPLACE on
  UNIQUE(fetch_date, target_date, station, model, variable) => idempotent.

Usage:
  python3 scripts/nwp_may_june_refetch.py [--force] [--delay 1.0]
      [--models gfs,ecmwf,icon,gem] [--start 2026-04-28] [--end 2026-06-30]

Run scope (defaults):
  fetch dates: 2026-04-28 .. 2026-06-30 (GEM: 2026-05-28 .. 2026-06-30)
  lead times : 0-3 days (forecast_days=4)
  targets    : only target_date in [2026-05-01, 2026-06-30] are stored
               (gives complete lead 0-3 coverage for every May/June target)
"""

import sqlite3
import urllib.request
import urllib.error
import json
import time
import argparse
import random
from pathlib import Path
from datetime import datetime, timezone, timedelta

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DB_PATH = PROJECT_ROOT / "data" / "nwp_forecasts.db"

UA = "OpenClaw-WeatherEngine/1.0 (nwp_may_june_refetch backfill)"

# 20 Kalshi cities — identical to scripts/nwp_collect.py CITIES
STATIONS = [
    ("KATL", "Atlanta", 33.64, -84.43),
    ("KAUS", "Austin", 30.20, -97.67),
    ("KBOS", "Boston", 42.37, -71.01),
    ("KDCA", "Washington DC", 38.85, -77.04),
    ("KDEN", "Denver", 39.86, -104.67),
    ("KDFW", "Dallas", 32.90, -97.04),
    ("KHOU", "Houston", 29.98, -95.36),
    ("KLAS", "Las Vegas", 36.08, -115.16),
    ("KLAX", "Los Angeles", 33.94, -118.41),
    ("KMDW", "Chicago", 41.79, -87.75),
    ("KMIA", "Miami", 25.80, -80.29),
    ("KMSP", "Minneapolis", 44.88, -93.22),
    ("KMSY", "New Orleans", 29.99, -90.26),
    ("KNYC", "New York", 40.71, -74.01),
    ("KOKC", "Oklahoma City", 35.39, -97.60),
    ("KPHL", "Philadelphia", 39.87, -75.24),
    ("KPHX", "Phoenix", 33.43, -112.01),
    ("KSAT", "San Antonio", 29.53, -98.47),
    ("KSEA", "Seattle", 47.45, -122.31),
    ("KSFO", "San Francisco", 37.62, -122.38),
]

# (db_model_name, single_runs_models_param, first_fetch_date, batch_size)
# batch_size=20 => all stations in one call; gem backend requires per-station (1).
MODELS = [
    ("gfs",   "gfs_seamless",  "2026-04-28", 20),
    ("ecmwf", "ecmwf_ifs025",  "2026-04-28", 20),
    ("icon",  "icon_seamless", "2026-04-28", 20),
    ("gem",   "gem_seamless",  "2026-05-28", 1),
]

API_BASE = "https://single-runs-api.open-meteo.com/v1/forecast"
FORECAST_DAYS = 4  # leads 0-3
TARGET_MIN = "2026-05-01"
TARGET_MAX = "2026-06-30"

DAILY_VARS = ["temperature_2m_max", "temperature_2m_min", "precipitation_sum"]
HOURLY_VARS = [
    "temperature_850hPa", "wind_speed_10m", "wind_direction_10m",
    "wind_speed_850hPa", "wind_direction_850hPa", "cloud_cover",
    "dew_point_2m", "geopotential_height_500hPa",
]


def parse_args():
    p = argparse.ArgumentParser(description="NWP May-June 2026 short-lead re-fetch")
    p.add_argument("--force", action="store_true",
                   help="re-fetch dates even if fetch_log shows a clean prior run")
    p.add_argument("--delay", type=float, default=1.0,
                   help="seconds between API calls (default 1.0)")
    p.add_argument("--models", type=str, default="gfs,ecmwf,icon,gem",
                   help="comma-separated subset of models to run")
    p.add_argument("--start", type=str, default=None,
                   help="override global first fetch date (per-model floor still applies)")
    p.add_argument("--end", type=str, default=None,
                   help="override last fetch date (default 2026-06-30)")
    return p.parse_args()


def http_get_json(url, timeout=120, max_tries=5):
    """GET with retry/backoff. Treats 429/5xx/empty-body-200 as retryable."""
    delay = 2.0
    last_err = None
    for attempt in range(max_tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                code = resp.getcode()
                body = resp.read()
            if code == 200 and len(body) == 0:
                # GEM backend occasionally returns empty 200 bodies under burst
                last_err = "HTTP 200 with empty body"
            else:
                try:
                    return json.loads(body.decode("utf-8")), None
                except (ValueError, UnicodeDecodeError) as e:
                    last_err = f"non-JSON body ({len(body)} bytes): {body[:80]!r}"
        except urllib.error.HTTPError as e:
            retry_after = e.headers.get("Retry-After") if e.headers else None
            body_head = ""
            try:
                body_head = e.read().decode()[:200]
            except Exception:
                pass
            if e.code == 429 or e.code >= 500:
                last_err = f"HTTP {e.code}: {body_head}"
                sleep_for = float(retry_after) if retry_after else delay
                time.sleep(min(sleep_for, 90))
                delay = min(delay * 2.5, 90)
                continue
            return None, f"HTTP {e.code}: {body_head}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        if attempt < max_tries - 1:
            jitter = random.uniform(0.5, 1.5)
            time.sleep(min(delay * jitter, 90))
            delay = min(delay * 2.5, 90)
    return None, f"retries exhausted: {last_err}"


def build_url(model_param, lats, lons, init_date):
    return (
        f"{API_BASE}"
        f"?latitude={','.join(str(x) for x in lats)}"
        f"&longitude={','.join(str(x) for x in lons)}"
        f"&run={init_date}T00"
        f"&forecast_days={FORECAST_DAYS}"
        f"&timezone=UTC"
        f"&models={model_param}"
        f"&hourly={','.join(HOURLY_VARS)}"
        f"&daily={','.join(DAILY_VARS)}"
    )


def already_fetched(conn, model, init_date):
    """True if a clean fetch_log entry exists for this (init_date, model)."""
    c = conn.cursor()
    c.execute(
        "SELECT COUNT(*) FROM fetch_log "
        "WHERE fetch_date = ? AND model = ? AND errors IS NULL AND variables_fetched > 0",
        (init_date, model),
    )
    return c.fetchone()[0] > 0


def store_result(conn, result, model, station, init_date, fetch_timestamp):
    """Mirror nwp_collect.parse_and_store() for one station result."""
    c = conn.cursor()
    stored = 0
    if result.get("error") or ("daily" not in result and "hourly" not in result):
        return 0, [result.get("reason") or "no forecast data in response"]

    daily = result.get("daily") or {}
    daily_time = daily.get("time") or []
    for var in DAILY_VARS:
        values = daily.get(var) or []
        for i, target_date in enumerate(daily_time):
            if i < len(values) and values[i] is not None:
                if not (TARGET_MIN <= target_date <= TARGET_MAX):
                    continue
                c.execute(
                    """INSERT OR REPLACE INTO nwp_forecasts
                       (fetch_date, target_date, station, model, variable, value, fetch_timestamp)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (init_date, target_date, station, model, var,
                     float(values[i]), fetch_timestamp),
                )
                stored += 1

    hourly = result.get("hourly") or {}
    hourly_time = hourly.get("time") or []
    for var in HOURLY_VARS:
        values = hourly.get(var) or []
        if not values:
            continue
        daily_means = {}
        for i, ts in enumerate(hourly_time):
            if i < len(values) and values[i] is not None:
                date = ts[:10]
                daily_means.setdefault(date, []).append(float(values[i]))
        for target_date, vals in daily_means.items():
            if vals and TARGET_MIN <= target_date <= TARGET_MAX:
                mean = sum(vals) / len(vals)
                c.execute(
                    """INSERT OR REPLACE INTO nwp_forecasts
                       (fetch_date, target_date, station, model, variable, value, fetch_timestamp)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (init_date, target_date, station, model,
                     f"{var}_daily_mean", mean, fetch_timestamp),
                )
                stored += 1
    return stored, []


def log_fetch(conn, init_date, model, stations_ok, values_stored, errors, ts):
    conn.execute(
        """INSERT INTO fetch_log
           (fetch_date, model, stations_fetched, variables_fetched, errors, fetch_timestamp)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (init_date, model, stations_ok, values_stored,
         "; ".join(errors[:10]) if errors else None, ts),
    )
    conn.commit()


def process_date_for_model(conn, db_model, model_param, init_date, batch_size, delay, force):
    """Fetch one model run (init_date) for all stations. Returns (ok, stored, errors)."""
    if not force and already_fetched(conn, db_model, init_date):
        return "skipped", 0, []

    ts = datetime.now(timezone.utc).isoformat()
    total_stored = 0
    errors = []
    stations_ok = 0

    stations = STATIONS
    i = 0
    while i < len(stations):
        chunk = stations[i:i + batch_size]
        lats = [s[2] for s in chunk]
        lons = [s[3] for s in chunk]
        url = build_url(model_param, lats, lons, init_date)
        data, err = http_get_json(url)

        if err is None and isinstance(data, list) and len(data) == len(chunk):
            for j, res in enumerate(data):
                st_n, st_errs = store_result(conn, res, db_model, chunk[j][0], init_date, ts)
                if st_errs:
                    errors.append(f"{chunk[j][0]}: {st_errs[0]}")
                else:
                    stations_ok += 1
                total_stored += st_n
            i += len(chunk)
        elif err is None and isinstance(data, dict) and batch_size > 1:
            # single dict for multi-location request => unexpected; fall back per-station
            errors.append(f"batch: unexpected dict response, falling back per-station")
            for k in range(i, len(stations)):
                res, err2 = http_get_json(build_url(model_param, [stations[k][2]], [stations[k][3]], init_date))
                if err2:
                    errors.append(f"{stations[k][0]}: {err2}")
                else:
                    n, errs3 = store_result(conn, res, db_model, stations[k][0], init_date, ts)
                    total_stored += n
                    if errs3:
                        errors.extend(f"{stations[k][0]}: {e}" for e in errs3)
                    else:
                        stations_ok += 1
                time.sleep(1.0)
            i = len(stations)
        else:
            # hard failure of the whole call: retry this chunk per-station
            for k in range(i, i + len(chunk)):
                res, err2 = http_get_json(build_url(model_param, [stations[k][2]], [stations[k][3]], init_date))
                if err2:
                    errors.append(f"{stations[k][0]}: {err2}")
                else:
                    n, errs3 = store_result(conn, res, db_model, stations[k][0], init_date, ts)
                    total_stored += n
                    if errs3:
                        errors.extend(f"{stations[k][0]}: {e}" for e in errs3)
                    else:
                        stations_ok += 1
                time.sleep(1.0)
            i += len(chunk)
        time.sleep(delay)

    log_fetch(conn, init_date, db_model, stations_ok, total_stored, errors, ts)
    status = "ok" if not errors else ("partial" if stations_ok > 0 else "failed")
    return status, total_stored, errors


def main():
    args = parse_args()
    wanted = [m.strip() for m in args.models.split(",") if m.strip()]
    models = [m for m in MODELS if m[0] in wanted]
    end_date = args.end or "2026-06-30"

    print(f"=== NWP May-June 2026 short-lead re-fetch ===")
    print(f"DB: {DB_PATH}")
    print(f"Window: fetch dates -> {end_date}; targets stored {TARGET_MIN}..{TARGET_MAX}; leads 0-{FORECAST_DAYS-1}")
    print(f"Models: {[(m[0], m[1], m[2], f'batch{m[3]}') for m in models]}")
    print(f"Started: {datetime.now(timezone.utc).isoformat()}")

    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute("PRAGMA busy_timeout = 30000")
    c = conn.cursor()

    # sanity: schema present?
    c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='nwp_forecasts'")
    if c.fetchone()[0] != 1:
        raise SystemExit("FATAL: nwp_forecasts table missing")

    c.execute("SELECT COUNT(*) FROM nwp_forecasts")
    baseline_rows = c.fetchone()[0]
    print(f"Baseline row count: {baseline_rows}")

    # initialize backfill_progress rows (UNIQUE(model, station), additive only)
    for db_model, _, model_start, _ in models:
        for st in STATIONS:
            conn.execute(
                """INSERT OR IGNORE INTO backfill_progress (model, station, status, attempted_at)
                   VALUES (?, ?, 'pending', ?)""",
                (db_model, st[0], datetime.now(timezone.utc).isoformat()),
            )
    conn.commit()

    grand_total = 0
    summary = {}

    for db_model, model_param, model_start, batch_size in models:
        start = args.start or model_start
        if args.start and args.start > model_start:
            start = args.start  # per-model floor: never earlier than archive start
        d0 = datetime.fromisoformat(start).date()
        d1 = datetime.fromisoformat(end_date).date()
        print(f"\n--- Model {db_model} ({model_param}) fetch dates {start} .. {end_date} ---")

        model_stored = 0
        dates_ok = dates_partial = dates_failed = dates_skipped = 0
        station_fail_counts = {st[0]: 0 for st in STATIONS}
        station_err_msgs = {}

        d = d0
        while d <= d1:
            init_date = d.isoformat()
            status, stored, errors = process_date_for_model(
                conn, db_model, model_param, init_date, batch_size, args.delay, args.force)
            if status == "skipped":
                dates_skipped += 1
                print(f"  {init_date}: skipped (clean fetch_log entry exists)")
            else:
                model_stored += stored
                # attribute station-level failures
                for st_name in station_fail_counts:
                    if any(e.startswith(st_name + ":") for e in errors):
                        station_fail_counts[st_name] += 1
                        station_err_msgs[st_name] = next(
                            (e for e in errors if e.startswith(st_name + ":")), None)
                if status == "ok":
                    dates_ok += 1
                elif status == "partial":
                    dates_partial += 1
                else:
                    dates_failed += 1
                print(f"  {init_date}: {status} ({stored} values, {len(errors)} errors)")
            d += timedelta(days=1)
            time.sleep(args.delay)

        # finalize backfill_progress per station
        for st in STATIONS:
            st_name = st[0]
            if station_fail_counts[st_name] == 0:
                new_status = "complete"
                err_msg = None
            else:
                new_status = "failed"
                err_msg = f"{station_fail_counts[st_name]} dates failed; last: {station_err_msgs.get(st_name)}"
            conn.execute(
                """UPDATE backfill_progress
                   SET status = ?, completed_at = ?, error_msg = ?
                   WHERE model = ? AND station = ?""",
                (new_status, datetime.now(timezone.utc).isoformat(), err_msg, db_model, st_name),
            )
        conn.commit()

        grand_total += model_stored
        summary[db_model] = dict(stored=model_stored, ok=dates_ok, partial=dates_partial,
                                 failed=dates_failed, skipped=dates_skipped)
        print(f"  => {db_model}: {model_stored} values | dates ok={dates_ok} partial={dates_partial} "
              f"failed={dates_failed} skipped={dates_skipped}")

    # final summary from DB
    print("\n=== Verification queries ===")
    print(f"Rows added this run (approx): {grand_total}")
    c.execute("""SELECT model, COUNT(*) FROM nwp_forecasts
                 WHERE target_date BETWEEN ? AND ? AND model IN ('gfs','ecmwf','icon','gem')
                 GROUP BY model""", (TARGET_MIN, TARGET_MAX))
    for r in c.fetchall():
        print(f"  target-window rows for {r[0]}: {r[1]}")
    c.execute("""SELECT model,
                        SUM(CASE WHEN CAST(julianday(target_date)-julianday(fetch_date) AS INT) BETWEEN 0 AND 3 THEN 1 ELSE 0 END)
                 FROM nwp_forecasts
                 WHERE target_date BETWEEN ? AND ? AND model IN ('gfs','ecmwf','icon','gem')
                 GROUP BY model""", (TARGET_MIN, TARGET_MAX))
    for r in c.fetchall():
        print(f"  {r[0]}: short-lead(0-3) rows = {r[1]}")
    c.execute("""SELECT model, CAST(julianday(target_date)-julianday(fetch_date) AS INT) lead, COUNT(*)
                 FROM nwp_forecasts
                 WHERE target_date BETWEEN ? AND ? AND model IN ('gfs','ecmwf','icon','gem')
                 GROUP BY model, lead ORDER BY model, lead""", (TARGET_MIN, TARGET_MAX))
    lead_rows = c.fetchall()
    by_model = {}
    for m, lead, n in lead_rows:
        by_model.setdefault(m, {})[lead] = n
    for m, leads in by_model.items():
        print(f"  {m} lead distribution: {dict(sorted(leads.items()))}")
    c.execute("SELECT COUNT(*) FROM nwp_forecasts")
    final_rows = c.fetchone()[0]
    print(f"Final total rows: {final_rows} (baseline {baseline_rows}, delta {final_rows - baseline_rows})")

    conn.close()
    print(f"\nDone: {datetime.now(timezone.utc).isoformat()}")


if __name__ == "__main__":
    main()