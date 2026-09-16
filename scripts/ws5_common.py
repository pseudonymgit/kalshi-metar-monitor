#!/usr/bin/env python3
"""
ws5_common.py — Shared utility module for WS5 sweep scripts.

Provides correct, tested implementations of:
- Settlement data loading from kalshi_settlements table (with deduplication)
- Ground truth direction computation from kalshi_temp
- METAR daily aggregate loading from daily_stats table
- Signal evaluation loop against ground truth
- Fusion accuracy computation with real signal predictions
- Standardized accuracy and P&L metrics

All 12 WS5 sweep scripts should import from this module instead of
duplicating broken data-loading logic.
"""

import bisect
import logging
import sqlite3
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

# Module-level STATIONS alias: re-export from canonical source (immutable tuple)
from scripts.sweep.config import STATIONS

logger = logging.getLogger(__name__)

# ── Source type priority for deduplication ──
SOURCE_PRIORITY = {
    "finalized": 0,
    "historical_api": 1,
    "live_api": 2,
}

CALIBRATION_CUTOFF_MAX = "2026-02-28"


def _market_direction(series: str) -> Optional[str]:
    """Return the explicit Kalshi event direction, or None if unknown."""
    value = (series or "").upper()
    if "HIGH" in value and "LOW" not in value:
        return "HIGH"
    if "LOW" in value and "HIGH" not in value:
        return "LOW"
    return None


def load_historical_calibration_labels(
    kalshi_db_path: str,
    cutoff_date: str = CALIBRATION_CUTOFF_MAX,
    holdout_start: Optional[str] = None,
    holdout_end: Optional[str] = None,
    return_audit: bool = False,
):
    """Load immutable, pre-cutoff Kalshi labels for calibration research.

    Unlike :func:`load_settlement_data`, this path never collapses HIGH and
    LOW events into ``station/date``.  Labels are keyed by
    ``(station, target_date, market_direction)`` and are derived from two
    consecutive settled events of the *same* direction.  Conflicting rows or
    multiple event tickers for one identity are excluded rather than chosen
    by insertion order.

    This function is intentionally separate from the ordinary scoring loader;
    callers must explicitly pass its result to the evaluator.
    """
    import os
    if not os.path.exists(kalshi_db_path):
        raise FileNotFoundError(f"Kalshi DB not found: {kalshi_db_path}")
    if cutoff_date > CALIBRATION_CUTOFF_MAX:
        raise ValueError(
            f"calibration cutoff {cutoff_date} exceeds safety maximum "
            f"{CALIBRATION_CUTOFF_MAX}"
        )

    conn = sqlite3.connect(kalshi_db_path)
    try:
        rows = conn.execute(
            """SELECT station, series, event_ticker, target_date, kalshi_temp,
                      source_type
                 FROM kalshi_settlements
                WHERE target_date <= ? AND kalshi_temp IS NOT NULL
                ORDER BY station, series, target_date, event_ticker, id""",
            (cutoff_date,),
        ).fetchall()
    finally:
        conn.close()

    # First preserve event identity.  Exact repeated copies are harmless;
    # conflicting values or multiple tickers are ambiguous and excluded.
    by_identity = defaultdict(list)
    for station, series, ticker, target_date, temp, source_type in rows:
        direction = _market_direction(series)
        if direction is None:
            continue
        by_identity[(station, target_date, direction)].append(
            (str(ticker), float(temp), source_type)
        )

    values = {}
    ambiguous = []
    exact_duplicates = 0
    for key, entries in by_identity.items():
        unique_pairs = {(ticker, temp) for ticker, temp, _ in entries}
        tickers = {ticker for ticker, _, _ in entries}
        temps = {temp for _, temp, _ in entries}
        if len(tickers) != 1 or len(temps) != 1:
            ambiguous.append(key)
            continue
        values[key] = next(iter(temps))
        exact_duplicates += max(0, len(entries) - 1)

    labels = []
    by_event = defaultdict(list)
    for (station, target_date, direction), temp in values.items():
        if holdout_start and target_date >= holdout_start and (not holdout_end or target_date <= holdout_end):
            continue
        if holdout_end and target_date <= holdout_end and (not holdout_start or target_date >= holdout_start):
            continue
        by_event[(station, direction)].append((target_date, temp))

    for (station, direction), event_rows in by_event.items():
        event_rows.sort()
        for (target_date, temp), (previous_date, previous_temp) in zip(
            event_rows[1:], event_rows[:-1]
        ):
            if target_date <= previous_date:
                continue
            delta = temp - previous_temp
            actual_direction = None if abs(delta) < 0.5 else ("up" if delta > 0 else "down")
            if actual_direction is None:
                continue
            labels.append({
                "station": station,
                "target_date": target_date,
                "market_direction": direction,
                "event_direction": actual_direction,
                "previous_target_date": previous_date,
                "target_value": temp,
                "previous_value": previous_temp,
            })

    labels.sort(key=lambda x: (x["station"], x["target_date"], x["market_direction"]))
    audit = {
        "cutoff_date": cutoff_date,
        "rows_read": len(rows),
        "unique_event_keys": len(by_identity),
        "valid_event_keys": len(values),
        "ambiguous_event_keys": len(ambiguous),
        "exact_duplicate_rows": exact_duplicates,
        "labels_emitted": len(labels),
        "ambiguous_keys": [list(k) for k in sorted(ambiguous)],
        "holdout_excluded": bool(holdout_start or holdout_end),
    }
    return (labels, audit) if return_audit else labels


# ═══════════════════════════════════════════════════════════════════
# 1. Settlement Data Loading
# ═══════════════════════════════════════════════════════════════════

def deduplicate_settlements(conn: sqlite3.Connection) -> Dict[str, Dict[str, float]]:
    """
    Load and deduplicate settlement data from kalshi_settlements table.

    Priority: finalized > historical_api > live_api.
    Within same source_type: keep the last (most recent) row.

    Returns {station: {target_date: kalshi_temp}}.
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT station, target_date, kalshi_temp, source_type
        FROM kalshi_settlements
        WHERE kalshi_temp IS NOT NULL
        ORDER BY station, target_date, source_type
    """)
    rows = cur.fetchall()

    settlements: Dict[str, Dict[str, float]] = {}
    for station, target_date, kalshi_temp, source_type in rows:
        if station not in settlements:
            settlements[station] = {}
        key = (station, target_date)
        existing = settlements[station].get(target_date)
        if existing is not None:
            # Already have a value — apply priority
            existing_priority = SOURCE_PRIORITY.get(source_type, 99)
            # Since ORDER BY includes source_type, later rows may have
            # different source_type. We keep the one with higher priority
            # (lower number). If same priority, keep the later one.
            # We need to track the source_type of the existing value.
            # For simplicity: re-query or track in a parallel dict.
            # Since ORDER BY is station, target_date, source_type,
            # and source_type values are 'finalized' < 'historical_api' < 'live_api'
            # alphabetically, 'finalized' comes first. So the first row for a
            # (station, date) is the highest priority. We keep the first.
            continue
        settlements[station][target_date] = kalshi_temp

    return settlements


def load_settlement_data(kalshi_db_path: str) -> Dict[str, Dict[str, float]]:
    """
    Load Kalshi settlement temperatures, deduplicated.

    Returns {station: {target_date: kalshi_temp}}.

    Deduplication priority: finalized > historical_api > live_api.
    Within same source_type: keep newest source.
    Excludes rows where kalshi_temp IS NULL.

    Raises FileNotFoundError if DB path doesn't exist.
    """
    import os
    if not os.path.exists(kalshi_db_path):
        raise FileNotFoundError(f"Kalshi DB not found: {kalshi_db_path}")

    conn = sqlite3.connect(kalshi_db_path)
    try:
        return deduplicate_settlements(conn)
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════
# 2. Ground Truth Direction
# ═══════════════════════════════════════════════════════════════════

def compute_ground_truth_direction(
    temp_today: float,
    temp_yesterday: float,
    epsilon: float = 0.5,
) -> Optional[str]:
    """
    Compute actual temperature direction from settlement data.

    Returns 'up', 'down', or None (flat — |diff| < epsilon).

    The 0.5°F epsilon prevents noise from being counted as signal.
    Uses kalshi_temp directly, NOT settlement_bucket comparison.
    """
    diff = temp_today - temp_yesterday
    if abs(diff) < epsilon:
        return None  # flat — exclude from accuracy
    return "up" if diff > 0 else "down"


# ═══════════════════════════════════════════════════════════════════
# 3. METAR Daily Aggregate Loading
# ═══════════════════════════════════════════════════════════════════

def load_metar_daily_aggregates(
    metar_db_path: str, station: str
) -> List[Dict[str, Any]]:
    """
    Load daily METAR weather data for a station, sorted by date.

    Returns list of dicts with keys: date, high, low, temp, dewpoint,
    wind_dir, wind_speed, pressure (compatible with signal.evaluate()).

    Queries metar_observations directly with daily aggregation to provide
    all fields that signals need (pressure, wind, dewpoint).
    """
    import os
    if not os.path.exists(metar_db_path):
        raise FileNotFoundError(f"METAR DB not found: {metar_db_path}")

    conn = sqlite3.connect(metar_db_path)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT
                date_utc,
                MAX(temp_f) as high,
                MIN(temp_f) as low,
                AVG(temp_f) as temp,
                AVG(dewpoint_f) as dewpoint,
                AVG(wind_direction_deg) as wind_dir,
                AVG(wind_speed_kt) as wind_speed,
                AVG(pressure_mb) as pressure
            FROM metar_observations
            WHERE station = ? AND temp_f IS NOT NULL
            GROUP BY date_utc
            ORDER BY date_utc ASC
        """, (station,))
        rows = cur.fetchall()
    finally:
        conn.close()

    days = []
    for date_utc, high, low, temp, dewpoint, wind_dir, wind_speed, pressure in rows:
        days.append({
            "date": date_utc,
            "high": high,
            "low": low,
            "temp": temp if temp is not None else ((high or 0) + (low or 0)) / 2.0,
            "dewpoint": dewpoint,
            "wind_dir": wind_dir,
            "wind_speed": wind_speed,
            "pressure": pressure,
        })
    return days


def load_daily_stats_batched(
    metar_db_path: str, stations: List[str]
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Load daily METAR aggregates for multiple stations in a single query.

    Returns {station: [day_dict, ...]}.

    Queries metar_observations directly with daily aggregation to provide
    all fields that signals need (pressure, wind, dewpoint).
    Performance improvement: avoids 20+ separate round trips.
    """
    import os
    if not os.path.exists(metar_db_path):
        raise FileNotFoundError(f"METAR DB not found: {metar_db_path}")
    if not stations:
        return {}

    conn = sqlite3.connect(metar_db_path)
    try:
        placeholders = ",".join(["?" for _ in stations])
        cur = conn.cursor()
        cur.execute(f"""
            SELECT
                station,
                date_utc,
                MAX(temp_f) as high,
                MIN(temp_f) as low,
                AVG(temp_f) as temp,
                AVG(dewpoint_f) as dewpoint,
                AVG(wind_direction_deg) as wind_dir,
                AVG(wind_speed_kt) as wind_speed,
                AVG(pressure_mb) as pressure
            FROM metar_observations
            WHERE station IN ({placeholders}) AND temp_f IS NOT NULL
            GROUP BY station, date_utc
            ORDER BY station, date_utc ASC
        """, stations)
        rows = cur.fetchall()
    finally:
        conn.close()

    result: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for station, date_utc, high, low, temp, dewpoint, wind_dir, wind_speed, pressure in rows:
        result[station].append({
            "station": station,  # WS3/5 fix: station field required by signals (eighty_two_member, seasonal_regime primary path)
            "date": date_utc,
            "high": high,
            "low": low,
            "temp": temp if temp is not None else ((high or 0) + (low or 0)) / 2.0,
            "dewpoint": dewpoint,
            "wind_dir": wind_dir,
            "wind_speed": wind_speed,
            "pressure": pressure,
        })
    return dict(result)


# ═══════════════════════════════════════════════════════════════════
# 4. Accuracy Metrics
# ═══════════════════════════════════════════════════════════════════

def compute_accuracy_metrics(
    correct: int,
    total: int,
    correct_up: int,
    total_up: int,
    correct_down: int,
    total_down: int,
) -> Dict[str, Any]:
    """
    Compute standardized accuracy metrics from raw counts.

    Returns dict with: accuracy, accuracy_up, accuracy_down,
    asymmetry, total, n_up, n_down.
    All floats rounded to 4 decimal places.
    Returns zero-filled dict if total == 0.
    """
    if total == 0:
        return {
            "accuracy": 0.0,
            "accuracy_up": 0.0,
            "accuracy_down": 0.0,
            "asymmetry": 0.0,
            "total": 0,
            "n_up": 0,
            "n_down": 0,
        }

    accuracy = correct / total
    accuracy_up = correct_up / total_up if total_up > 0 else 0.0
    accuracy_down = correct_down / total_down if total_down > 0 else 0.0
    asymmetry = abs(accuracy_up - accuracy_down)

    return {
        "accuracy": round(accuracy, 4),
        "accuracy_up": round(accuracy_up, 4),
        "accuracy_down": round(accuracy_down, 4),
        "asymmetry": round(asymmetry, 4),
        "total": total,
        "n_up": total_up,
        "n_down": total_down,
    }


# ═══════════════════════════════════════════════════════════════════
# 5. Label-safe METAR indexing
# ═══════════════════════════════════════════════════════════════════

def prior_metar_index(metar_days: List[Dict[str, Any]], target_date: str) -> Optional[int]:
    """Return the last METAR index strictly before ``target_date``.

    The strict inequality is the evaluator's look-ahead boundary: a row on
    the scored date is never supplied to a signal, even when available.
    ``metar_days`` must be chronological (the loaders guarantee this).
    """
    dates = [day["date"] for day in metar_days]
    idx = bisect.bisect_left(dates, target_date) - 1
    return idx if idx >= 0 else None


# ═══════════════════════════════════════════════════════════════════
# 6. Core Signal Evaluation Loop
# ═══════════════════════════════════════════════════════════════════

def evaluate_signal_on_ground_truth(
    station: str,
    settlements: Dict[str, float],  # {date: temp}
    metar_days: List[Dict[str, Any]],
    signal: Any,  # BaseSignal
    min_lookback: int,
    epsilon: float = 0.5,
) -> Dict[str, Any]:
    """
    Evaluate a single signal against settlement ground truth for one station.

    Returns dict with: correct, total, correct_up, total_up,
    correct_down, total_down, flat_days, predictions_list.

    predictions_list: list of {date, pred_dir, actual_dir, confidence, correct}
    for detailed downstream analysis.
    """
    sorted_dates = sorted(settlements.keys())

    correct = correct_up = correct_down = 0
    total = total_up = total_down = 0
    flat_days = 0
    predictions = []

    for i in range(1, len(sorted_dates)):
        today = sorted_dates[i]
        yesterday = sorted_dates[i - 1]

        today_temp = settlements[today]
        yesterday_temp = settlements[yesterday]

        actual_dir = compute_ground_truth_direction(today_temp, yesterday_temp, epsilon)
        if actual_dir is None:
            flat_days += 1
            continue

        metar_idx = prior_metar_index(metar_days, today)
        if metar_idx is None or metar_idx < min_lookback:
            continue

        try:
            pred_dir, confidence = signal.evaluate(metar_idx, metar_days)
        except Exception:
            logger.warning(
                "Signal %s failed on station=%s date=%s idx=%d",
                getattr(signal, "name", "unknown"),
                station,
                today,
                metar_idx,
                exc_info=True,
            )
            continue

        if pred_dir is None:
            continue  # signal didn't fire

        total += 1
        is_correct = pred_dir == actual_dir
        if is_correct:
            correct += 1
        if actual_dir == "up":
            total_up += 1
            if is_correct:
                correct_up += 1
        else:
            total_down += 1
            if is_correct:
                correct_down += 1

        predictions.append({
            "date": today,
            "pred_dir": pred_dir,
            "actual_dir": actual_dir,
            "confidence": round(confidence, 4),
            "correct": is_correct,
            "today_temp": today_temp,
            "yesterday_temp": yesterday_temp,
        })

    return {
        "correct": correct,
        "total": total,
        "correct_up": correct_up,
        "total_up": total_up,
        "correct_down": correct_down,
        "total_down": total_down,
        "flat_days": flat_days,
        "predictions": predictions,
    }


# ═══════════════════════════════════════════════════════════════════
# 6. Fusion Accuracy
# ═══════════════════════════════════════════════════════════════════

def compute_fusion_accuracy(
    settlements: Dict[str, Dict[str, float]],
    metar_batched: Dict[str, List[Dict[str, Any]]],
    registry: Any,  # SignalRegistry
    fuser: Any,
    fusion_method: str,
    station_list: List[str],
    epsilon: float = 0.5,
    confidence_floor: float = 0.0,
    kelly_fraction: float = 1.0,
    calibration_cutoff_date: Optional[str] = None,
    holdout_start: Optional[str] = None,
    holdout_end: Optional[str] = None,
    period: str = "eval",  # "eval" (exclude holdout window) | "holdout" (window only)
    exclude_degenerate: bool = False,
    calibration_method: str = "isotonic",
    eval_signal_names: Optional[List[str]] = None,
    fold_id: Optional[int] = None,  # WS7 matched-outcome correction: fold identifier
    calibration_label_source: str = "disabled",
    historical_calibration_labels: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Evaluate fusion accuracy using REAL signal predictions.

    For each station+date:
    1. Compute ground truth direction from kalshi_temp
    2. Call signal.evaluate() for every signal with enough history
    3. Feed real predictions into fuser.fuse() / fuser.fuse_signals()
    4. Compare fused output to ground truth

    Args:
        settlements: {station: {target_date: kalshi_temp}}
        metar_batched: {station: [day_dict, ...]}
        registry: SignalRegistry
        fuser: fusion engine with fuse() or fuse_signals()
        fusion_method: "llop" | "uwc"
        station_list: stations to evaluate
        epsilon: ground-truth flat threshold
        confidence_floor: minimum per-signal confidence to enter fusion
        kelly_fraction: Kelly fraction (reported; P&L scaling applied downstream)
        calibration_cutoff_date: dates <= cutoff used to fit calibration only
        holdout_start: first date of holdout window (YYYY-MM-DD)
        holdout_end: last date of holdout window (YYYY-MM-DD)
        period: "eval" excludes the holdout window from scoring;
                "holdout" scores ONLY the holdout window
        exclude_degenerate: skip DEGENERATE_SIGNAL_NAMES in fusion
                        (default False — include degenerates, per Dan's requirement)
        calibration_method: calibration backend label ("isotonic" for primary grid)
        calibration_label_source: immutable calibration-label source. The default
            "disabled" performs no fit. "historical_kalshi" accepts only the
            explicit prevalidated labels passed in historical_calibration_labels.
        historical_calibration_labels: records from
            load_historical_calibration_labels(); each record is keyed by
            station + target_date + market_direction and has event_direction.

    Returns dict with accuracy, total, correct, per_station, per_signal,
            predictions (per-trade records for P&L computation),
            calibration_quality (brier_raw, brier_calibrated, brier_improvement, ece),
            failure_counters (n_signal_exceptions, n_fusion_exceptions, ...).
    """
    from core.signals import REGISTERED_SIGNAL_NAMES, DEGENERATE_SIGNAL_NAMES

    if period not in ("eval", "holdout"):
        raise ValueError(f"period must be 'eval' or 'holdout', got {period!r}")

    # Signals that participate in fusion (degenerate ones excluded by default)
    if eval_signal_names is not None:
        pass  # Use provided signal names (Layer 2 custom subsets)
    elif exclude_degenerate:
        eval_signal_names = [
            n for n in REGISTERED_SIGNAL_NAMES if n not in DEGENERATE_SIGNAL_NAMES
        ]
    else:
        eval_signal_names = list(REGISTERED_SIGNAL_NAMES)

    def _in_period(today: str) -> bool:
        """Return True if `today` belongs to the requested period."""
        if period == "holdout":
            if holdout_start and today < holdout_start:
                return False
            if holdout_end and today > holdout_end:
                return False
            return True
        # eval period: exclude the holdout window (never touch it during selection)
        if holdout_start and today >= holdout_start:
            return False
        if holdout_end and today > holdout_end:
            return False
        return True

    correct = 0
    total = 0
    predictions = []
    per_station: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"correct": 0, "total": 0}
    )
    per_signal: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"fired": 0, "total": 0, "correct": 0,
                 "confidence_sum": 0.0, "raw_conf_sum": 0.0,
                 "raw_conf_eval": [], "cal_conf_eval": [], "correct_eval": []}
    )
    # ── Failure-accounting counters (S5) ──
    n_signal_exceptions = 0
    n_fusion_exceptions = 0
    n_dates_skipped_flat = 0
    n_dates_skipped_no_metar = 0
    n_no_fusion_output = 0
    # WS1 instrumentation (F0.2/F0.3): disaggregated rejection classes
    n_no_signal_fired = 0
    n_ds_conflict_veto = 0
    n_fusion_none_other = 0
    n_eligible_cells = 0

    # ── Calibration training pass ──
    # Settlement-derived was_correct is a scoring outcome, not a safe calibration
    # label for this evaluator. Fail closed until an independently supplied,
    # immutable historical label source is wired through this API.
    if calibration_label_source not in ("disabled", "historical_kalshi"):
        raise ValueError(
            "Unsupported calibration_label_source; use disabled or "
            "historical_kalshi"
        )
    if calibration_label_source == "historical_kalshi" and not historical_calibration_labels:
        raise ValueError("historical_kalshi requires explicit historical_calibration_labels")
    if calibration_label_source == "historical_kalshi" and calibration_cutoff_date is None:
        raise ValueError("historical_kalshi requires calibration_cutoff_date")
    if calibration_cutoff_date and calibration_cutoff_date > CALIBRATION_CUTOFF_MAX:
        raise ValueError(
            f"calibration cutoff {calibration_cutoff_date} exceeds safety maximum "
            f"{CALIBRATION_CUTOFF_MAX}"
        )
    use_calibration = (
        calibration_cutoff_date is not None and calibration_label_source != "disabled"
    )
    if use_calibration and hasattr(fuser, 'fit_calibration'):
        logger.info('Collecting calibration training data (cutoff=%s)...', calibration_cutoff_date)
        all_station_signal_history = {}  # {(signal_name, station): [(raw_conf, was_correct), ...]}
        if calibration_label_source == "historical_kalshi":
            labels = list(historical_calibration_labels or [])
            seen_keys = set()
            for label in labels:
                station = label.get("station")
                target_date = label.get("target_date")
                market_direction = label.get("market_direction")
                actual_dir = label.get("event_direction")
                key_id = (station, target_date, market_direction)
                if key_id in seen_keys:
                    raise ValueError(f"duplicate historical calibration label: {key_id}")
                seen_keys.add(key_id)
                if station not in station_list or station not in metar_batched:
                    continue
                if not isinstance(target_date, str) or target_date > calibration_cutoff_date:
                    raise ValueError(f"historical label outside cutoff: {key_id}")
                if holdout_start and holdout_start <= target_date <= (holdout_end or target_date):
                    raise ValueError(f"historical label overlaps holdout: {key_id}")
                if market_direction not in ("HIGH", "LOW") or actual_dir not in ("up", "down"):
                    raise ValueError(f"invalid historical calibration label: {label}")
                metar_idx = prior_metar_index(metar_batched[station], target_date)
                if metar_idx is None:
                    continue
                for sig_name in eval_signal_names:
                    try:
                        signal = registry.get_signal(sig_name)
                        min_lb = getattr(signal, 'min_lookback', 0)
                        if metar_idx < min_lb:
                            continue
                        pred_dir, raw_conf = signal.evaluate(metar_idx, metar_batched[station])
                    except Exception:
                        continue
                    if pred_dir is not None:
                        all_station_signal_history.setdefault((sig_name, station), []).append(
                            (raw_conf, pred_dir == actual_dir)
                        )
        else:
            # Legacy settlement-derived fitting is intentionally unreachable.
            # Keep this branch explicit so future changes cannot silently
            # reintroduce target-day labels through the scoring table.
            raise AssertionError("settlement-derived calibration path is forbidden")

        if all_station_signal_history:
            logger.info('Fitting calibration on %d signal-station pairs...', len(all_station_signal_history))
            fuser.fit_calibration(all_station_signal_history)
        else:
            logger.warning('No calibration training data collected -- skipping fit')
    # ── End calibration training pass ──

    for station in station_list:
        if station not in settlements or station not in metar_batched:
            continue

        station_settlements = settlements[station]
        metar_days = metar_batched[station]
        sorted_dates = sorted(station_settlements.keys())
        for i in range(1, len(sorted_dates)):
            today = sorted_dates[i]
            yesterday = sorted_dates[i - 1]

            today_temp = station_settlements[today]
            yesterday_temp = station_settlements[yesterday]

            # Skip training dates when calibration is active
            if use_calibration and today <= calibration_cutoff_date:
                continue

            # Skip dates outside the requested period (eval/holdout)
            if not _in_period(today):
                continue

            actual_dir = compute_ground_truth_direction(
                today_temp, yesterday_temp, epsilon
            )
            if actual_dir is None:
                n_dates_skipped_flat += 1  # WS1: was dead counter
                continue

            metar_idx = prior_metar_index(metar_days, today)
            if metar_idx is None:
                n_dates_skipped_no_metar += 1  # WS1: was dead counter
                continue
            n_eligible_cells += 1

            # Evaluate all signals for this (station, date)
            signal_predictions: Dict[str, Tuple[Optional[str], float]] = {}
            for sig_name in eval_signal_names:
                try:
                    signal = registry.get_signal(sig_name)
                    if signal is None:
                        n_signal_exceptions += 1
                        continue
                except Exception:
                    n_signal_exceptions += 1
                    continue
                min_lb = getattr(signal, "min_lookback", 0)
                if metar_idx < min_lb:
                    continue
                try:
                    pred_dir, raw_conf = signal.evaluate(metar_idx, metar_days)
                except Exception:
                    n_signal_exceptions += 1
                    logger.warning(
                        "Signal %s evaluate() threw on station=%s date=%s idx=%d",
                        sig_name, station, today, metar_idx, exc_info=True,
                    )
                    continue

                per_signal[sig_name]["total"] += 1

                if pred_dir is not None and raw_conf >= confidence_floor:
                    signal_predictions[sig_name] = (pred_dir, raw_conf)
                    per_signal[sig_name]["fired"] += 1
                    per_signal[sig_name]["confidence_sum"] += raw_conf
                    per_signal[sig_name]["raw_conf_sum"] += raw_conf

                    # Record was_correct for this signal on this date
                    was_correct = (pred_dir == actual_dir)
                    if was_correct:
                        per_signal[sig_name]["correct"] += 1

                    # ── Collect eval-period calibration records (S3) ──
                    if hasattr(fuser, 'calibration_pipeline'):
                        cal_conf = fuser.calibration_pipeline.calibrate(
                            sig_name, station, raw_conf
                        )
                    else:
                        cal_conf = raw_conf
                    per_signal[sig_name]["raw_conf_eval"].append(raw_conf)
                    per_signal[sig_name]["cal_conf_eval"].append(cal_conf)
                    per_signal[sig_name]["correct_eval"].append(was_correct)

            if not signal_predictions:
                n_no_signal_fired += 1  # WS1: was invisible
                continue

            # Fuse
            fused_confidence = None
            try:
                if fusion_method == "llop":
                    result = fuser.fuse_signals(
                        [(name, d, c) for name, (d, c) in signal_predictions.items()],
                        station,
                    )
                    if result and result[0] is not None:
                        pred_dir = result[0]
                        fused_confidence = result[1] if len(result) > 1 else None
                    else:
                        pred_dir = None
                        # WS1 (B4): reason code distinguishes DS-conflict veto
                        # from other None returns (empty/no_calibrated)
                        fusion_reason = result[3] if (result and len(result) > 3) else None
                        if fusion_reason == "ds_conflict":
                            n_ds_conflict_veto += 1
                        else:
                            n_fusion_none_other += 1
                elif fusion_method == "uwc":
                    result = fuser.fuse(signal_predictions, station=station)
                    pred_dir = result.get("direction") if result else None
                    fused_confidence = (
                        result.get("confidence") if result else None
                    )
                else:
                    raise ValueError(f"Unknown fusion_method: {fusion_method}")
            except Exception:
                n_fusion_exceptions += 1
                logger.warning(
                    "Fusion failed for station=%s date=%s method=%s",
                    station, today, fusion_method, exc_info=True,
                )
                continue

            if pred_dir is None:
                # UWC path or legacy: no reason code available
                if fusion_method != "llop":
                    n_no_fusion_output += 1
                continue

            is_correct = pred_dir == actual_dir
            if is_correct:
                correct += 1
            total += 1
            per_station[station]["correct"] += 1 if is_correct else 0
            per_station[station]["total"] += 1

            predictions.append({
                "date": today,
                "station": station,
                "fold_id": fold_id,  # WS7 matched-outcome correction
                "pred_dir": pred_dir,
                "actual_dir": actual_dir,
                "confidence": (
                    round(fused_confidence, 4)
                    if fused_confidence is not None else None
                ),
                "correct": is_correct,
                "abstained": False,  # WS7 matched-outcome correction
                "rejection_reason": None,  # WS7 matched-outcome correction
                "today_temp": today_temp,
                "yesterday_temp": yesterday_temp,
            })

    accuracy = correct / total if total > 0 else 0.0

    # ── Compute Brier/ECE from eval-period records (S3) ──
    # Per-signal eval samples stored as parallel lists:
    #   raw_conf_eval[*], cal_conf_eval[*], correct_eval[*]
    # These are IDENTICAL index sets per signal (AT3.2)
    per_signal_stats = {}
    global_raw_brier = 0.0
    global_cal_brier = 0.0
    global_n_brier = 0

    for sig_name, stats in per_signal.items():
        fire_rate = stats["fired"] / stats["total"] if stats["total"] > 0 else 0.0
        avg_confidence = (
            stats["confidence_sum"] / stats["fired"] if stats["fired"] > 0 else 0.0
        )
        sig_accuracy = (
            stats["correct"] / stats["fired"] if stats["fired"] > 0 else 0.0
        )

        # Per-signal Brier from eval samples (identical index sets)
        raw_samples = stats.get("raw_conf_eval", [])
        cal_samples = stats.get("cal_conf_eval", [])
        correct_samples = stats.get("correct_eval", [])
        n_eval = min(len(raw_samples), len(cal_samples), len(correct_samples))

        sig_brier_raw = 0.0
        sig_brier_cal = 0.0
        for j in range(n_eval):
            y = 1.0 if correct_samples[j] else 0.0
            sig_brier_raw += (y - raw_samples[j]) ** 2
            sig_brier_cal += (y - cal_samples[j]) ** 2

        if n_eval > 0:
            sig_brier_raw /= n_eval
            sig_brier_cal /= n_eval
            global_raw_brier += sig_brier_raw * n_eval
            global_cal_brier += sig_brier_cal * n_eval
            global_n_brier += n_eval

        per_signal_stats[sig_name] = {
            "accuracy": round(sig_accuracy, 4),
            "confidence": round(avg_confidence, 4),
            "fire_rate": round(fire_rate, 4),
            "fired": stats["fired"],
            "correct": stats["correct"],
            "total": stats["total"],
            "n_eval": n_eval,
            "brier_raw": round(sig_brier_raw, 6) if n_eval > 0 else 0.0,
            "brier_calibrated": round(sig_brier_cal, 6) if n_eval > 0 else 0.0,
        }

    # Global Brier (pooled weighted average across signals)
    brier_raw = global_raw_brier / global_n_brier if global_n_brier > 0 else 0.0
    brier_calibrated = global_cal_brier / global_n_brier if global_n_brier > 0 else 0.0
    brier_improvement = brier_raw - brier_calibrated

    # ECE: 10-bin equal-width over fused predictions confidence
    ece = 0.0
    ece_bins = []
    if total > 0:
        n_bins = 10
        bin_data = [[] for _ in range(n_bins)]
        for p in predictions:
            conf = p.get("confidence")
            if conf is None:
                continue
            conf = float(conf)
            bin_idx = min(int(conf * n_bins), n_bins - 1)
            bin_data[bin_idx].append(1.0 if p["correct"] else 0.0)
        for i in range(n_bins):
            lo = i / n_bins
            hi = (i + 1) / n_bins
            n_k = len(bin_data[i])
            if n_k == 0:
                ece_bins.append((lo, hi, 0, 0.0, (lo + hi) / 2.0))
                continue
            acc_k = sum(bin_data[i]) / n_k
            mean_conf_k = (lo + hi) / 2.0
            ece += (n_k / total) * abs(acc_k - mean_conf_k)
            ece_bins.append((lo, hi, n_k, round(acc_k, 4), round(mean_conf_k, 4)))
        ece = round(ece, 6)

    # ── Warning logging for failure counters (S5) ──
    if n_signal_exceptions > 0:
        logger.warning(
            "compute_fusion_accuracy: %d signal.evaluate() exceptions logged",
            n_signal_exceptions
        )
    if n_fusion_exceptions > 0:
        logger.warning(
            "compute_fusion_accuracy: %d fusion exceptions logged",
            n_fusion_exceptions
        )
    if n_dates_skipped_flat > 0 or n_dates_skipped_no_metar > 0:
        logger.info(
            "compute_fusion_accuracy: flat=%d no_metar=%d",
            n_dates_skipped_flat, n_dates_skipped_no_metar
        )

    # ── Calibration provenance (S1) ──
    calibration_method_used = calibration_method
    if hasattr(fuser, 'is_calibrated'):
        calibration_actually_applied = fuser.is_calibrated()
    else:
        calibration_actually_applied = False

    return {
        "fusion_method": fusion_method,
        "period": period,
        "accuracy": round(accuracy, 4),
        "total_trades": total,
        "filtered_trades": total,
        "kelly_weighted_trades": round(total * kelly_fraction, 2),
        "correct": correct,
        "calibration_method": calibration_method,
        "calibration_method_actually_used": calibration_method_used,
        "calibration_actually_applied": calibration_actually_applied,
        "calibration_quality": {
            "brier_raw": round(brier_raw, 6),
            "brier_calibrated": round(brier_calibrated, 6),
            "brier_improvement": round(brier_improvement, 6),
            "ece": ece,
            "ece_bins": ece_bins,
            "_n_eval": global_n_brier,
        },
        "failure_counters": {
            "n_signal_exceptions": n_signal_exceptions,
            "n_fusion_exceptions": n_fusion_exceptions,
            "n_dates_skipped_flat": n_dates_skipped_flat,
            "n_dates_skipped_no_metar": n_dates_skipped_no_metar,
            "n_no_fusion_output": n_no_fusion_output,
            "n_no_signal_fired": n_no_signal_fired,
            "n_ds_conflict_veto": n_ds_conflict_veto,
            "n_fusion_none_other": n_fusion_none_other,
            "n_eligible_cells": n_eligible_cells,
        },
        "per_station": {
            s: {
                "accuracy": round(d["correct"] / d["total"], 4) if d["total"] > 0 else 0.0,
                "trades": d["total"],
            }
            for s, d in per_station.items()
        },
        "per_signal": per_signal_stats,
        "predictions": predictions,
    }


# ═══════════════════════════════════════════════════════════════════
# 7. P&L Metrics
# ═══════════════════════════════════════════════════════════════════

def compute_pnl_metrics(
    predictions: List[Dict[str, Any]],
    fee_model: str = "taker_only",
    kelly_fraction: float = 1.0,
) -> Dict[str, Any]:
    """
    Compute fee-aware P&L metrics from prediction list.

    (PROXY) Simplified P&L: canonical Kalshi fee (price-dependent
    $0.01-$0.02/contract), $1/trade notional. Fee uses canonical
    kalshi_fee(1, confidence) per Kalshi formula:
      ceil(0.07 x P x (1-P) x 100) / 100 cents per contract.
    Market price = confidence (for binary options, confidence IS the market price).
    NOT real P&L - simplified $1/trade notional (not real fill prices).
    Directional accuracy is the primary sweep metric.

    Args:
        predictions: list of {pred_dir, confidence, correct, ...}
        fee_model: "taker_only" (canonical Kalshi taker fee),
                   "taker_and_slippage" (canonical fee + SLIPPAGE_BUDGET),
                   "maker" (no fee)
        kelly_fraction: Kelly fraction to scale P&L (0.0-1.0)

    Returns dict with net_pnl, net_pnl_per_trade, sharpe_ratio,
            profit_factor, max_drawdown, win_rate, avg_win, avg_loss,
            kelly_adjusted_pnl.
    """
    import math

    if not predictions:
        return {
            "net_pnl": 0.0,
            "net_pnl_per_trade": 0.0,
            "sharpe_ratio": 0.0,
            "profit_factor": 0.0,
            "max_drawdown": 0.0,
            "win_rate": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "n_trades": 0,
            "_disclaimer": "(PROXY) Simplified P&L: canonical Kalshi fee (price-dependent $0.01-$0.02/contract), "
                            "Not real P&L — structurally wrong for binary options. "
                            "Directional accuracy is the primary sweep metric. "
                            "See PRE-SWEEP-IMPLEMENTATION-SPECS-2026-08-13.md \u00a7S7.",
        }

    # Canonical Kalshi fee: price-dependent per Kalshi formula.
    #   ceil(0.07 x P x (1-P) x 100) / 100 cents per contract.
    #
    # Market price = confidence (for binary options, confidence IS the bid/ask
    # midpoint -- buying a Yes contract at P=confidence is the market price).
    # Exit price is $1 (win) or $0 (loss) -- boundary prices have zero fee.
    # So total fee = kalshi_fee(1, confidence), i.e. entry fee only.
    # Slippage: + SLIPPAGE_BUDGET ($0.002 per trade) when fee_model allows.
    #
    # The P&L formula remains simplified $1/trade notional -- this is a PROXY
    # P&L that does not use real fill prices. The fee IS canonical.
    from scripts.sweep.config import kalshi_fee, SLIPPAGE_BUDGET
    trade_pnls = []
    for p in predictions:
        # Confidence serves as market price for binary options
        conf = max(0.001, min(0.999, p.get("confidence", 0.5)))
        fee = kalshi_fee(1, conf)
        if fee_model == "taker_and_slippage":
            fee += SLIPPAGE_BUDGET
        elif fee_model == "maker":
            fee = 0.0
        if p.get("correct", False):
            trade_pnls.append(1.0 - fee)
        else:
            trade_pnls.append(-1.0 - fee)


    n = len(trade_pnls)
    net_pnl = sum(trade_pnls)
    net_pnl_per_trade = net_pnl / n if n > 0 else 0.0

    wins = [p for p in trade_pnls if p > 0]
    losses = [p for p in trade_pnls if p < 0]
    win_rate = len(wins) / n if n > 0 else 0.0
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0

    # Sharpe ratio (annualized, assuming daily trades)
    if n > 1:
        mean_pnl = net_pnl_per_trade
        variance = sum((p - mean_pnl) ** 2 for p in trade_pnls) / (n - 1)
        std_pnl = math.sqrt(variance) if variance > 0 else 0.0
        sharpe = (mean_pnl / std_pnl * math.sqrt(252)) if std_pnl > 0 else 0.0
    else:
        sharpe = 0.0

    # Profit factor
    gross_profit = sum(wins) if wins else 0.0
    gross_loss = abs(sum(losses)) if losses else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0.0

    # Max drawdown
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in trade_pnls:
        cumulative += pnl
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd

    # Kelly-adjusted P&L
    kelly_adjusted_pnl = net_pnl * kelly_fraction

    return {
        "net_pnl": round(net_pnl, 4),
        "net_pnl_per_trade": round(net_pnl_per_trade, 4),
        "sharpe_ratio": round(sharpe, 4),
        "profit_factor": round(profit_factor, 4),
        "max_drawdown": round(max_dd, 4),
        "win_rate": round(win_rate, 4),
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "n_trades": n,
        "kelly_fraction": kelly_fraction,
        "kelly_adjusted_pnl": round(kelly_adjusted_pnl, 4),
        "_disclaimer": "(PROXY) Simplified P&L: canonical Kalshi fee (price-dependent $0.01-$0.02/contract), "
                        "Not real P&L — structurally wrong for binary options. "
                        "Directional accuracy is the primary sweep metric. "
                        "See PRE-SWEEP-IMPLEMENTATION-SPECS-2026-08-13.md §S7.",
    }


# ═══════════════════════════════════════════════════════════════════
# 8. Baseline Accuracy Computation
# ═══════════════════════════════════════════════════════════════════

def compute_baseline_accuracies(
    settlements: Dict[str, Dict[str, float]],
    station_list: List[str],
    epsilon: float = 0.5,
    holdout_start: Optional[str] = None,
    holdout_end: Optional[str] = None,
    period: str = "eval",
) -> Dict[str, float]:
    """
    Compute naive baseline accuracies for comparison.

    Returns dict with always_up_accuracy, always_down_accuracy,
    always_up_trades, always_down_trades, n_days, plus:
    - persistence_accuracy: predict today's direction = yesterday's direction
      (the minimum viable weather forecast; standard baseline).
    - trend_accuracy: predict direction of 3-day trailing trend
      (today vs 3 days ago sign).
    """
    if period not in ("eval", "holdout"):
        raise ValueError(f"period must be 'eval' or 'holdout', got {period!r}")

    def _in_period(today: str) -> bool:
        if period == "holdout":
            if holdout_start and today < holdout_start:
                return False
            if holdout_end and today > holdout_end:
                return False
            return True
        if holdout_start and today >= holdout_start:
            return False
        if holdout_end and today > holdout_end:
            return False
        return True

    up_correct = up_total = 0
    down_correct = down_total = 0
    n_days = 0

    # Persistence: predict today's direction = yesterday's realized direction.
    # Requires temp(today), temp(yesterday), temp(day-before) all present.
    persist_correct = persist_total = 0
    # Trend (Version B, HONEST): predict today's direction = 3-day trailing trend
    # computed from YESTERDAY-AND-EARLIER ONLY (sign(yesterday - 3ago)).
    # F0.1 fix: the old implementation used sign(today - 3ago), which peeks at
    # today's settled temperature (the label). Withdrawn as a benchmark.
    trend_correct = trend_total = 0
    # Trend (Version C): short persistence sign(yesterday - 2ago) — label-safe.
    trend_c_correct = trend_c_total = 0

    for station in station_list:
        if station not in settlements:
            continue
        station_settlements = settlements[station]
        sorted_dates = sorted(station_settlements.keys())

        for i in range(1, len(sorted_dates)):
            today = sorted_dates[i]
            yesterday = sorted_dates[i - 1]

            if not _in_period(today):
                continue

            today_temp = station_settlements[today]
            yesterday_temp = station_settlements[yesterday]

            actual_dir = compute_ground_truth_direction(
                today_temp, yesterday_temp, epsilon
            )
            if actual_dir is None:
                continue

            n_days += 1

            # Always-UP: predicts 'up' every day
            if actual_dir == "up":
                up_correct += 1
            up_total += 1

            # Always-DOWN: predicts 'down' every day
            if actual_dir == "down":
                down_correct += 1
            down_total += 1

            # Persistence: yesterday's realized direction (needs day-before data)
            if i >= 2:
                day_before = sorted_dates[i - 2]
                day_before_temp = station_settlements[day_before]
                yest_dir = compute_ground_truth_direction(
                    yesterday_temp, day_before_temp, epsilon
                )
                if yest_dir is not None:
                    persist_total += 1
                    if yest_dir == actual_dir:
                        persist_correct += 1

            # Trend (Version B, HONEST): direction of 3-day trend AS OF YESTERDAY.
            # Uses ONLY dates <= yesterday — no target-day (label) access.
            if i >= 3:
                three_ago = sorted_dates[i - 3]
                three_ago_temp = station_settlements[three_ago]
                trend_dir = compute_ground_truth_direction(
                    yesterday_temp, three_ago_temp, epsilon
                )
                if trend_dir is not None:
                    trend_total += 1
                    if trend_dir == actual_dir:
                        trend_correct += 1

            # Trend (Version C): short persistence sign(yesterday - 2ago).
            if i >= 2:
                two_ago = sorted_dates[i - 2]
                two_ago_temp = station_settlements[two_ago]
                trend_c_dir = compute_ground_truth_direction(
                    yesterday_temp, two_ago_temp, epsilon
                )
                if trend_c_dir is not None:
                    trend_c_total += 1
                    if trend_c_dir == actual_dir:
                        trend_c_correct += 1

    return {
        "always_up_accuracy": round(up_correct / up_total, 4) if up_total > 0 else 0.0,
        "always_down_accuracy": round(down_correct / down_total, 4) if down_total > 0 else 0.0,
        "always_up_trades": up_total,
        "always_down_trades": down_total,
        "persistence_accuracy": round(persist_correct / persist_total, 4) if persist_total > 0 else 0.0,
        "persistence_trades": persist_total,
        "trend_accuracy": round(trend_correct / trend_total, 4) if trend_total > 0 else 0.0,
        "trend_trades": trend_total,
        "trend_version": "B_honest_sign(yesterday-3ago)",
        "trend_c_accuracy": round(trend_c_correct / trend_c_total, 4) if trend_c_total > 0 else 0.0,
        "trend_c_trades": trend_c_total,
        "trend_c_version": "C_persistence_sign(yesterday-2ago)",
        "n_days": n_days,
    }
