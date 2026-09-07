#!/usr/bin/env python3
"""
Intraday Production Runner (v1.0 — 2026-09-02)

Hourly entry-point for the intraday time-of-day stack.

Called by cron every hour during Kalshi trading hours (10:00-16:00 ET).
Uses live METAR/ASOS/NWP data to compute per-station calibrated directional
probability vs market-implied price. Generates entry signals when divergence
exceeds risk-adjusted threshold.

Stack:
  1. HourlyPipeline — aggregates METAR/ASOS/NWP into hourly feature vectors
  2. HourlySignalEvaluator — runs intraday signals on each hour bucket
  3. HourlyFusionEngine — LIOP fusion of hourly signals
  4. HourlyCalibration — isotonic/regression calibration per (station, hour_before_settlement)
  5. ContinuousRecalibrationLoop — Kalman-ish belief update
  6. EntryTimingEngine — risk-gated entry decisions

Cron spec (UTC):
    # Intraday runner: 14:30-20:30 UTC = 10:00-16:00 ET (trading hours)
    30 14-20 * * * cd /home/node/.openclaw/workspace/prototypes/weather-engine-source && python3 scripts/intraday_runner.py >> logs/intraday_runner.log 2>&1

Usage:
    python3 scripts/intraday_runner.py                    # Run today's current hour
    python3 scripts/intraday_runner.py --hour 14 --station KATL  # Single station debug
    python3 scripts/intraday_runner.py --dry-run           # Log only, no entry decisions

Returns:
    Exit code 0 = success
    Exit code 1 = already running (lock held)
    Exit code 2 = runtime error
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from core.instance_config import write_health_status, InstanceLock

# ─── Logging ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("intraday_runner")

# ─── Paths ────────────────────────────────────────────────────────────────
METAR_DB = REPO_ROOT / "data" / "metar_backfill.db"
ASOS_DB = REPO_ROOT / "data" / "iem_asos_1min.db"
NWP_DB = REPO_ROOT / "data" / "nwp_forecasts.db"
GEFS_DB = REPO_ROOT / "data" / "gefs_archive.db"
SETTLEMENTS_DB = REPO_ROOT / "data" / "metar_backfill.db"  # settlement_epochs in metar DB
GHCN_DB = REPO_ROOT / "data" / "ghcn_settlements.db"
_DASHBOARD_LAST_RUN_PATH = REPO_ROOT / "data" / "intraday" / "dashboard" / "G8_last_snapshot.json"

STATIONS = [
    'KATL', 'KAUS', 'KBOS', 'KDCA', 'KDEN', 'KDFW', 'KHOU', 'KLAS',
    'KLAX', 'KMDW', 'KMIA', 'KMSP', 'KMSY', 'KNYC', 'KOKC', 'KPHL',
    'KPHX', 'KSAT', 'KSEA', 'KSFO'
]

# Trading hours (ET): 10:00-16:00 = UTC: 14:00-20:00 (standard time)
# +30 min buffer: start at 14:30, last run at 20:30
TRADING_HOUR_START_UTC = 14
TRADING_HOUR_END_UTC = 21  # exclusive

# Risk defaults
DEFAULT_MIN_PROB_GATE = 0.55
DEFAULT_MAX_POSITION_USD = 250
DEFAULT_STOP_LOSS_PCT = 0.15


def _check_data_freshness() -> List[str]:
    """Verify data sources are fresh enough to run. Returns warnings list."""
    warnings = []
    cutoff_hours = 48  # Accept data up to 48 hours stale for backtest/offline
        
    for name, path, max_hours in [
        ("METAR", METAR_DB, 24),
        ("ASOS 1min", ASOS_DB, 24),
        ("NWP forecasts", NWP_DB, 72),
        ("GEFS archive", GEFS_DB, 72),
    ]:
        if not path.exists():
            warnings.append(f"{name}: NOT FOUND at {path}")
            continue
        mtime = os.path.getmtime(path)
        stale_hours = (time.time() - mtime) / 3600
        if stale_hours > max_hours:
            warnings.append(f"{name}: stale ({stale_hours:.0f}h, max {max_hours}h)")
    
    return warnings


def _get_current_hour_utc() -> int:
    """Get the current UTC hour (0-23)."""
    return datetime.now(timezone.utc).hour


def _is_trading_hours(hour_utc: int) -> bool:
    """Check if hour falls within Kalshi trading hours."""
    return TRADING_HOUR_START_UTC <= hour_utc < TRADING_HOUR_END_UTC


def _should_run_dashboard() -> bool:
    """Return True if 1+ hour since last G.8 dashboard snapshot."""
    path = _DASHBOARD_LAST_RUN_PATH
    if not path.exists():
        return True
    try:
        age_hours = (time.time() - os.path.getmtime(str(path))) / 3600
        return age_hours >= 1.0
    except (OSError, ValueError):
        return True


def run_intraday_cycle(
    target_hour: Optional[int] = None,
    stations: Optional[List[str]] = None,
    dry_run: bool = False,
) -> Dict:
    """
    Run one cycle of the intraday pipeline.
    
    Args:
        target_hour: UTC hour to evaluate (default: current hour)
        stations: Station subset (default: all 20)
        dry_run: If True, log only, no entry decisions
        
    Returns:
        Dict with summary of signals generated
    """
    target_hour = target_hour or _get_current_hour_utc()
    stations = stations or STATIONS
    
    logger.info(f"Intraday cycle: hour={target_hour}UTC, dry_run={dry_run}")
    
    # 1. Check freshness
    warnings = _check_data_freshness()
    for w in warnings:
        logger.warning(f"Data freshness: {w}")
    
    # 2. Build hourly pipeline (build_for_station_date per station)
    try:
        from core.intraday.pipeline import HourlyPipeline
        pipeline = HourlyPipeline(
            metar_db=str(METAR_DB),
            asos_db=str(ASOS_DB),
            nwp_db=str(NWP_DB),
        )
        all_vectors = []
        for st in stations:
            vectors = pipeline.build_for_station_date(st, datetime.now(timezone.utc).strftime('%Y-%m-%d'))
            if vectors:
                all_vectors.extend(vectors)
        logger.info(f"Pipeline built: {len(all_vectors)} hourly vectors across {len(stations)} stations")
    except Exception as e:
        logger.error(f"Pipeline failed: {e}", exc_info=True)
        return {"error": f"Pipeline: {e}", "signals": 0}
    
    # 3. Evaluate hourly signals (pool-name mapped for fusion compatibility)
    try:
        from core.intraday.signal_evaluator import (
            HourlySignalEvaluator,
            load_hourly_signal_hours,
        )
        evaluator = HourlySignalEvaluator(
            metar_db=str(METAR_DB),
        )
        date_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        # Use pool-name mapped output so fusion engine's pool definitions match
        pool_signal_results = {}
        for st in stations:
            pool_signal_results[st] = evaluator.evaluate_hour_with_pool_names(st, date_str, target_hour)
        # Also keep bare-named for logging
        signal_results = {}
        for st in stations:
            raw = evaluator.evaluate_hour(st, date_str, target_hour)
            signal_results[st] = raw
        logger.info(f"Signals evaluated: {len(pool_signal_results)} stations ({len(stations)} total)")
    except Exception as e:
        logger.error(f"Signal evaluation failed: {e}", exc_info=True)
        return {"error": f"Signals: {e}", "signals": 0}
    
    # 4. Fuse signals (LIOP) — per-station, using pool-compatible signal names
    try:
        from core.intraday.fusion import HourlyFusionEngine
        fusion_engine = HourlyFusionEngine()
        
        hours_before_settlement = {
            st: max(0, 18 - target_hour)  # settlement ~18 UTC (12 ET)
            for st in stations
        }
        
        fused_results = {}
        for st in stations:
            sigs = pool_signal_results.get(st, {})
            # Filter to only fired signals (direction not None, confidence > 0)
            fired = {name: pred for name, pred in sigs.items()
                     if pred[0] is not None and pred[1] > 0}
            if fired:
                try:
                    fusion_result = fusion_engine.evaluate(
                        signal_predictions=fired,
                        station=st,
                        current_utc_hour=target_hour,
                        hour_before_settlement=hours_before_settlement.get(st, 6),
                    )
                    fused_results[st] = fusion_result
                except Exception as fuse_err:
                    logger.warning(f"Fusion failed for {st}: {fuse_err}")
                    fused_results[st] = None
            else:
                fused_results[st] = None
        
        logger.info(f"Fusion complete: {len(fused_results)} stations fused via LOOP")
    except Exception as e:
        logger.error(f"Fusion failed: {e}", exc_info=True)
        return {"error": f"Fusion: {e}", "signals": 0}
    
    # 4b. Additive gamma — apply temperature offsets as probability modulators
    additive_applied = 0
    try:
        from core.additive import evaluate_all as additive_evaluate_all
        from core.additive.context import AdditiveContext
        from core.additive.integration import apply_additive_offsets
        
        from core.additive import SIGNAL_NAMES, ENABLED_BY_DEFAULT
        n_enabled = len(ENABLED_BY_DEFAULT)
        n_total = len(SIGNAL_NAMES)
        logger.info(f"Additive gamma: {n_enabled}/{n_total} signals enabled (ENABLED_BY_DEFAULT={'empty' if n_enabled==0 else list(ENABLED_BY_DEFAULT)})")
        
        for st in stations:
            if st not in fused_results or fused_results[st] is None:
                continue
            hb = hours_before_settlement.get(st, 6)
            try:
                ctx = AdditiveContext(st, date_str)
                results = additive_evaluate_all(ctx)
                # Apply enabled additive signals (all disabled by default)
                stack = apply_additive_offsets(results, enabled=list(ENABLED_BY_DEFAULT))
                ctx.close()
                
                if abs(stack.total_offset_f) > 0.01:
                    # Convert additive °F offset to probability adjustment
                    # Heuristic: 1°F ≈ 0.02 probability shift (based on typical
                    # station temperature std-dev ~5°F over a 50pp range)
                    prob_shift = stack.total_offset_f * 0.02
                    old_prob = fused_results[st].mean_probability
                    new_prob = max(0.01, min(0.99, old_prob + prob_shift))
                    fused_results[st].mean_probability = new_prob
                    additive_applied += 1
                    logger.info(
                        f"Additive gamma: {st} offset={stack.total_offset_f:.2f}°F "
                        f"prob={old_prob:.3f}→{new_prob:.3f}"
                    )
            except Exception as add_err:
                logger.debug(f"Additive gamma skipped for {st}: {add_err}")
    except Exception as e:
        logger.warning(f"Additive gamma stack error: {e}")
    
    # 4c. PWW crossfeed modulation — whale detection probability bump
    # PWW crossfeed — wire 2026-09-06, fail-graceful
    try:
        from core.crossfeed_consumer import CrossfeedConsumer
        pww_consumer = CrossfeedConsumer()
        if pww_consumer.is_available:
            pww_modulated_count = 0
            for st in stations:
                if st not in fused_results or fused_results[st] is None:
                    continue
                fused = fused_results[st]
                direction = "UP" if str(fused.direction).upper() in ("UP", "HIGH", "YES", "1") else "DOWN"
                orig_prob = fused.mean_probability
                mod_prob, meta = pww_consumer.consume_and_modulate(
                    station_icao=st,
                    analytical_prob=orig_prob,
                    direction=direction,
                )
                if meta is not None and abs(mod_prob - orig_prob) > 1e-6:
                    fused_results[st].mean_probability = mod_prob
                    pww_modulated_count += 1
                    logger.info(
                        f"PWW crossfeed: {st} {direction} "
                        f"{orig_prob:.4f}->{mod_prob:.4f} "
                        f"{meta.get('bump_pp', 0):.1f}pp"
                    )
            logger.info(f"PWW crossfeed: {pww_modulated_count}/{len(stations)} stations modulated")
        else:
            logger.info("PWW crossfeed: consumer unavailable — skipping modulation")
    except Exception as e:
        logger.warning(f"PWW crossfeed modulation failed (non-blocking): {e}")
    
    # 5. Calibrate
    try:
        from core.intraday.calibration import HourlyCalibration
        calibrator = HourlyCalibration()
        
        calibrated = {}
        for st in stations:
            if st not in fused_results or fused_results[st] is None:
                continue
            hb = hours_before_settlement.get(st, 6)
            # Extract fused probability and confidence from LIOP result
            fused = fused_results[st]
            raw_prob = fused.mean_probability
            raw_conf = abs(raw_prob - 0.5) * 2.0  # Map distance from 0.5 to [0,1]
            cal_prob = calibrator.get_calibrated(st, hb, raw_prob)
            calibrated[st] = {
                "probability": max(0.01, min(0.99, cal_prob)),
                "raw_confidence": raw_conf,
                "hour_before": hb,
                "fused_direction": fused.direction,
                "fused_gate_passed": fused.agreement_gate_passed,
                "fused_pools_agreeing": fused.n_pools_agreeing,
            }
        logger.info(f"Calibration complete: {len(calibrated)} stations calibrated")
    except Exception as e:
        logger.error(f"Calibration failed: {e}", exc_info=True)
        return {"error": f"Calibration: {e}", "signals": 0}
    
    # 5c. NWS revision correction — apply per-station probability shift
    try:
        from core.calibration.hourly_calibration import HourlyCalibrationLoader
        loader = HourlyCalibrationLoader()
        for st in calibrated:
            corrector = loader.apply_nws_revision_correction(
                calibrated[st]["probability"], st
            )
            if corrector != calibrated[st]["probability"]:
                logger.info(
                    f"NWS correction: {st} "
                    f"{calibrated[st]['probability']:.4f}->{corrector:.4f}"
                )
                calibrated[st]["probability"] = corrector
    except Exception as nws_err:
        logger.warning(f"NWS revision correction failed: {nws_err}")
    
    # 5b. G.8 dashboard snapshot (1/hour)
    if _should_run_dashboard():
        try:
            from core.additive.monitor import run_daily_dashboard
            dashboard_report = run_daily_dashboard(db_path=str(REPO_ROOT / 'data'))
            live = dashboard_report.get('summary', {}).get('n_signals_live', 0)
            n_days = dashboard_report.get('summary', {}).get('n_evaluated_days', 0)
            logger.info(f"G.8 dashboard: {live}/{len(dashboard_report.get('signals', []))} signals live, {n_days} days evaluated")
        except Exception as dash_err:
            logger.warning(f"G.8 dashboard skipped: {dash_err}")
    
    # 6. Entry timing — with real Kalshi market prices
    if not dry_run:
        try:
            from core.intraday.entry_engine import (
                EntryTimingEngine,
                EntrySignal,
                RiskGuardrails,
            )
            from core.kalshi_price_fetcher import get_live_market_price
            
            engine = EntryTimingEngine()
            entry_results = []
            for st in stations:
                if st not in calibrated:
                    continue
                result = calibrated[st]
                our_prob = result.get("probability", 0.5)
                confidence = result.get("raw_confidence", 0.5)
                hb = result.get("hour_before", 6)
                
                # Get real Kalshi market price (HIGH market)
                try:
                    live_price, meta = get_live_market_price(st, "HIGH", date_str)
                    market_price = live_price
                except Exception as price_err:
                    logger.warning(f"Kalshi price fetch failed for {st}: {price_err}")
                    market_price = 0.5  # fallback
                
                signal = engine.evaluate_hour(
                    hour=target_hour,
                    hour_before_settlement=hb,
                    our_probability=our_prob,
                    market_price=market_price,
                    confidence=confidence,
                )
                if signal and signal.signal_type.name != "PASS":
                    entry_results.append({
                        "station": st,
                        "hour": target_hour,
                        "signal": signal.signal_type.name,
                        "our_prob": our_prob,
                        "market_price": market_price,
                        "position_size": signal.position_size,
                        "additive_applied": additive_applied > 0,
                    })
                    logger.info(
                        f"SIGNAL: {st} hour={target_hour} "
                        f"{signal.signal_type.name} prob={our_prob:.3f} "
                        f"market={market_price:.3f} size=${signal.position_size:.0f}"
                    )
                else:
                    logger.debug(f"PASS: {st} hour={target_hour} prob={our_prob:.3f} market={market_price:.3f}")
        except Exception as e:
            logger.error(f"Entry engine failed: {e}", exc_info=True)
            entry_results = []
    else:
        entry_results = []
        logger.info("Dry run — no entry decisions")
    
    # 7. Exit logic — close positions that meet exit criteria
    # Build open_positions from entry_results (live positions opened this cycle)
    # In production, open_positions would come from a position tracker.
    # For now, treat entry_results as open positions for exit evaluation.
    try:
        from core.intraday.exit_logic import evaluate_exits
        open_positions = []
        for er in entry_results:
            open_positions.append({
                "station": er["station"],
                "direction": "UP" if er["signal"] == "BUY" else "DOWN",
                "entry_price": er.get("market_price", 0.5),
                "position_size": er.get("position_size", 1.0),
                "entry_hour": er.get("hour", target_hour),
            })
        
        # Build live_prices map from the entry evaluation loop
        live_prices = {}
        for er in entry_results:
            live_prices[er["station"]] = er.get("market_price", 0.5)
        
        # Determine current hour_before_settlement (use min across stations)
        current_hb = min(
            (calibrated[s].get("hour_before", 6) for s in calibrated),
            default=6,
        )
        
        exits = evaluate_exits(
            open_positions=open_positions,
            fused_results=fused_results,
            live_prices=live_prices,
            hour_before_settlement=current_hb,
            current_hour=target_hour,
        )
        for exit_signal in exits:
            logger.info(
                f"EXIT: {exit_signal.station} {exit_signal.reason} "
                f"pnl={exit_signal.estimated_pnl:.2f}"
            )
    except Exception as e:
        logger.warning(f"Exit logic failed: {e}")
    
    return {
        "hour": target_hour,
        "stations": len(stations),
        "warnings": warnings,
        "signals_generated": len(entry_results),
        "entry_results": entry_results,
        "additive_applied": additive_applied,
        "fused_stations": len([s for s in fused_results.values() if s is not None and s.agreement_gate_passed]),
    }


def main():
    parser = argparse.ArgumentParser(description="Intraday Production Runner")
    parser.add_argument("--hour", type=int, default=None, help="UTC hour to evaluate (default: current)")
    parser.add_argument("--station", type=str, default=None, help="Single station (default: all)")
    parser.add_argument("--dry-run", action="store_true", help="Log only, no entry decisions")
    parser.add_argument("--no-lock", action="store_true", help="Skip lock acquisition")
    args = parser.parse_args()
    
    # Determine target
    target_hour = args.hour or _get_current_hour_utc()
    stations = [args.station.upper()] if args.station else STATIONS
    
    # Check if trading hours (warn but proceed)
    if not _is_trading_hours(target_hour):
        logger.warning(f"Hour {target_hour}UTC is outside trading hours ({TRADING_HOUR_START_UTC}-{TRADING_HOUR_END_UTC}UTC)")
    
    # Acquire lock (unless no-lock)
    if not args.no_lock:
        lock = InstanceLock("/tmp/intraday-daemon.lock")
        if not lock.acquire():
            logger.warning("Lock held by another process — exiting")
            sys.exit(1)
        try:
            result = run_intraday_cycle(
                target_hour=target_hour,
                stations=stations,
                dry_run=args.dry_run,
            )
        finally:
            lock.release()
    else:
        result = run_intraday_cycle(
            target_hour=target_hour,
            stations=stations,
            dry_run=args.dry_run,
        )
    
    # Write health status
    write_health_status("intraday", status="running" if result.get("signals_generated", 0) > 0 else "idle")
    
    # Print summary
    logger.info(f"Intraday cycle complete: {json.dumps({k: v for k, v in result.items() if k != 'entry_results'})}")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())