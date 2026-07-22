#!/usr/bin/env python3
"""
Phase 1: Calibration for 7 intraday/NWP signals against the live database.

This script connects to the live METAR DB (metar_backfill.db) and NWP DB
(nwp_forecasts.db) and evaluates each of the 7 signals that require
intraday or NWP data — signals that don't fire in the METAR-only backfill
calibration pipeline.

Target signals:
  1. nwp_direct              — needs nwp_forecasts.db
  2. nwp_dtdt_fusion         — needs nwp_forecasts.db + metar_observations
  3. hrrr_bias_corrected     — needs metar_observations
  4. intraday_metar_confirmation — needs metar_observations
  5. metar_dtdt              — needs metar_observations
  6. pressure_tendency       — needs metar_observations
  7. ai_composite            — needs ai_forecasts table (may not exist)

For each signal, records: fire_rate, total_attempts, total_fired, avg_confidence.

Usage:
    python3 scripts/calibrate_live_signals.py
    python3 scripts/calibrate_live_signals.py --days 90
    python3 scripts/calibrate_live_signals.py --station KNYC
    python3 scripts/calibrate_live_signals.py --output data/calibration_live_7_signals.json
    python3 scripts/calibrate_live_signals.py --verbose
"""

import sqlite3
import os
import sys
import json
import argparse
import logging
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from typing import Optional, Tuple, List, Dict

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.signals import SignalRegistry, create_signal_registry
from core.signals.nwp_direct_signal import NwpDirectSignal
from core.signals.nwp_dtdt_fusion_signal import NwpDtdtFusionSignal
from core.signals.hrrr_bias_corrected_signal import HrrrBiasCorrectedSignal
from core.signals.intraday_metar_confirmation_signal import IntradayMetarConfirmationSignal
from core.signals.metar_dtdt_signal import MetarDtdtSignal
from core.signals.pressure_tendency_signal import PressureTendencySignal
from core.signals.ai_composite_signal import AiCompositeSignal

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('calibrate_live_signals')

# ── Default paths (relative to project root) ───────────────────────────────
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
METAR_DB = os.path.join(PROJECT_ROOT, 'data', 'metar_backfill.db')
NWP_DB = os.path.join(PROJECT_ROOT, 'data', 'nwp_forecasts.db')

# 20 standard Kalshi stations
STATIONS = [
    'KATL', 'KAUS', 'KBOS', 'KDCA', 'KDEN', 'KDFW', 'KHOU', 'KLAS',
    'KLAX', 'KMDW', 'KMIA', 'KMSP', 'KMSY', 'KNYC', 'KOKC', 'KPHL',
    'KPHX', 'KSAT', 'KSEA', 'KSFO',
]

# The 7 signals that need intraday/NWP data
TARGET_SIGNALS = [
    'nwp_direct',
    'nwp_dtdt_fusion',
    'hrrr_bias_corrected',
    'intraday_metar_confirmation',
    'metar_dtdt',
    'pressure_tendency',
    'ai_composite',
]


def verify_databases() -> Dict[str, bool]:
    """Verify all required databases exist and have the expected tables."""
    status = {}

    # METAR DB
    if os.path.exists(METAR_DB):
        try:
            conn = sqlite3.connect(METAR_DB)
            c = conn.cursor()
            c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='metar_observations'")
            status['metar_db'] = c.fetchone() is not None
            if status['metar_db']:
                c.execute("SELECT COUNT(*) FROM metar_observations")
                status['metar_obs_count'] = c.fetchone()[0]
                c.execute("SELECT MIN(date_utc), MAX(date_utc) FROM metar_observations")
                status['metar_date_range'] = c.fetchone()
            conn.close()
        except Exception as e:
            status['metar_db'] = f"error: {e}"
    else:
        status['metar_db'] = False

    # NWP DB
    if os.path.exists(NWP_DB):
        try:
            conn = sqlite3.connect(NWP_DB)
            c = conn.cursor()
            c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='nwp_forecasts'")
            status['nwp_db'] = c.fetchone() is not None
            if status['nwp_db']:
                c.execute("SELECT COUNT(*) FROM nwp_forecasts")
                status['nwp_forecast_count'] = c.fetchone()[0]
                c.execute("SELECT MIN(target_date), MAX(target_date) FROM nwp_forecasts")
                status['nwp_date_range'] = c.fetchone()
                c.execute("SELECT DISTINCT model FROM nwp_forecasts ORDER BY model")
                status['nwp_models'] = [r[0] for r in c.fetchall()]
                c.execute("SELECT DISTINCT variable FROM nwp_forecasts ORDER BY variable")
                status['nwp_variables'] = [r[0] for r in c.fetchall()]
                # Check for ensemble member data
                c.execute("SELECT COUNT(DISTINCT member_index) FROM nwp_forecasts WHERE member_index IS NOT NULL AND member_index != 0")
                status['nwp_ensemble_members'] = c.fetchone()[0]
            conn.close()
        except Exception as e:
            status['nwp_db'] = f"error: {e}"
    else:
        status['nwp_db'] = False

    # Check for ai_forecasts table (needed by ai_composite)
    if os.path.exists(NWP_DB):
        try:
            conn = sqlite3.connect(NWP_DB)
            c = conn.cursor()
            c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ai_forecasts'")
            status['ai_forecasts_table'] = c.fetchone() is not None
            if status['ai_forecasts_table']:
                c.execute("SELECT COUNT(*) FROM ai_forecasts")
                status['ai_forecast_count'] = c.fetchone()[0]
            conn.close()
        except Exception as e:
            status['ai_forecasts_table'] = f"error: {e}"
    else:
        status['ai_forecasts_table'] = False

    return status


def get_date_range(days_back: int = 90) -> List[str]:
    """
    Generate a list of ISO date strings for the last N days.

    Uses a wider window (default 90 days) to maximize the chance of
    intraday signals finding sufficient data to trigger.
    """
    dates = []
    today = datetime.now(timezone.utc)
    for i in range(days_back, 0, -1):
        d = today - timedelta(days=i)
        dates.append(d.strftime('%Y-%m-%d'))
    return dates


def diagnose_intraday_timestamps(conn: sqlite3.Connection, station: str, date: str) -> Dict:
    """
    Check if the METAR timestamps have timezone offsets that would break
    the intraday signal parsers (which use strptime with %Y-%m-%dT%H:%M:%S).

    The actual DB timestamps include +00:00 timezone info, e.g.:
        '2026-07-22T19:51:00+00:00'
    The signal parsers try:
        datetime.strptime(ts, '%Y-%m-%dT%H:%M:%S')
    which fails on the +00:00 suffix.
    """
    result = {'exists': False, 'has_tz_offset': False, 'sample_timestamps': []}

    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT timestamp_utc FROM metar_observations
            WHERE station = ? AND date_utc = ?
            ORDER BY timestamp_utc DESC
            LIMIT 3
        """, (station, date))
        rows = cur.fetchall()
        if rows:
            result['exists'] = True
            result['sample_timestamps'] = [r[0] for r in rows]
            # Check for timezone suffix
            for r in rows:
                if r[0] and ('+' in r[0] or r[0].endswith('Z')):
                    result['has_tz_offset'] = True
                    break
    except Exception as e:
        result['error'] = str(e)

    return result


def evaluate_signal_for_station(
    signal_obj,
    signal_name: str,
    station: str,
    date: str,
    metar_conn: sqlite3.Connection,
    nwp_conn: sqlite3.Connection,
    db_status: Dict,
) -> Dict:
    """
    Call evaluate_for_station() on a signal with the appropriate DB connection.

    Returns a dict with the signal evaluation result.
    """
    try:
        # Pass the appropriate connection based on signal type
        if signal_name == 'nwp_direct':
            # NwpDirectSignal does NOT use the passed conn for evaluate_for_station
            # It opens its own NWP DB connection internally via _get_temp()
            direction, confidence = signal_obj.evaluate_for_station(
                station, date, market_type='HIGH'
            )
        elif signal_name == 'nwp_dtdt_fusion':
            # NwpDtdtFusionSignal delegates to NwpDirectSignal and MetarDtdtSignal
            # It needs metar_conn for the MetarDtdtSignal part
            direction, confidence = signal_obj.evaluate_for_station(
                station, date, conn=metar_conn, market_type='HIGH'
            )
        elif signal_name == 'hrrr_bias_corrected':
            direction, confidence = signal_obj.evaluate_for_station(
                station, date, conn=metar_conn, market_type='HIGH', lookahead_hours=3
            )
        elif signal_name == 'intraday_metar_confirmation':
            # Note: This signal class uses observation_time field, not timestamp_utc
            # It also has a time window check (only fires for today or yesterday)
            direction, confidence = signal_obj.evaluate_for_station(
                station, date, conn=metar_conn, market_type='HIGH'
            )
        elif signal_name in ('metar_dtdt', 'pressure_tendency'):
            direction, confidence = signal_obj.evaluate_for_station(
                station, date, conn=metar_conn, market_type='HIGH'
            )
        elif signal_name == 'ai_composite':
            direction, confidence = signal_obj.evaluate_for_station(
                station, date, market_type='HIGH'
            )
        else:
            direction, confidence = signal_obj.evaluate_for_station(
                station, date, conn=metar_conn
            )

        return {
            'signal': signal_name,
            'station': station,
            'date': date,
            'direction': direction,
            'confidence': round(confidence, 4) if confidence is not None else None,
            'fired': direction is not None and confidence > 0,
        }

    except Exception as e:
        logger.debug(f"  {signal_name}@{station} {date}: error: {e}")
        return {
            'signal': signal_name,
            'station': station,
            'date': date,
            'direction': None,
            'confidence': None,
            'fired': False,
            'error': str(e),
        }


def run_calibration(
    db_path: str = METAR_DB,
    nwp_db_path: str = NWP_DB,
    station_filter: Optional[str] = None,
    days_back: int = 90,
    output_path: str = 'data/calibration_live_7_signals.json',
) -> Dict:
    """
    Run live calibration for the 7 intraday/NWP signals.

    For each signal and each station, evaluates the signal on a range of
    historical dates. Records fire_rate, total_attempts, total_fired, avg_confidence.

    Args:
        db_path: Path to METAR SQLite database
        nwp_db_path: Path to NWP forecasts SQLite database
        station_filter: Optional single station to test
        days_back: Number of days of historical data to evaluate
        output_path: Output path for calibration results JSON

    Returns:
        dict with calibration results, status, and metadata
    """
    # ── Verify databases ─────────────────────────────────────────────────
    db_status = verify_databases()
    logger.info("Database status:")
    for k, v in db_status.items():
        logger.info(f"  {k}: {v}")

    if not db_status.get('metar_db'):
        logger.error("METAR database not available. Aborting.")
        return {'status': 'error', 'reason': 'metar_db_unavailable'}

    if not db_status.get('nwp_db'):
        logger.warning("NWP database not available. NWP signals will not fire.")

    # ── Connect to databases ─────────────────────────────────────────────
    metar_conn = sqlite3.connect(METAR_DB)
    nwp_conn = sqlite3.connect(NWP_DB) if db_status.get('nwp_db') else None

    # ── Initialize signal registry ───────────────────────────────────────
    # NOTE: SignalRegistry(db_path) passes the same db_path to all signals.
    # NwpDirectSignal and AiCompositeSignal use db_path as their NWP path.
    # If we pass METAR_DB, those signals will query the wrong DB.
    # We instantiate signals directly with the correct DB paths instead.
    all_signals = {}
    all_signals['nwp_direct'] = NwpDirectSignal(db_path=nwp_db_path)
    all_signals['nwp_dtdt_fusion'] = NwpDtdtFusionSignal(db_path=db_path, nwp_db_path=nwp_db_path)
    all_signals['hrrr_bias_corrected'] = HrrrBiasCorrectedSignal(db_path=db_path, hrrr_db_path=nwp_db_path)
    all_signals['intraday_metar_confirmation'] = IntradayMetarConfirmationSignal(db_path=db_path)
    all_signals['metar_dtdt'] = MetarDtdtSignal(db_path=db_path)
    all_signals['pressure_tendency'] = PressureTendencySignal(db_path=db_path)
    all_signals['ai_composite'] = AiCompositeSignal(db_path=nwp_db_path)

    logger.info(f"Target signals: {TARGET_SIGNALS}")

    # Validate all signals were created
    for name in TARGET_SIGNALS:
        if name not in all_signals:
            logger.error(f"Signal '{name}' failed to instantiate!")
        else:
            logger.info(f"  {name}: {type(all_signals[name]).__name__}")

    # ── Determine stations and dates ─────────────────────────────────────
    stations = [station_filter.upper()] if station_filter else STATIONS
    dates = get_date_range(days_back)

    logger.info(f"Stations: {len(stations)} ({', '.join(stations)})")
    logger.info(f"Date range: {dates[0]} to {dates[-1]} ({len(dates)} days)")

    # ── Timestamp diagnostic ─────────────────────────────────────────────
    # Check for known timestamp parsing issue with intraday signals
    ts_diagnostic = {}
    for station in stations[:3]:
        for date in dates[-14:-7]:  # Check a recent week
            ts_diag = diagnose_intraday_timestamps(metar_conn, station, date)
            if ts_diag['exists']:
                ts_diagnostic[f"{station}_{date}"] = ts_diag
                break
        if ts_diagnostic:
            break

    has_tz_issue = any(
        diag.get('has_tz_offset') for diag in ts_diagnostic.values()
    )
    if has_tz_issue:
        logger.warning(
            "⚠️  Detected timezone offset in METAR timestamps (e.g. '+00:00'). "
            "Intraday signals (metar_dtdt, pressure_tendency, hrrr_bias_corrected) "
            "use strptime with '%Y-%%m-%%dT%%H:%%M:%%S' which does NOT handle "
            "timezone suffixes. This will cause them to return (None, 0.0) "
            "even when sufficient data exists."
        )

    # ── Evaluate each signal ─────────────────────────────────────────────
    results = []
    signal_summary = defaultdict(lambda: {'fired': 0, 'total': 0, 'errors': 0, 'confidences': []})

    for signal_name in TARGET_SIGNALS:
        signal_obj = all_signals.get(signal_name)
        if signal_obj is None:
            logger.warning(f"Signal '{signal_name}' not found in registry. Skipping.")
            continue

        logger.info(f"\n{'='*60}")
        logger.info(f"Signal: {signal_name}")

        for station in stations:
            station_fired = 0
            station_total = 0
            station_errors = 0

            for date in dates:
                result = evaluate_signal_for_station(
                    signal_obj, signal_name, station, date,
                    metar_conn, nwp_conn, db_status,
                )
                results.append(result)

                if result.get('error'):
                    station_errors += 1
                elif result['fired']:
                    station_fired += 1
                station_total += 1

                signal_summary[signal_name]['fired'] += 1 if result['fired'] else 0
                signal_summary[signal_name]['total'] += 1
                if result.get('error'):
                    signal_summary[signal_name]['errors'] += 1
                if result['fired'] and result['confidence'] is not None:
                    signal_summary[signal_name]['confidences'].append(result['confidence'])

            if station_fired > 0:
                logger.info(f"  {station}: {station_fired}/{station_total} fired "
                           f"({station_errors} errors)")

        total_fired = signal_summary[signal_name]['fired']
        total_attempts = signal_summary[signal_name]['total']
        total_errs = signal_summary[signal_name]['errors']
        confidences = signal_summary[signal_name]['confidences']
        avg_conf = sum(confidences) / len(confidences) if confidences else None
        logger.info(f"  => Total: {total_fired}/{total_attempts} fired "
                   f"({total_errs} errors), avg_confidence={avg_conf}")

    metar_conn.close()
    if nwp_conn:
        nwp_conn.close()

    # ── Build per-signal calibration report ──────────────────────────────
    signal_reports = []
    for signal_name in TARGET_SIGNALS:
        if signal_name not in signal_summary:
            continue

        summary = signal_summary[signal_name]
        signal_results = [r for r in results if r['signal'] == signal_name]

        # Per-station aggregation
        per_station = defaultdict(lambda: {'fired': 0, 'total': 0, 'errors': 0, 'confidences': []})
        for r in signal_results:
            st = r['station']
            per_station[st]['total'] += 1
            if r['fired']:
                per_station[st]['fired'] += 1
                if r['confidence'] is not None:
                    per_station[st]['confidences'].append(r['confidence'])
            if r.get('error'):
                per_station[st]['errors'] += 1

        # Per-station summary
        per_station_summary = {}
        for st, s in per_station.items():
            per_station_summary[st] = {
                'fired': s['fired'],
                'total': s['total'],
                'errors': s['errors'],
                'fire_rate_pct': round(s['fired'] / max(s['total'], 1) * 100, 2),
                'avg_confidence': round(sum(s['confidences']) / max(len(s['confidences']), 1), 4) if s['confidences'] else None,
            }

        # Error analysis
        error_results = [r for r in signal_results if r.get('error')]
        error_types = defaultdict(int)
        for r in error_results:
            err_msg = r.get('error', 'unknown')
            # Normalize error messages
            for key in ['parse', 'no such table', 'cannot unpack', 'NoneType', 'timedelta', 'timezone', 'strptime']:
                if key in err_msg.lower():
                    error_types[key] += 1
                    break
            else:
                error_types['other'] += 1

        # Confidences
        confidences = summary['confidences']

        report = {
            'signal': signal_name,
            'total_attempts': summary['total'],
            'total_fired': summary['fired'],
            'total_errors': summary['errors'],
            'fire_rate_pct': round(summary['fired'] / max(summary['total'], 1) * 100, 2),
            'avg_confidence': round(sum(confidences) / max(len(confidences), 1), 4) if confidences else None,
            'max_confidence': round(max(confidences), 4) if confidences else None,
            'min_confidence': round(min(confidences), 4) if confidences else None,
            'data_source': _classify_signal_data_source(signal_name),
            'error_types': dict(error_types),
            'per_station': per_station_summary,
        }

        # Add notes about known issues
        notes = []
        if signal_name == 'ai_composite' and not db_status.get('ai_forecasts_table'):
            notes.append(
                "ai_forecasts table does not exist in nwp_forecasts.db. "
                "The ai_composite signal queries the ai_forecasts table for AI model "
                "predictions (AIGFS, GraphCast, AIFS). Without this table, the signal "
                "falls back to classical NWP data only, but may still fire if classical "
                "models (GFS, ECMWF, ICON, GEM) have sufficient data."
            )
        if signal_name in ('metar_dtdt', 'pressure_tendency', 'hrrr_bias_corrected') and has_tz_issue:
            notes.append(
                "METAR timestamps include timezone offset (+00:00) which breaks the "
                "strptime format '%Y-%%m-%%dT%%H:%%M:%%S'. This causes the signal to "
                "return (None, 0.0) even when sufficient data exists. "
                "Fix: update the signal's timestamp parsing to handle timezone suffixes."
            )
        if signal_name == 'nwp_dtdt_fusion' and has_tz_issue:
            notes.append(
                "The NWP part (NwpDirectSignal) works, but the METAR dT/dt sub-signal "
                "fails due to the timestamp parsing issue above, so fusion may not fire."
            )
        if signal_name == 'intraday_metar_confirmation':
            notes.append(
                "This signal uses 'observation_time' field (not 'timestamp_utc') and "
                "also has a time-window check: it only evaluates for today or yesterday. "
                "Historical evaluations will return None."
            )
        if signal_name == 'esdr':
            member_count = db_status.get('nwp_ensemble_members', 0)
            notes.append(
                f"ESDR requires >=10 distinct ensemble members. "
                f"Current nwp_forecasts.db has {member_count} member(s). "
                f"Without full ensemble data, ESDR cannot fire."
            )

        if notes:
            report['notes'] = notes

        signal_reports.append(report)

    # ── Aggregate summary ────────────────────────────────────────────────
    total_fired_all = sum(s['fired'] for s in signal_summary.values())
    total_attempts_all = sum(s['total'] for s in signal_summary.values())
    signals_with_trades = [s for s in TARGET_SIGNALS if s in signal_summary and signal_summary[s]['fired'] > 0]
    signals_with_errors = [s for s in TARGET_SIGNALS if s in signal_summary and signal_summary[s]['errors'] > 0]

    aggregate = {
        'total_signals': len(TARGET_SIGNALS),
        'signals_with_trades': len(signals_with_trades),
        'signals_with_zero_trades': len(TARGET_SIGNALS) - len(signals_with_trades),
        'signals_with_errors': len(signals_with_errors),
        'total_attempts': total_attempts_all,
        'total_fired': total_fired_all,
        'overall_fire_rate_pct': round(total_fired_all / max(total_attempts_all, 1) * 100, 2),
        'signals_with_trades_list': sorted(signals_with_trades),
        'signals_with_zero_trades_list': sorted(
            s for s in TARGET_SIGNALS if s not in signal_summary or signal_summary[s]['fired'] == 0
        ),
    }

    # ── Build output ─────────────────────────────────────────────────────
    output = {
        'metadata': {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'script': 'calibrate_live_signals.py',
            'project_root': PROJECT_ROOT,
            'metar_db': METAR_DB,
            'nwp_db': NWP_DB,
            'stations': stations,
            'date_range': f"{dates[0]} to {dates[-1]} ({len(dates)} days)",
            'days_back': days_back,
            'db_status': {k: str(v) for k, v in db_status.items()},
        },
        'diagnostics': {
            'timestamp_parsing_issue': has_tz_issue,
            'timestamp_sample': ts_diagnostic,
            'nwp_ensemble_members': db_status.get('nwp_ensemble_members', 0),
            'ai_forecasts_exists': db_status.get('ai_forecasts_table', False),
        },
        'signal_results': signal_reports,
        'aggregate': aggregate,
    }

    # ── Save output ──────────────────────────────────────────────────────
    output_abs = os.path.join(PROJECT_ROOT, output_path) if not os.path.isabs(output_path) else output_path
    os.makedirs(os.path.dirname(output_abs) or '.', exist_ok=True)
    with open(output_abs, 'w') as f:
        json.dump(output, f, indent=2, default=str)
    logger.info(f"\n{'='*60}")
    logger.info(f"Results saved to {output_abs}")
    logger.info(f"{'='*60}")

    # ── Print summary ────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"Phase 1: Live Calibration for 7 Intraday/NWP Signals")
    print(f"{'='*60}")
    print(f"  Date range: {dates[0]} to {dates[-1]} ({len(dates)} days)")
    print(f"  Stations: {len(stations)}")
    print(f"  Signals with trades: {len(signals_with_trades)}/{len(TARGET_SIGNALS)}")
    print(f"  Signals with zero trades: {aggregate['signals_with_zero_trades']}")
    print(f"  Total signal evaluations: {total_attempts_all}")
    print(f"  Total signal fires: {total_fired_all}")
    print(f"  Overall fire rate: {aggregate['overall_fire_rate_pct']}%")
    print()
    print(f"  Per-signal results:")
    for report in signal_reports:
        print(f"    {report['signal']:>30}: {report['total_fired']:>6} fires / "
              f"{report['total_attempts']:>6} attempts "
              f"({report['fire_rate_pct']:>5.2f}%) "
              f"avg_conf={report['avg_confidence']}")
    print()
    if has_tz_issue:
        print(f"  ⚠️  NOTE: Timestamp parsing issue detected in metar timestamps.")
        print(f"     Intraday signals (metar_dtdt, pressure_tendency, hrrr_bias_corrected)")
        print(f"     use strptime '%Y-%m-%dT%H:%M:%S' which doesn't handle '+00:00'")
        print(f"     timezone offset. This causes them to return (None, 0.0).")
    if not db_status.get('ai_forecasts_table'):
        print(f"  ⚠️  NOTE: ai_forecasts table does not exist. ai_composite signal")
        print(f"     cannot query AI model data (AIGFS, GraphCast, AIFS).")
    print(f"{'='*60}")

    return output


def _classify_signal_data_source(signal_name: str) -> str:
    """Classify the data source required by a signal."""
    if signal_name == 'nwp_direct':
        return 'nwp_forecasts'
    if signal_name == 'nwp_dtdt_fusion':
        return 'nwp_forecasts + metar_observations'
    if signal_name == 'hrrr_bias_corrected':
        return 'metar_observations (intraday) + hrrr_forecasts'
    if signal_name == 'intraday_metar_confirmation':
        return 'metar_observations (intraday)'
    if signal_name == 'metar_dtdt':
        return 'metar_observations (intraday)'
    if signal_name == 'pressure_tendency':
        return 'metar_observations (intraday)'
    if signal_name == 'ai_composite':
        return 'nwp_forecasts + ai_forecasts'
    return 'unknown'


# ── CLI ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description='Phase 1: Calibration for 7 intraday/NWP signals',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 scripts/calibrate_live_signals.py
  python3 scripts/calibrate_live_signals.py --days 90
  python3 scripts/calibrate_live_signals.py --station KNYC
  python3 scripts/calibrate_live_signals.py --output data/calibration_live_7_signals.json
  python3 scripts/calibrate_live_signals.py --verbose
        """,
    )
    parser.add_argument('--db-path', type=str, default=METAR_DB,
                        help='Path to METAR SQLite database')
    parser.add_argument('--nwp-db-path', type=str, default=NWP_DB,
                        help='Path to NWP forecasts SQLite database')
    parser.add_argument('--station', type=str, default=None,
                        help='Single station to test (default: all 20)')
    parser.add_argument('--days', type=int, default=90,
                        help='Number of days of historical data to evaluate (default: 90)')
    parser.add_argument('--output', type=str, default='data/calibration_live_7_signals.json',
                        help='Output path for calibration results JSON')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Enable debug logging')

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger('calibrate_live_signals').setLevel(logging.DEBUG)

    run_calibration(
        db_path=args.db_path,
        nwp_db_path=args.nwp_db_path,
        station_filter=args.station,
        days_back=args.days,
        output_path=args.output,
    )


if __name__ == '__main__':
    main()