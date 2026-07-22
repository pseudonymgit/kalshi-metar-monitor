#!/usr/bin/env python3
"""
B-Mode 1: Live Calibration Script for All 23 Signals

Unlocks the 12 signals that produce zero trades in the METAR-only backfill
calibration pipeline by connecting to the live databases and calling
evaluate_for_station() for each signal/station/date combination.

Signal Data Sources:
  - Intraday (6):   metar_observations table (metar_backfill.db)
  - NWP (3):        nwp_forecasts table (nwp_forecasts.db)
  - Market (3):     Kalshi order book API (live — noted as unavailable historically)
  - Daily (11):     metar_observations daily aggregates (metar_backfill.db)

Usage:
    python3 scripts/run_calibration_live.py
    python3 scripts/run_calibration_live.py --station KNYC
    python3 scripts/run_calibration_live.py --days 30
    python3 scripts/run_calibration_live.py --output data/calibration_live_signals.json
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

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('run_calibration_live')

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

# Signals that require live Kalshi order book data (cannot be tested historically)
MARKET_SIGNALS = {'settlement_arbitrage', 'spread_based_entry', 'volume_momentum'}

# Signals that require NWP forecasts DB (nwp_forecasts.db)
NWP_SIGNALS = {'nwp_direct', 'temperature_advection', 'esdr'}

# Signals that use both metar_observations and nwp_forecasts
FUSION_SIGNALS = {'nwp_dtdt_fusion', 'hrrr_bias_corrected'}

# Intraday signals that query metar_observations with time-window filters
INTRADAY_SIGNALS = {
    'fogr_reversion', 'metar_dtdt', 'intraday_metar_confirmation',
    'pressure_tendency', 'frontal_detector',
}


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
            conn.close()
        except Exception as e:
            status['nwp_db'] = f"error: {e}"
    else:
        status['nwp_db'] = False

    return status


def get_date_range(days_back: int = 60) -> List[str]:
    """
    Generate a list of ISO date strings for the last N days.
    Defaults to 60 days of historical lookback (matches typical calibration window).
    """
    dates = []
    today = datetime.now(timezone.utc)
    for i in range(days_back, 0, -1):
        d = today - timedelta(days=i)
        dates.append(d.strftime('%Y-%m-%d'))
    return dates


def evaluate_signal_for_station(
    signal_obj,
    signal_name: str,
    station: str,
    date: str,
    metar_conn: sqlite3.Connection,
    nwp_conn: sqlite3.Connection,
) -> Dict:
    """
    Call evaluate_for_station() on a signal with the appropriate DB connection.

    Different signals have different data source requirements:
    - Intraday signals: need metar_conn (metar_observations)
    - NWP signals: self-resolve nwp_forecasts.db internally
    - Fusion signals: need metar_conn (sub-signals self-resolve NWP)
    - Daily signals: can use metar_conn (base class queries daily aggregates)
    - Market signals: require live Kalshi API (noted as unavailable)

    Returns:
        dict with signal_name, station, date, direction, confidence, source
    """
    try:
        # Determine which connection to pass
        conn_to_pass = metar_conn

        # For NWP-only signals, the signal class resolves its own DB path
        # For fusion signals, they need metar_conn for the metar sub-signal
        # For market signals, they need metar_conn but also live Kalshi API

        # Call evaluate_for_station with the appropriate signature
        # Most signals accept: evaluate_for_station(station, date, conn=conn)
        # Some accept additional kwargs: market_type, lookahead_hours

        if signal_name == 'fogr_reversion':
            direction, confidence = signal_obj.evaluate_for_station(
                station, date, conn=metar_conn, market_type='HIGH'
            )
        elif signal_name in ('nwp_direct', 'esdr'):
            direction, confidence = signal_obj.evaluate_for_station(
                station, date, market_type='HIGH', conn=metar_conn
            )
        elif signal_name in ('nwp_dtdt_fusion',):
            direction, confidence = signal_obj.evaluate_for_station(
                station, date, conn=metar_conn, market_type='HIGH'
            )
        elif signal_name == 'hrrr_bias_corrected':
            direction, confidence = signal_obj.evaluate_for_station(
                station, date, conn=metar_conn, market_type='HIGH', lookahead_hours=3
            )
        elif signal_name in MARKET_SIGNALS:
            # Market signals require live Kalshi API — attempt evaluation
            # but catch the expected failure gracefully
            direction, confidence = signal_obj.evaluate_for_station(
                station, date, conn=metar_conn
            )
        else:
            # Standard signals: use base class evaluate_for_station
            direction, confidence = signal_obj.evaluate_for_station(
                station, date, conn=metar_conn
            )

        result = {
            'signal': signal_name,
            'station': station,
            'date': date,
            'direction': direction,
            'confidence': round(confidence, 4) if confidence is not None else None,
            'fired': direction is not None and confidence > 0,
        }
        return result

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
    days_back: int = 60,
    output_path: str = 'data/calibration_live_signals.json',
) -> Dict:
    """
    Run live calibration for all 23 signals across all stations.

    For each signal and each station, evaluates the signal on a range of
    historical dates by calling evaluate_for_station() with the live DB.

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
    registry = SignalRegistry(db_path)
    all_signals = registry.get_all_signals()
    signal_names = sorted(all_signals.keys())

    logger.info(f"Signal registry loaded: {len(signal_names)} signals")
    logger.info(f"  Intraday signals (6):     {[s for s in signal_names if s in INTRADAY_SIGNALS or s in ('hrrr_bias_corrected', 'nwp_dtdt_fusion')]}")
    logger.info(f"  NWP signals (3):          {[s for s in signal_names if s in NWP_SIGNALS]}")
    logger.info(f"  Market signals (3):       {[s for s in signal_names if s in MARKET_SIGNALS]}")
    logger.info(f"  Daily signals (11):       {[s for s in signal_names if s not in INTRADAY_SIGNALS and s not in NWP_SIGNALS and s not in MARKET_SIGNALS and s not in FUSION_SIGNALS]}")

    # ── Determine stations and dates ─────────────────────────────────────
    stations = [station_filter.upper()] if station_filter else STATIONS
    dates = get_date_range(days_back)

    logger.info(f"Stations: {len(stations)} ({', '.join(stations)})")
    logger.info(f"Date range: {dates[0]} to {dates[-1]} ({len(dates)} days)")

    # ── Evaluate each signal ─────────────────────────────────────────────
    results = []
    signal_summary = defaultdict(lambda: {'fired': 0, 'total': 0, 'errors': 0})
    errors_list = []

    for signal_name in signal_names:
        signal_obj = all_signals[signal_name]
        logger.info(f"\n{'='*60}")
        logger.info(f"Signal: {signal_name}")

        for station in stations:
            station_fired = 0
            station_total = 0
            station_errors = 0

            for date in dates:
                result = evaluate_signal_for_station(
                    signal_obj, signal_name, station, date,
                    metar_conn, nwp_conn,
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

            if station_fired > 0:
                logger.info(f"  {station}: {station_fired}/{station_total} fired "
                           f"({station_errors} errors)")

        total_fired = signal_summary[signal_name]['fired']
        total_attempts = signal_summary[signal_name]['total']
        total_errs = signal_summary[signal_name]['errors']
        logger.info(f"  => Total: {total_fired}/{total_attempts} fired "
                   f"({total_errs} errors)")

        if total_fired == 0 and signal_name in MARKET_SIGNALS:
            logger.info(f"  => NOTE: {signal_name} requires live Kalshi API. "
                       f"Cannot be tested historically. "
                       f"See KALSHI_API_INTEGRATION_PROGRESS.md for details.")

    metar_conn.close()
    if nwp_conn:
        nwp_conn.close()

    # ── Build per-signal calibration report ──────────────────────────────
    signal_reports = []
    for signal_name in signal_names:
        summary = signal_summary[signal_name]
        signal_results = [r for r in results if r['signal'] == signal_name]

        # Count correct predictions (where direction matches actual)
        # Note: We don't have ground truth here, so we just report fired counts
        fired_results = [r for r in signal_results if r['fired']]

        # Aggregate per-station stats
        per_station = defaultdict(lambda: {'fired': 0, 'total': 0, 'errors': 0})
        for r in signal_results:
            st = r['station']
            per_station[st]['total'] += 1
            if r['fired']:
                per_station[st]['fired'] += 1
            if r.get('error'):
                per_station[st]['errors'] += 1

        # Calculate confidence stats
        confidences = [r['confidence'] for r in fired_results if r['confidence'] is not None]

        report = {
            'signal': signal_name,
            'total_attempts': summary['total'],
            'total_fired': summary['fired'],
            'total_errors': summary['errors'],
            'fire_rate_pct': round(summary['fired'] / max(summary['total'], 1) * 100, 2),
            'avg_confidence': round(sum(confidences) / max(len(confidences), 1), 4) if confidences else None,
            'max_confidence': round(max(confidences), 4) if confidences else None,
            'data_source': _classify_signal_data_source(signal_name),
            'per_station': dict(per_station),
        }

        if signal_name in MARKET_SIGNALS:
            report['note'] = (
                "Requires live Kalshi API (order book data). "
                "Cannot be evaluated historically without API access. "
                "See KALSHI_API_INTEGRATION_PROGRESS.md for integration status."
            )

        signal_reports.append(report)

    # ── Aggregate summary ────────────────────────────────────────────────
    total_fired_all = sum(s['fired'] for s in signal_summary.values())
    total_attempts_all = sum(s['total'] for s in signal_summary.values())
    signals_with_trades = [s for s, v in signal_summary.items() if v['fired'] > 0]
    signals_with_errors = [s for s, v in signal_summary.items() if v['errors'] > 0]

    aggregate = {
        'total_signals': len(signal_names),
        'signals_with_trades': len(signals_with_trades),
        'signals_with_zero_trades': len(signal_names) - len(signals_with_trades),
        'signals_with_errors': len(signals_with_errors),
        'total_attempts': total_attempts_all,
        'total_fired': total_fired_all,
        'overall_fire_rate_pct': round(total_fired_all / max(total_attempts_all, 1) * 100, 2),
        'signals_with_trades_list': sorted(signals_with_trades),
        'signals_with_zero_trades_list': sorted(
            s for s in signal_names if signal_summary[s]['fired'] == 0
        ),
        'market_signals_require_live_api': sorted(MARKET_SIGNALS),
    }

    # ── Build output ─────────────────────────────────────────────────────
    output = {
        'metadata': {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'script': 'run_calibration_live.py',
            'db_path': METAR_DB,
            'nwp_db_path': NWP_DB,
            'stations': stations,
            'date_range': f"{dates[0]} to {dates[-1]} ({len(dates)} days)",
            'db_status': db_status,
        },
        'signal_results': signal_reports,
        'aggregate': aggregate,
        'errors': errors_list,
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
    print(f"B-Mode 1: Live Calibration Summary")
    print(f"{'='*60}")
    print(f"  Signals with trades: {len(signals_with_trades)}/{len(signal_names)}")
    print(f"  Signals with zero trades: {len(signal_names) - len(signals_with_trades)}")
    print(f"  Total signal evaluations: {total_attempts_all}")
    print(f"  Total signal fires: {total_fired_all}")
    print(f"  Overall fire rate: {aggregate['overall_fire_rate_pct']}%")
    print()
    print(f"  Signals with trades:")
    for s in sorted(signals_with_trades):
        v = signal_summary[s]
        print(f"    {s:>30}: {v['fired']:>6} fires / {v['total']:>6} attempts")
    print()
    print(f"  Signals with zero trades:")
    for s in sorted(s for s in signal_names if signal_summary[s]['fired'] == 0):
        v = signal_summary[s]
        note = " [LIVE KALSHI API REQUIRED]" if s in MARKET_SIGNALS else ""
        print(f"    {s:>30}: {v['errors']:>6} errors{note}")
    print()
    print(f"  Market signals requiring live Kalshi API:")
    for s in sorted(MARKET_SIGNALS):
        print(f"    {s}")
    print(f"{'='*60}")

    return output


def _classify_signal_data_source(signal_name: str) -> str:
    """Classify the data source required by a signal."""
    if signal_name in MARKET_SIGNALS:
        return 'kalshi_api'
    if signal_name in NWP_SIGNALS:
        return 'nwp_forecasts'
    if signal_name in FUSION_SIGNALS:
        return 'nwp_forecasts + metar_observations'
    if signal_name in INTRADAY_SIGNALS:
        return 'metar_observations (intraday)'
    if signal_name in ('seasonal_regime', 'corrected_pressure_delta'):
        return 'dual_polarity (daily metar)'
    return 'metar_observations (daily aggregates)'


# ── CLI ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description='B-Mode 1: Live calibration for all 23 signals',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 scripts/run_calibration_live.py
  python3 scripts/run_calibration_live.py --station KNYC
  python3 scripts/run_calibration_live.py --days 30
  python3 scripts/run_calibration_live.py --output data/calibration_live_signals.json
        """,
    )
    parser.add_argument('--db-path', type=str, default=METAR_DB,
                        help='Path to METAR SQLite database')
    parser.add_argument('--nwp-db-path', type=str, default=NWP_DB,
                        help='Path to NWP forecasts SQLite database')
    parser.add_argument('--station', type=str, default=None,
                        help='Single station to test (default: all 20)')
    parser.add_argument('--days', type=int, default=60,
                        help='Number of days of historical data to evaluate (default: 60)')
    parser.add_argument('--output', type=str, default='data/calibration_live_signals.json',
                        help='Output path for calibration results JSON')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Enable debug logging')

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger('run_calibration_live').setLevel(logging.DEBUG)

    run_calibration(
        db_path=args.db_path,
        nwp_db_path=args.nwp_db_path,
        station_filter=args.station,
        days_back=args.days,
        output_path=args.output,
    )


if __name__ == '__main__':
    main()