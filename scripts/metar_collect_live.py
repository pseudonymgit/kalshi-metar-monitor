#!/usr/bin/env python3
"""
METAR Live Collection Engine — v2.0 (2026-07-03)
Standalone script, NO AGENT INVOLVED.

Collects live METAR observations for all 20 Kalshi stations and stores to:
  prototypes/weather-engine-source/data/metar_backfill.db

Designed to run every 30 minutes via cron:
  */30 * * * * cd /path/to/prototypes/weather-engine-source && python3 scripts/metar_collect_live.py >> /var/log/metar_live.log 2>&1

Each run fetches the latest METAR for each station. Multiple observations per
day are stored with distinct timestamps. The script also updates the
`daily_stats` and `settlement_epochs` tables when new data arrives.

⚠️  STANDALONE SCRIPT — DO NOT RUN VIA AI/AGENT.
    Run manually:  python3 scripts/metar_collect_live.py
    Or via host cron (see above).

Version History:
  v1.0  2026-07-03  Initial version (daily collection)
  v2.0  2026-07-03  Every-30-min collection, 6h aggregates, settlement support, robustness
"""

import urllib.request
import urllib.error
import sqlite3
import json
import time
import re
import sys
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = str(REPO_ROOT / "data" / "metar_backfill.db")

# All 20 Kalshi stations — VERIFIED 2026-07-02
STATIONS = [
    "KATL", "KAUS", "KBOS", "KDCA", "KDEN",
    "KDFW", "KHOU", "KLAS", "KLAX", "KMDW",
    "KMIA", "KMSP", "KMSY", "KNYC", "KOKC",
    "KPHL", "KPHX", "KSAT", "KSEA", "KSFO"
]

SCRIPT_VERSION = "v2.0"


# ─── METAR Parsing ───────────────────────────────────────────────────────

def fetch_raw_metar(icao):
    """Fetch raw METAR text from NOAA AWC."""
    url = f"https://tgftp.nws.noaa.gov/data/observations/metar/stations/{icao}.TXT"
    req = urllib.request.Request(url, headers={
        'User-Agent': f'OpenClaw Weather Engine {SCRIPT_VERSION}',
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode('utf-8').strip()
        return text
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None  # Station not reporting right now — normal
        raise
    except Exception as e:
        print(f"  fetch error {icao}: {e}")
        return None


def parse_metar(raw_text):
    """
    Parse a raw METAR string into structured fields.

    Extracts: temp_f, dewpoint_f, wind_dir, wind_spd_kt, pressure_mb,
              visibility_mi, ceiling_ft, raw_metar
    """
    if not raw_text or len(raw_text.strip()) < 10:
        return None

    lines = raw_text.strip().split('\n')
    # The METAR is usually the last non-empty line
    metar_line = None
    metar_time = None

    for line in lines:
        line = line.strip()
        if not line:
            continue
        # First line may be a date like "2026/07/03 18:55"
        if re.match(r'^\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}$', line):
            try:
                metar_time = datetime.strptime(line, "%Y/%m/%d %H:%M").replace(tzinfo=timezone.utc)
            except ValueError:
                pass
            continue
        # METAR line: starts with station code
        if line.startswith('METAR') or re.match(r'^[A-Z]{4}\s', line):
            metar_line = line
            break

    if not metar_line:
        # Fallback: use last line
        metar_line = lines[-1].strip()
        if not re.match(r'^[A-Z]{4}', metar_line):
            return None

    tokens = metar_line.split()
    temp_f = None
    dewpoint_f = None
    wind_dir = None
    wind_spd_kt = None
    wind_gust_kt = None
    press_mb = None
    visibility_mi = None
    ceiling_ft = None

    for token in tokens[1:]:  # Skip station id
        if token == 'METAR' or token == 'AUTO':
            continue
        if token.endswith('Z') and re.match(r'^\d{6}Z$', token):
            continue  # Observation timestamp

        # Temperature/Dewpoint: 25/18, M03/M05, 25////
        if '/' in token and len(token) >= 3:
            parts = token.split('/')
            if len(parts) == 2:
                temp_c = _parse_temp_c(parts[0])
                dew_c = _parse_temp_c(parts[1])
                if temp_c is not None:
                    temp_f = temp_c * 9/5 + 32
                if dew_c is not None:
                    dewpoint_f = dew_c * 9/5 + 32
                if temp_c is not None or dew_c is not None:
                    continue

        # Wind: 18010KT, VRB05KT, 18010G20KT
        if token.endswith('KT'):
            wind_data = token.rstrip('KT')
            gust_match = re.search(r'G(\d+)', wind_data)
            if gust_match:
                wind_gust_kt = int(gust_match.group(1))
                wind_data = re.sub(r'G\d+', '', wind_data)
            if wind_data.startswith('VRB'):
                wind_dir = None  # Variable
                try:
                    wind_spd_kt = int(wind_data[3:])
                except ValueError:
                    pass
            elif len(wind_data) >= 5:
                try:
                    wind_dir = int(wind_data[:3])
                    wind_spd_kt = int(wind_data[3:])
                except ValueError:
                    pass
            continue

        # Pressure: Q1023 (mb) or A2992 (inHg)
        if token.startswith('Q') and len(token) >= 4:
            try:
                press_mb = float(token[1:])
                continue
            except ValueError:
                pass
        if token.startswith('A') and len(token) == 5:
            try:
                inhg = float(token[1:]) / 100  # A2992 → 29.92
                press_mb = inhg * 33.8639
                continue
            except ValueError:
                pass

        # Visibility: 10SM, 3/4SM, 1 1/2SM
        if token.endswith('SM'):
            vis_str = token.rstrip('SM')
            try:
                if ' ' in vis_str and '/' in vis_str:
                    whole, frac = vis_str.split(' ')
                    num, den = frac.split('/')
                    visibility_mi = float(whole) + float(num) / float(den)
                elif '/' in vis_str:
                    num, den = vis_str.split('/')
                    visibility_mi = float(num) / float(den)
                else:
                    visibility_mi = float(vis_str)
                continue
            except ValueError:
                pass

        # Ceiling: BKN020, OVC008, FEW010, SCT015
        if re.match(r'^(BKN|OVC|FEW|SCT)\d{3}$', token):
            try:
                ceiling_ft = int(token[3:]) * 100
                continue
            except ValueError:
                pass

    # Determine observation time
    if metar_time is None:
        metar_time = datetime.now(timezone.utc)

    return {
        'temp_f': temp_f,
        'temp_c': (temp_f - 32) * 5/9 if temp_f is not None else None,
        'dewpoint_f': dewpoint_f,
        'dewpoint_c': (dewpoint_f - 32) * 5/9 if dewpoint_f is not None else None,
        'wind_direction_deg': wind_dir,
        'wind_speed_kt': wind_spd_kt,
        'wind_gust_kt': wind_gust_kt,
        'pressure_mb': press_mb,
        'visibility_mi': visibility_mi,
        'ceiling_ft': ceiling_ft,
        'raw_metar': metar_line,
        'obs_datetime_utc': metar_time,
    }


def _parse_temp_c(s):
    """Parse temperature string like '25' or 'M03' into Celsius float."""
    if not s or s == '//' or s == '///':
        return None
    try:
        if s.startswith('M'):
            return float('-' + s[1:])
        return float(s)
    except ValueError:
        return None


# ─── Database Operations ──────────────────────────────────────────────────

def init_db(db_path):
    """Create database schema if needed."""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    # metar_observations table
    c.execute("""SELECT name FROM sqlite_master WHERE type='table' AND name='metar_observations'""")
    if not c.fetchone():
        c.execute("""
            CREATE TABLE metar_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                station TEXT NOT NULL,
                date_utc TEXT NOT NULL,
                timestamp_utc TEXT NOT NULL,
                temp_f REAL,
                temp_c REAL,
                dewpoint_f REAL,
                dewpoint_c REAL,
                wind_direction_deg INTEGER,
                wind_speed_kt REAL,
                wind_gust_kt REAL,
                pressure_mb REAL,
                sea_level_pressure_mb REAL,
                visibility_mi REAL,
                ceiling_ft INTEGER,
                raw_metar TEXT,
                source TEXT,
                ingestion_timestamp_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(station, timestamp_utc)
            )
        """)
        c.execute("CREATE INDEX idx_metar_station_date ON metar_observations(station, date_utc)")
        c.execute("CREATE INDEX idx_metar_station_ts ON metar_observations(station, timestamp_utc)")

    # six_hour_aggregates table
    c.execute("""SELECT name FROM sqlite_master WHERE type='table' AND name='six_hour_aggregates'""")
    if not c.fetchone():
        c.execute("""
            CREATE TABLE six_hour_aggregates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                station TEXT NOT NULL,
                date_utc TEXT NOT NULL,
                period_start_utc TEXT NOT NULL,
                period_end_utc TEXT NOT NULL,
                temp_max_f REAL,
                temp_min_f REAL,
                temp_avg_f REAL,
                temp_range_f REAL,
                obs_count INTEGER,
                pressure_avg_mb REAL,
                wind_avg_kt REAL,
                UNIQUE(station, period_start_utc, period_end_utc)
            )
        """)
        c.execute("CREATE INDEX idx_6h_station_date ON six_hour_aggregates(station, date_utc)")

    # daily_stats table (for settlement calculation)
    c.execute("""SELECT name FROM sqlite_master WHERE type='table' AND name='daily_stats'""")
    if not c.fetchone():
        c.execute("""
            CREATE TABLE daily_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                station TEXT NOT NULL,
                date_utc TEXT NOT NULL UNIQUE,
                max_temp_f REAL,
                min_temp_f REAL,
                avg_temp_f REAL,
                temp_range_f REAL,
                obs_count INTEGER,
                last_updated_utc TEXT NOT NULL,
                UNIQUE(station, date_utc)
            )
        """)
        c.execute("CREATE INDEX idx_daily_station ON daily_stats(station, date_utc)")

    conn.commit()
    conn.close()


def save_observation(db_path, station, parsed):
    """Save a single observation. Returns True on success."""
    if not parsed or parsed.get('temp_f') is None:
        return False

    obs_time = parsed['obs_datetime_utc']
    date_str = obs_time.strftime('%Y-%m-%d')
    ts_str = obs_time.isoformat()

    conn = sqlite3.connect(db_path, timeout=10)
    try:
        c = conn.cursor()
        c.execute("""
            INSERT OR REPLACE INTO metar_observations
            (station, date_utc, timestamp_utc, temp_f, temp_c, dewpoint_f, dewpoint_c,
             wind_direction_deg, wind_speed_kt, wind_gust_kt, pressure_mb,
             sea_level_pressure_mb, visibility_mi, ceiling_ft, raw_metar, source,
             ingestion_timestamp_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            station, date_str, ts_str,
            parsed['temp_f'], parsed['temp_c'],
            parsed['dewpoint_f'], parsed['dewpoint_c'],
            parsed['wind_direction_deg'], parsed['wind_speed_kt'],
            parsed.get('wind_gust_kt'),
            parsed['pressure_mb'], None,  # sea_level_pressure_mb not parsed
            parsed['visibility_mi'], parsed['ceiling_ft'],
            parsed['raw_metar'], 'awc_tgftp',
            datetime.now(timezone.utc).isoformat()
        ))
        conn.commit()
        return True
    except sqlite3.Error as e:
        print(f"  DB error ({station}): {e}")
        return False
    finally:
        conn.close()


def update_daily_stats(db_path, station, date_str):
    """Recompute daily_stats for a station/date from raw observations."""
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        c = conn.cursor()
        daily_cols = {row[1] for row in c.execute("PRAGMA table_info(daily_stats)").fetchall()}
        obs_cols = {row[1] for row in c.execute("PRAGMA table_info(metar_observations)").fetchall()}
        obs_ts_col = 'obs_datetime_utc' if 'obs_datetime_utc' in obs_cols else 'timestamp_utc'
        c.execute("""
            SELECT MAX(temp_f), MIN(temp_f), AVG(temp_f), COUNT(*)
            FROM metar_observations
            WHERE station = ? AND date_utc = ? AND temp_f IS NOT NULL
        """, (station, date_str))
        row = c.fetchone()
        if row and row[0] is not None:
            max_t, min_t, avg_t, count = row
            temp_range = max_t - min_t if max_t is not None and min_t is not None else None
            now = datetime.now(timezone.utc).isoformat()
            if {'date_local', 'observation_count', 'ingestion_timestamp_utc'}.issubset(daily_cols):
                # Existing backfill schema.
                c.execute(f"""
                    INSERT OR REPLACE INTO daily_stats
                    (station, date_local, date_utc, max_temp_f, min_temp_f, avg_temp_f,
                     observation_count, first_observation_utc, last_observation_utc,
                     ingestion_timestamp_utc)
                    SELECT ?, ?, ?, ?, ?, ?, ?, MIN({obs_ts_col}), MAX({obs_ts_col}), ?
                    FROM metar_observations
                    WHERE station = ? AND date_utc = ? AND temp_f IS NOT NULL
                """, (station, date_str, date_str, max_t, min_t, avg_t, count, now,
                      station, date_str))
            else:
                # v2 standalone schema for freshly-created DBs.
                c.execute("""
                    INSERT OR REPLACE INTO daily_stats
                    (station, date_utc, max_temp_f, min_temp_f, avg_temp_f, temp_range_f,
                     obs_count, last_updated_utc)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (station, date_str, max_t, min_t, avg_t, temp_range, count, now))
            conn.commit()
    except sqlite3.Error as e:
        print(f"  daily_stats error ({station}/{date_str}): {e}")
    finally:
        conn.close()


def update_six_hour_aggregates(db_path, station, date_str):
    """Compute or refresh 6-hour aggregates for a station/date."""
    periods = [
        ('00:00', '06:00', 0, 6),
        ('06:00', '12:00', 6, 12),
        ('12:00', '18:00', 12, 18),
        ('18:00', '24:00', 18, 24),
    ]

    conn = sqlite3.connect(db_path, timeout=10)
    try:
        c = conn.cursor()
        for label_start, label_end, h_start, h_end in periods:
            period_start = f"{date_str}T{label_start}:00+00:00"
            period_end = f"{date_str}T{label_end}:00+00:00"

            c.execute("""
                SELECT temp_f, pressure_mb, wind_speed_kt
                FROM metar_observations
                WHERE station = ? AND date_utc = ?
                AND CAST(strftime('%H', timestamp_utc) AS INTEGER) >= ?
                AND CAST(strftime('%H', timestamp_utc) AS INTEGER) < ?
                AND temp_f IS NOT NULL
            """, (station, date_str, h_start, h_end))

            rows = c.fetchall()
            if not rows:
                continue

            temps = [r[0] for r in rows if r[0] is not None]
            pressures = [r[1] for r in rows if r[1] is not None]
            winds = [r[2] for r in rows if r[2] is not None]

            if not temps:
                continue

            c.execute("""
                INSERT OR REPLACE INTO six_hour_aggregates
                (station, date_utc, period_start_utc, period_end_utc,
                 temp_max_f, temp_min_f, temp_avg_f, temp_range_f,
                 obs_count, pressure_avg_mb, wind_avg_kt)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                station, date_str, period_start, period_end,
                max(temps), min(temps), sum(temps)/len(temps),
                max(temps) - min(temps),
                len(temps),
                sum(pressures)/len(pressures) if pressures else None,
                sum(winds)/len(winds) if winds else None,
            ))
        conn.commit()
    except sqlite3.Error as e:
        print(f"  6h aggregate error ({station}/{date_str}): {e}")
    finally:
        conn.close()


# ─── Main Collection Loop ─────────────────────────────────────────────────

def main():
    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.strftime('%Y-%m-%d')

    print(f"METAR Live Collector {SCRIPT_VERSION} — {now_utc.isoformat()}")
    print(f"Database: {DB_PATH}")
    print(f"Stations: {len(STATIONS)}")
    print()

    init_db(DB_PATH)

    successful = 0
    failed = 0
    skipped = 0
    errors = []

    print("Fetching METAR observations...")
    for i, station in enumerate(STATIONS):
        print(f"  [{i+1:2d}/{len(STATIONS)}] {station}...", end=" ", flush=True)

        raw = fetch_raw_metar(station)
        if raw is None:
            print("NO DATA (404)")
            skipped += 1
            continue

        parsed = parse_metar(raw)
        if not parsed:
            print("PARSE FAIL")
            failed += 1
            errors.append(f"{station}: parse failure")
            continue

        if parsed.get('temp_f') is None:
            print("NO TEMP")
            skipped += 1
            continue

        ok = save_observation(DB_PATH, station, parsed)
        if ok:
            print(f"OK ({parsed['temp_f']:.1f}°F)")
            successful += 1
            # Update derived tables
            date_str = parsed['obs_datetime_utc'].strftime('%Y-%m-%d')
            update_daily_stats(DB_PATH, station, date_str)
            update_six_hour_aggregates(DB_PATH, station, date_str)
        else:
            print("DB SAVE FAIL")
            failed += 1
            errors.append(f"{station}: DB save failure")

        time.sleep(0.15)  # Rate-limit courtesy

    print()
    print("=" * 55)
    print("COLLECTION REPORT")
    print(f"  Timestamp:  {now_utc.isoformat()}")
    print(f"  Total:     {len(STATIONS)}")
    print(f"  Success:   {successful}")
    print(f"  Skipped:   {skipped} (no data / no temp)")
    print(f"  Failed:    {failed}")
    print(f"  Rate:      {successful/len(STATIONS)*100:.1f}%")

    if errors:
        print(f"\n  Errors ({len(errors)}):")
        for err in errors[:5]:
            print(f"    • {err}")

    # DB summary
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM metar_observations WHERE date_utc = ?", (today_str,))
        today_count = c.fetchone()[0]
        c.execute("SELECT MIN(date_utc), MAX(date_utc), COUNT(*) FROM metar_observations")
        r = c.fetchone()
        c.execute("SELECT COUNT(DISTINCT station) FROM metar_observations WHERE date_utc = ?", (today_str,))
        today_stations = c.fetchone()[0]
        conn.close()

        print(f"\nDatabase Summary:")
        print(f"  Today's observations: {today_count} ({today_stations} stations)")
        print(f"  Total date range:     {r[0]} → {r[1]}")
        print(f"  Total observations:   {r[2]}")
    except Exception as e:
        print(f"  DB summary error: {e}")

    print(f"\nDone at {datetime.now(timezone.utc).isoformat()}")
    return 0 if successful > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
