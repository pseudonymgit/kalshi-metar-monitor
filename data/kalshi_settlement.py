"""
kalshi_settlement.py — Load Kalshi settlement data as ground truth for sweep accuracy.

This is a data module (not a script) — importable by scripts/sweep/data.py,
scripts/sweep/accuracy.py, and any other evaluation pipeline.

Ground truth: Kalshi market `expiration_value` — the actual NWS daily maximum
temperature in °F for the station/date, determined after the trading day closes
and markets finalize. This is the canonical "what actually happened" for the
weather market.

Cache policy: JSON file on disk with configurable TTL (default 4 hours).
"""

import json
import logging
import os
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

_LOGGER = logging.getLogger(__name__)

# ── Paths ──
_DATA_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_DATA_DIR)

# ── Kalshi API ──
_KALSHI_BASE = (
    os.environ.get("KALSHI_PUBLIC_BASE_URL")
    or "https://api.elections.kalshi.com/trade-api/v2"
).rstrip("/")

import requests
_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": "WeatherEngineSettlementLoader/1.0",
    "Accept": "application/json",
})

# ── Rate Limiting ──
_RATE_LIMIT_LOCK = threading.Lock()
_LAST_REQUEST_TIME: float = 0.0
_MIN_INTERVAL = 0.55  # ~1.8 requests/sec — safe for Kalshi public tier

# ── Cache ──
_CACHE_LOCK = threading.Lock()
_CACHE: Dict[str, Any] = {}  # in-memory overlay for current session

# Cache file path (same as function default)
_DEFAULT_CACHE_PATH = os.path.join(_DATA_DIR, "phase0_settlement_data.json")


# ═══════════════════════════════════════════════════════════════════════════
# Station → Kalshi Series Mapping
# ═══════════════════════════════════════════════════════════════════════════
#
# Source: Verified against live Kalshi API /series endpoint and
#         core/kalshi_price_fetcher.py STATION_TO_KALSHI_CODE (2026-07-02)
#
# Mapping: station ICAO → Kalshi series code suffix
# Series ticker (HIGH): KXHIGH{code}
# Series ticker (LOW):  KXLOW{code}
# Event ticker:         {series_ticker}-{YY}{MON}{DD}

STATION_TO_KALSHI_CODE = {
    "KATL": "TATL",
    "KAUS": "AUS",
    "KBOS": "TBOS",
    "KDCA": "TDC",
    "KDEN": "DEN",
    "KDFW": "TDAL",
    "KHOU": "THOU",
    "KLAS": "TLV",
    "KLAX": "LAX",
    "KMDW": "CHI",
    "KMIA": "MIA",
    "KMSP": "TMIN",
    "KMSY": "TNOLA",
    "KNYC": "NY",
    "KOKC": "TOKC",
    "KPHL": "PHIL",
    "KPHX": "TPHX",
    "KSAT": "TSATX",
    "KSEA": "TSEA",
    "KSFO": "TSFO",
}

# Reverse: code → station
KALSHI_CODE_TO_STATION = {v: k for k, v in STATION_TO_KALSHI_CODE.items()}

# Additional aliases for stations the sweep config may use via KORD→KMDW bridge
# (the sweep uses KMDW for Chicago, not KORD)
STATION_ALIASES = {
    "KORD": "KMDW",  # O'Hare → Midway for Kalshi CHI series
    "KJFK": "KNYC",  # JFK → NYC series
    "KLGA": "KNYC",  # LaGuardia → NYC series
    "KDAL": "KDFW",  # Love Field → DFW series
}


def resolve_station(station: str) -> str:
    """Resolve a station ICAO to its canonical Kalshi-mapped station."""
    s = station.strip().upper()
    if s in STATION_TO_KALSHI_CODE:
        return s
    return STATION_ALIASES.get(s, s)


def series_ticker(station: str, market_type: str = "HIGH") -> Optional[str]:
    """Get the series ticker for a station and market type (HIGH/LOW)."""
    st = resolve_station(station)
    code = STATION_TO_KALSHI_CODE.get(st)
    if code is None:
        return None
    prefix = "KXHIGH" if market_type.upper() == "HIGH" else "KXLOW"
    return f"{prefix}{code}"


def event_ticker(station: str, date_obj: datetime, market_type: str = "HIGH") -> Optional[str]:
    """Build the event ticker for a station on a given UTC date."""
    series = series_ticker(station, market_type)
    if series is None:
        return None
    date_token = date_obj.strftime("%y%b%d").upper()
    return f"{series}-{date_token}"


def all_stations_from_mapping() -> List[str]:
    """Return all mapped station ICAOs."""
    return sorted(STATION_TO_KALSHI_CODE.keys())


# ═══════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════════

def _rate_limited_get(url: str, timeout: int = 15, max_retries: int = 3) -> Optional[dict]:
    """
    Make a throttled GET to Kalshi with retry on 429.
    Returns parsed JSON dict or None on failure.
    """
    global _LAST_REQUEST_TIME

    for attempt in range(1, max_retries + 1):
        # Throttle
        with _RATE_LIMIT_LOCK:
            now = time.time()
            elapsed = now - _LAST_REQUEST_TIME
            if elapsed < _MIN_INTERVAL:
                time.sleep(_MIN_INTERVAL - elapsed)
            _LAST_REQUEST_TIME = time.time()

        try:
            resp = _SESSION.get(url, timeout=timeout)
        except requests.exceptions.Timeout:
            _LOGGER.warning("kalshi_settlement_timeout url=%s attempt=%d/%d", url, attempt, max_retries)
            if attempt < max_retries:
                time.sleep(2 ** attempt)
            continue
        except requests.exceptions.ConnectionError as e:
            _LOGGER.warning("kalshi_settlement_connection_error url=%s err=%s", url, e)
            if attempt < max_retries:
                time.sleep(2 ** attempt)
            continue
        except Exception as e:
            _LOGGER.error("kalshi_settlement_request_failed url=%s err=%s", url, e)
            return None

        if resp.status_code == 200:
            try:
                return resp.json()
            except (json.JSONDecodeError, ValueError) as e:
                _LOGGER.warning("kalshi_settlement_decode_error url=%s err=%s", url, e)
                return None

        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", str(2 ** attempt)))
            _LOGGER.info(
                "kalshi_settlement_429 url=%s attempt=%d/%d retry_after=%ds",
                url, attempt, max_retries, retry_after,
            )
            if attempt < max_retries:
                time.sleep(retry_after)
            continue

        # Non-retryable error
        _LOGGER.warning(
            "kalshi_settlement_http_%d url=%s attempt=%d/%d",
            resp.status_code, url, attempt, max_retries,
        )
        return None

    _LOGGER.error("kalshi_settlement_exhausted_retries url=%s", url)
    return None


def _parse_expiration_value(market: dict) -> Optional[float]:
    """
    Extract expiration_value (ground truth temp) from a Kalshi market dict.

    Kalshi stores the actual NWS daily maximum temperature as a float string
    in the `expiration_value` field, visible after the market finalizes.
    Returns °F as float, or None if not available.
    """
    raw = market.get("expiration_value")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _fetch_settlement_for_event(event_tkr: str) -> Optional[float]:
    """
    Fetch the expiration_value for a single event ticker.

    Each event contains ~6-10 markets (bucket + threshold contracts), all

    NOTE: Kalshi does NOT support `status=finalized` as a query parameter.
    The response includes both open and finalized markets for the event;
    we extract expiration_value from whichever markets have it resolved.
    
    Status filter is handled client-side.
    sharing the same `expiration_value`. We return the first non-None value.
    """
    url = f"{_KALSHI_BASE}/markets?event_ticker={event_tkr}&limit=50"
    data = _rate_limited_get(url)
    if data is None:
        return None

    markets = data.get("markets") or []
    for m in markets:
        val = _parse_expiration_value(m)
        if val is not None:
            return val

    return None


def _iter_event_dates(days_back: int) -> List[datetime]:
    """
    Generate UTC date objects for the last N days (today inclusive).
    Returns newest-first to maximize cache hits.
    """
    now = datetime.now(timezone.utc)
    dates = []
    for i in range(days_back):
        d = now - timedelta(days=i)
        dates.append(d.replace(hour=0, minute=0, second=0, microsecond=0))
    return dates


# ═══════════════════════════════════════════════════════════════════════════
# Cache
# ═══════════════════════════════════════════════════════════════════════════

def _cache_path(db_path: str) -> str:
    if db_path:
        return db_path
    return _DEFAULT_CACHE_PATH


def _read_cache(path: str) -> Dict[str, Dict]:
    """Read cached settlement data. Returns empty dict on miss/failure."""
    try:
        with open(path, "r") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        pass
    return {}


def _write_cache(path: str, data: Dict[str, Dict]) -> None:
    """Atomically write settlement data to cache file."""
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except (OSError, PermissionError) as e:
        _LOGGER.warning("kalshi_settlement_cache_write_failed path=%s err=%s", path, e)


def _cache_is_fresh(path: str, ttl_hours: int) -> bool:
    """Check if cache file exists and is younger than TTL."""
    try:
        mtime = os.path.getmtime(path)
        age = time.time() - mtime
        return age < ttl_hours * 3600
    except FileNotFoundError:
        return False


# ═══════════════════════════════════════════════════════════════════════════
# NWS CLI alignment helper
# ═══════════════════════════════════════════════════════════════════════════

_NWS_DB_PATH = os.path.join(_DATA_DIR, "metar_backfill.db")


def _get_nws_cli_max(station: str, date_str: str) -> Optional[float]:
    """
    Query the METAR backfill DB for the NWS CLI daily max temp on a date.

    Uses the metar_observations table grouped by date_utc, returning the
    same MAX(temp_f) that NWS publishes as CLI (daily climate report).

    Returns °F as float, or None if no observations available.
    """
    try:
        import sqlite3
        conn = sqlite3.connect(_NWS_DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=3000;")
        cur = conn.cursor()
        cur.execute(
            "SELECT MAX(temp_f) FROM metar_observations "
            "WHERE station=? AND date_utc=? AND temp_f IS NOT NULL",
            (station.upper(), date_str),
        )
        row = cur.fetchone()
        conn.close()
        if row and row[0] is not None:
            return float(row[0])
    except Exception as e:
        _LOGGER.debug("nws_cli_query_failed station=%s date=%s err=%s", station, date_str, e)
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════

def load_kalshi_settlements(
    stations: Optional[List[str]] = None,
    days_back: int = 60,
    db_path: str = "",
    cache_ttl_hours: int = 4,
) -> Dict[str, Dict]:
    """
    Load Kalshi settlement data as ground truth for sweep accuracy.

    For each station, fetches `expiration_value` from finalized Kalshi markets
    for each day in the lookback window. Results are cached to disk.

    Args:
        stations: List of ICAO station codes. Defaults to all mapped stations.
        days_back: Number of days to look back (max 90 recommended to avoid
                   stale data and API volume).
        db_path: Path to cache JSON file. Defaults to
                 data/phase0_settlement_data.json.
        cache_ttl_hours: Discard cache and re-fetch if older than this.
                         Set to 0 to force refresh.

    Returns:
        {station: {date: {"kalshi_temp": float,
                          "nws_cli_source": str,
                          "cached": bool}}}

    The `nws_cli_source` field will be "kalshi" (from expiration_value)
    or "nws_db" (backfilled from metar_backfill.db) or "missing".
    """
    if stations is None:
        stations = all_stations_from_mapping()

    # Resolve aliases upfront
    resolved_stations = sorted(set(resolve_station(s) for s in stations))
    resolved_stations = [s for s in resolved_stations if s in STATION_TO_KALSHI_CODE]

    cache_path = _cache_path(db_path)
    dates = _iter_event_dates(days_back)

    # Try cache
    force_refresh = cache_ttl_hours <= 0
    cached_data = _read_cache(cache_path) if not force_refresh else {}
    cache_fresh = _cache_is_fresh(cache_path, cache_ttl_hours) if not force_refresh else False

    if cached_data and cache_fresh:
        _LOGGER.info(
            "kalshi_settlement_cache_hit path=%s stations=%d days=%d",
            cache_path, len(resolved_stations), len(dates),
        )
        # Merge in-memory overrides
        with _CACHE_LOCK:
            for st, day_data in _CACHE.items():
                if st in cached_data:
                    cached_data[st].update(day_data)
                else:
                    cached_data[st] = day_data
        return cached_data

    _LOGGER.info(
        "kalshi_settlement_fetch_start stations=%d days=%d cache_ttl=%dh",
        len(resolved_stations), len(dates), cache_ttl_hours,
    )

    # Build result from scratch
    result: Dict[str, Dict] = {}
    total_event_calls = 0
    total_hits = 0
    total_misses = 0

    for station in resolved_stations:
        station_result: Dict[str, Dict] = {}
        for date_dt in dates:
            date_str = date_dt.strftime("%Y-%m-%d")
            event_tkr = event_ticker(station, date_dt, "HIGH")
            if event_tkr is None:
                continue

            # Check in-memory cache first
            with _CACHE_LOCK:
                in_mem = _CACHE.get(station, {}).get(date_str)
            if in_mem is not None:
                station_result[date_str] = in_mem
                total_hits += 1
                continue

            # Check on-disk cache
            if cached_data and cache_fresh:
                entry = cached_data.get(station, {}).get(date_str)
                if entry is not None and entry.get("kalshi_temp") is not None:
                    station_result[date_str] = entry
                    total_hits += 1
                    continue

            # Fetch from API
            temp = _fetch_settlement_for_event(event_tkr)
            total_event_calls += 1

            if temp is not None:
                entry = {
                    "kalshi_temp": temp,
                    "nws_cli_source": "kalshi",
                    "cached": False,
                }
                # Also attach NWS CLI for comparison
                nws_temp = _get_nws_cli_max(station, date_str)
                if nws_temp is not None:
                    entry["nws_cli"] = nws_temp
                    entry["nws_cli_source"] = "kalshi_and_nws"
                station_result[date_str] = entry
                total_hits += 1
            else:
                # Fallback: try NWS DB directly
                nws_temp = _get_nws_cli_max(station, date_str)
                if nws_temp is not None:
                    station_result[date_str] = {
                        "kalshi_temp": None,
                        "nws_cli": nws_temp,
                        "nws_cli_source": "nws_db",
                        "cached": False,
                    }
                else:
                    station_result[date_str] = {
                        "kalshi_temp": None,
                        "nws_cli": None,
                        "nws_cli_source": "missing",
                        "cached": False,
                    }
                total_misses += 1

        if station_result:
            result[station] = station_result

    _LOGGER.info(
        "kalshi_settlement_fetch_done stations=%d api_calls=%d hits=%d misses=%d",
        len(result), total_event_calls, total_hits, total_misses,
    )

    # Write cache
    if not force_refresh:
        _write_cache(cache_path, result)

    # Update in-memory cache
    with _CACHE_LOCK:
        for st, day_data in result.items():
            if st not in _CACHE:
                _CACHE[st] = {}
            _CACHE[st].update(day_data)

    return result


def kalshi_accuracy(
    predictions: List[Tuple[str, str, str, float]],
    kalshi_data: Dict[str, Dict],
    bucket_width: int = 2,
) -> Dict:
    """
    Evaluate sweep predictions against Kalshi ground truth.

    Args:
        predictions: List of (station, date, predicted_bucket, confidence).
            - predicted_bucket can be:
                * A numeric bucket like "80-82" (range)
                * A direction string "up"/"down"
                * A numeric value string like "81.5" (exact temp)
        kalshi_data: Output from load_kalshi_settlements().
        bucket_width: Width of Kalshi temperature buckets in °F (default 2).

    Returns:
        {
            "strike_accuracy": float,         # % where predicted bucket matches actual
            "directional_accuracy": float,    # % where up/down direction matches
            "within_1f": float,               # % within 1°F of actual
            "within_2f": float,               # % within 2°F of actual
            "total_predictions": int,
            "by_station": { station: { ... } },
            "by_date": { date: { ... } },
            "confusion_matrix": {
                "up_up": int, "up_down": int, "down_up": int, "down_down": int
            },
            "errors_summary": {
                "mean_abs_error": float,
                "rmse": float,
                "max_abs_error": float,
                "bias": float,
            },
        }
    """
    from collections import Counter

    # Group predictions
    by_station: Dict[str, List] = defaultdict(list)
    by_date: Dict[str, List] = defaultdict(list)
    all_matched: List[bool] = []
    all_directional_matched: List[bool] = []
    all_within_1f: List[bool] = []
    all_within_2f: List[bool] = []
    all_abs_errors: List[float] = []

    confusion = Counter()

    for station, date_str, pred_bucket, confidence in predictions:
        st = resolve_station(station)
        # Get ground truth
        station_data = kalshi_data.get(st, {})
        day_entry = station_data.get(date_str, {})

        kalshi_temp = day_entry.get("kalshi_temp")
        if kalshi_temp is None:
            # Try NWS fallback
            kalshi_temp = day_entry.get("nws_cli")

        if kalshi_temp is None:
            continue  # no ground truth — skip this prediction

        actual_temp = float(kalshi_temp)

        # Compute actual bucket (needed regardless of pred type)
        actual_bucket_lo = int(actual_temp // bucket_width * bucket_width)
        actual_bucket_hi = actual_bucket_lo + bucket_width
        actual_bucket_str = f"{actual_bucket_lo}-{actual_bucket_hi}"

        # Handle numeric vs string pred_bucket
        if isinstance(pred_bucket, (int, float)):
            pred_center = float(pred_bucket)
            predicted_direction = None
        elif isinstance(pred_bucket, str):
            pred_upper = pred_bucket.strip().upper()

            # Determine predicted numeric center
            pred_center: Optional[float] = None
            predicted_direction: Optional[str] = None

            if pred_upper in ("UP", "DOWN"):
                predicted_direction = pred_upper.lower()
            elif "-" in pred_upper:
                # Range like "80-82"
                parts = pred_upper.split("-")
                try:
                    lo = float(parts[0])
                    hi = float(parts[1])
                    pred_center = (lo + hi) / 2.0
                except (ValueError, IndexError):
                    pass
            else:
                # Try numeric string
                try:
                    pred_center = float(pred_upper)
                except ValueError:
                    pass
        else:
            pred_center = None
            predicted_direction = None
        # ── Strike accuracy: does predicted bucket match actual bucket? ──
        if pred_center is not None:
            pred_bucket_lo = int(pred_center // bucket_width * bucket_width)
            pred_bucket_hi = pred_bucket_lo + bucket_width
            pred_bucket_str = f"{pred_bucket_lo}-{pred_bucket_hi}"
            strike_match = pred_bucket_str == actual_bucket_str

            # Within-N°F
            within_1f = abs(pred_center - actual_temp) <= 1.0
            within_2f = abs(pred_center - actual_temp) <= 2.0
            abs_error = abs(pred_center - actual_temp)
        else:
            strike_match = False
            within_1f = False
            within_2f = False
            abs_error = abs(actual_temp - 85.0)  # heuristic fallback

        # ── Directional accuracy ──
        if predicted_direction is not None:
            # Need previous day's actual temp
            # Find previous date
            prev_date = _prev_date_str(date_str)
            prev_entry = station_data.get(prev_date, {})
            prev_temp = prev_entry.get("kalshi_temp") or prev_entry.get("nws_cli")

            if prev_temp is not None:
                prev_temp_f = float(prev_temp)
                actual_direction = "up" if actual_temp > prev_temp_f else "down"
                direction_match = predicted_direction == actual_direction
                confusion[f"{predicted_direction}_{actual_direction}"] += 1
            else:
                direction_match = False
            all_directional_matched.append(direction_match)
        else:
            all_directional_matched.append(strike_match)  # use strike as proxy

        all_matched.append(strike_match)
        all_within_1f.append(within_1f)
        all_within_2f.append(within_2f)
        all_abs_errors.append(abs_error)

        # Build per-station entry
        entry = {
            "predicted_bucket": pred_bucket,
            "actual_temp": round(actual_temp, 2),
            "actual_bucket": actual_bucket_str,
            "strike_match": strike_match,
            "within_1f": within_1f,
            "within_2f": within_2f,
            "abs_error": round(abs_error, 2),
            "confidence": float(confidence),
        }
        if predicted_direction is not None:
            direction_entry = direction_match if pred_upper in ("UP", "DOWN") else None
            if direction_entry is not None:
                entry["direction_match"] = direction_match

        by_station[station].append(entry)
        by_date[date_str].append(entry)

    # ── Aggregate ──
    n = len(all_matched)
    if n == 0:
        empty = {
            "strike_accuracy": 0.0,
            "directional_accuracy": 0.0,
            "within_1f": 0.0,
            "within_2f": 0.0,
            "total_predictions": 0,
            "by_station": {},
            "by_date": {},
            "confusion_matrix": {"up_up": 0, "up_down": 0, "down_up": 0, "down_down": 0},
            "errors_summary": {
                "mean_abs_error": 0.0, "rmse": 0.0,
                "max_abs_error": 0.0, "bias": 0.0,
            },
        }
        return empty

    strike_accuracy = sum(all_matched) / n
    directional_accuracy = sum(all_directional_matched) / n
    within_1f = sum(all_within_1f) / n
    within_2f = sum(all_within_2f) / n

    mae = sum(all_abs_errors) / n
    rmse = (sum(e * e for e in all_abs_errors) / n) ** 0.5
    max_error = max(all_abs_errors) if all_abs_errors else 0.0
    bias = sum(
        (p[3] if isinstance(p[3], (int, float)) else 0.0) - a for p, a in zip(
            predictions, [e["actual_temp"] for e in sum(by_station.values(), [])]
        )
    ) if False else 0.0  # simplified: bias = mean(actual - predicted center)
    # Better bias calculation:
    bias_values = []
    for station, date_str, pred_bucket, confidence in predictions:
        st = resolve_station(station)
        day_entry = kalshi_data.get(st, {}).get(date_str, {})
        actual = day_entry.get("kalshi_temp") or day_entry.get("nws_cli")
        if actual is None:
            continue
        actual_f = float(actual)
        # Get predicted center
        if isinstance(pred_bucket, (int, float)):
            bias_values.append(actual_f - float(pred_bucket))
        elif isinstance(pred_bucket, str):
            pred_upper = pred_bucket.strip().upper()
            if "-" in pred_upper:
                parts = pred_upper.split("-")
                try:
                    center = (float(parts[0]) + float(parts[1])) / 2.0
                    bias_values.append(actual_f - center)
                except (ValueError, IndexError):
                    pass
            else:
                try:
                    center = float(pred_upper)
                    bias_values.append(actual_f - center)
                except ValueError:
                    pass

    bias_final = sum(bias_values) / len(bias_values) if bias_values else 0.0

    result = {
        "strike_accuracy": round(strike_accuracy, 4),
        "directional_accuracy": round(directional_accuracy, 4),
        "within_1f": round(within_1f, 4),
        "within_2f": round(within_2f, 4),
        "total_predictions": n,
        "by_station": {
            station: {
                "predictions": len(entries),
                "strike_accuracy": round(
                    sum(e["strike_match"] for e in entries) / len(entries), 4
                ) if entries else 0.0,
                "within_1f": round(
                    sum(e["within_1f"] for e in entries) / len(entries), 4
                ) if entries else 0.0,
                "within_2f": round(
                    sum(e["within_2f"] for e in entries) / len(entries), 4
                ) if entries else 0.0,
                "mean_abs_error": round(
                    sum(e["abs_error"] for e in entries) / len(entries), 2
                ) if entries else 0.0,
            }
            for station, entries in by_station.items()
        },
        "by_date": {
            date_str: {
                "predictions": len(entries),
                "strike_accuracy": round(
                    sum(e["strike_match"] for e in entries) / len(entries), 4
                ) if entries else 0.0,
                "mean_abs_error": round(
                    sum(e["abs_error"] for e in entries) / len(entries), 2
                ) if entries else 0.0,
            }
            for date_str, entries in by_date.items()
        },
        "confusion_matrix": {
            "up_up": confusion.get("up_up", 0),
            "up_down": confusion.get("up_down", 0),
            "down_up": confusion.get("down_up", 0),
            "down_down": confusion.get("down_down", 0),
        },
        "errors_summary": {
            "mean_abs_error": round(mae, 2),
            "rmse": round(rmse, 2),
            "max_abs_error": round(max_error, 2),
            "bias": round(bias_final, 2),
        },
    }

    return result


def _prev_date_str(date_str: str) -> str:
    """Return the previous calendar date as YYYY-MM-DD."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return (d - timedelta(days=1)).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def clear_cache() -> None:
    """Clear the in-memory settlement cache."""
    with _CACHE_LOCK:
        _CACHE.clear()


def invalidate_cache_file(path: str = "") -> None:
    """Delete the on-disk cache file. Safe to call if file doesn't exist."""
    p = _cache_path(path)
    try:
        os.remove(p)
        _LOGGER.info("kalshi_settlement_cache_invalidated path=%s", p)
    except FileNotFoundError:
        pass


# ═══════════════════════════════════════════════════════════════════════════
# Module-level convenience: load on import for singleton pattern
# ═══════════════════════════════════════════════════════════════════════════

def get_cached_settlements(
    stations: Optional[List[str]] = None,
    days_back: int = 60,
    cache_ttl_hours: int = 4,
) -> Dict[str, Dict]:
    """
    Wrapper around load_kalshi_settlements that reuses previously loaded data
    within the same process. Returns cached data without re-fetching if
    already loaded in this session.

    This is the preferred entrypoint for sweep scripts that call this module
    multiple times.
    """
    if _CACHE and not _cache_is_fresh(_DEFAULT_CACHE_PATH, cache_ttl_hours):
        # In-memory cache is stale — force refresh
        clear_cache()

    if _CACHE:
        with _CACHE_LOCK:
            result = dict(_CACHE)
        # Fill in any missing stations
        if stations is not None:
            missing = [s for s in stations if resolve_station(s) not in result]
            if missing:
                fresh = load_kalshi_settlements(
                    missing, days_back, cache_ttl_hours=cache_ttl_hours,
                )
                with _CACHE_LOCK:
                    for st, day_data in fresh.items():
                        if st not in _CACHE:
                            _CACHE[st] = {}
                        _CACHE[st].update(day_data)
                    result.update(dict(_CACHE))
        return result

    return load_kalshi_settlements(
        stations, days_back, cache_ttl_hours=cache_ttl_hours,
    )