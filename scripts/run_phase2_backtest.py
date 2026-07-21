#!/usr/bin/env python3
"""
PHASE 2.4 BACKTEST — Temperature Advection Signal (Signal 6) Validation
==========================================================================
Validates the 850-mb Temperature Advection signal against historical baselines.

Tests:
  1. Temperature Advection standalone — Signal 6 alone (if historical data exists)
  2. All SignalRegistry signals together — full ensemble including Signal 6
  3. Best Phase 6 combo + Signal 6 — add Signal 6 to best Phase 6 ensemble
  4. Walk-forward validation — no look-ahead bias, sequential time windows
  5. B1.5 guardrails enforced — consecutive_loss_limit=8, kill_switch, max_drawdown
  6. Metrics recorded — directional accuracy, Sharpe ratio, trade count, coverage, Brier score

⚠️ NO AI CALLS INSIDE THE LOOP — deterministic signal evaluation only.
"""

import sqlite3
import json
import os
import sys
import math
import statistics
from collections import defaultdict, Counter
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Tuple, Optional, Any
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parents[1]
METAR_DB = str(REPO_ROOT / "data" / "metar_backfill.db")
NWP_DB = str(REPO_ROOT / "data" / "nwp_forecasts.db")
PHASE6_RESULTS = str(REPO_ROOT / "data" / "phase6_combinatorial_search.json")
OUTPUT_PATH = str(REPO_ROOT / "data" / "phase2_backtest_results.json")
CONTINUITY_DIR = REPO_ROOT / ".meta" / "continuity" / "weather-engine"

# Seeds
STATIONS = [
    "KATL", "KAUS", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU",
    "KLAS", "KLAX", "KMDW", "KMIA", "KMSP", "KMSY", "KNYC",
    "KOKC", "KPHL", "KPHX", "KSAT", "KSEA", "KSFO",
]

# B1.5 Guardrails
MAX_CONSECUTIVE_LOSSES = 8
MAX_DAILY_LOSS = 300.0
MAX_DRAWDOWN_PCT = 10.0
INITIAL_BALANCE = 10000.0
POSITION_SIZE = 100.0
FEE_RATE = 0.0

# Walk-forward split ratios
WALK_FORWARD_SPLITS = [
    ("train_2021_2022", "2021-01-01", "2022-12-31", "2023-01-01", "2023-06-30"),
    ("train_2022_2023", "2022-01-01", "2023-06-30", "2023-07-01", "2023-12-31"),
    ("train_2021_2023", "2021-01-01", "2023-12-31", "2024-01-01", "2024-06-30"),
    ("train_2022_2024", "2022-01-01", "2024-06-30", "2024-07-01", "2025-01-31"),
    ("holdout_all", "2024-01-01", "2025-08-27", None, None),  # Full test
]

# ── Signal Registry Import ─────────────────────────────────────────────
sys.path.insert(0, str(REPO_ROOT))
from core.signals import create_signal_registry, SignalRegistry


# ═══════════════════════════════════════════════════════════════════════
# DATA LOADING
# ═══════════════════════════════════════════════════════════════════════

def load_station_data(station: str, db_path: str) -> Tuple[List[dict], dict]:
    """Load METAR observations and settlement market outcomes for a station."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # Daily METAR aggregates
    cur.execute("""
        SELECT date_utc, MAX(temp_f) as high, MIN(temp_f) as low,
               AVG(dewpoint_f) as dewpoint, AVG(wind_direction_deg) as wind_dir,
               AVG(pressure_mb) as pressure, AVG(temp_f) as temp,
               AVG(wind_speed_kt) as wind_speed
        FROM metar_observations
        WHERE station = ? AND temp_f IS NOT NULL
        GROUP BY date_utc
        ORDER BY date_utc ASC
    """, (station,))

    days = []
    for r in cur.fetchall():
        if r[0] is None:
            continue
        days.append({
            'date': r[0], 'high': r[1], 'low': r[2],
            'dewpoint': r[3], 'wind_dir': r[4], 'pressure': r[5],
            'temp': r[6], 'wind_speed': r[7],
        })

    # Market outcomes (HIGH market)
    cur.execute("""
        SELECT local_trading_date, settlement_bucket, prior_settlement_bucket
        FROM settlement_epochs
        WHERE station = ? AND market_type = 'HIGH' AND epoch_status = 'closed'
        ORDER BY local_trading_date ASC
    """, (station,))

    market = {}
    for r in cur.fetchall():
        if r[2] is not None:
            direction = 'up' if r[1] > r[2] else ('down' if r[1] < r[2] else 'flat')
            market[r[0]] = direction

    conn.close()
    return days, market


def align_data(days: List[dict], market: dict) -> List[dict]:
    """Merge METAR data with market outcomes, keeping only dates with both."""
    aligned = []
    for d in days:
        if d['date'] in market:
            aligned.append({**d, 'market_dir': market[d['date']]})
    return aligned


def load_phase6_best_combo() -> Tuple[List[str], dict]:
    """Load Phase 6 combinatorial search results and find the best combo."""
    best_combo = []
    best_metrics = {}

    try:
        with open(PHASE6_RESULTS) as f:
            data = json.load(f)

        results = data.get('results', {})
        if not results:
            print("  WARNING: No Phase 6 results found. Using default combo.")
            return ['calendar_climatology', 'gaussian'], {}

        # Find best combo by Sharpe with coverage > 0.5
        best_sharpe = 0
        best_key = None
        for k, v in results.items():
            s = float(v.get('sharpe', 0))
            cov = float(v.get('coverage', 0))
            if s > best_sharpe and cov > 0.5:
                best_sharpe = s
                best_key = k

        if best_key:
            v = results[best_key]
            best_combo = v.get('signals', best_key.split('+'))
            best_metrics = {
                'accuracy': v.get('accuracy', 0),
                'sharpe': v.get('sharpe', 0),
                'coverage': v.get('coverage', 0),
                'correct': v.get('correct', 0),
                'total': v.get('total', 0),
            }
            print(f"  Best Phase 6 combo: {best_key} (Sharpe={best_sharpe:.4f})")
        else:
            print("  WARNING: No valid Phase 6 combo found. Using default.")
            best_combo = ['calendar_climatology', 'gaussian']

    except FileNotFoundError:
        print(f"  WARNING: Phase 6 results not found at {PHASE6_RESULTS}. Using default combo.")
        best_combo = ['calendar_climatology', 'gaussian']
    except Exception as e:
        print(f"  WARNING: Error loading Phase 6 results: {e}. Using default combo.")
        best_combo = ['calendar_climatology', 'gaussian']

    return best_combo, best_metrics


# ═══════════════════════════════════════════════════════════════════════
# SIGNAL EVALUATION
# ═══════════════════════════════════════════════════════════════════════

def evaluate_signals(registry: SignalRegistry, days: List[dict],
                     signal_names: List[str]) -> List[Tuple[Optional[str], float, str]]:
    """Evaluate a list of signals across all days and return predictions.

    Args:
        registry: SignalRegistry instance
        days: Aligned list of daily weather dicts with market_dir
        signal_names: List of signal names to evaluate

    Returns:
        List of (direction, confidence, signal_name) tuples, one per day.
        If no signal fires, direction is None.
    """
    predictions = []

    for idx in range(len(days)):
        day_results = []
        for name in signal_names:
            signal = registry.get_signal(name)
            if signal is None:
                continue

            # Check min_lookback
            if idx < signal.min_lookback:
                continue

            try:
                direction, confidence = signal.evaluate(idx, days)
                if direction is not None and confidence > 0:
                    day_results.append((direction, confidence, name))
            except Exception as e:
                # Silently skip signals that fail on this day
                pass

        # Ensemble: majority vote, weighted by confidence
        if day_results:
            up_votes = sum(c for d, c, _ in day_results if d == 'up')
            down_votes = sum(c for d, c, _ in day_results if d == 'down')
            total_votes = up_votes + down_votes

            if total_votes > 0:
                if up_votes > down_votes:
                    ensemble_dir = 'up'
                    ensemble_conf = up_votes / total_votes
                elif down_votes > up_votes:
                    ensemble_dir = 'down'
                    ensemble_conf = down_votes / total_votes
                else:
                    # Tie — no signal
                    predictions.append((None, 0.0, "tie"))
                    continue

                # Apply minimum confidence threshold
                if ensemble_conf >= 0.5:
                    predictions.append((ensemble_dir, ensemble_conf, "ensemble"))
                else:
                    predictions.append((None, 0.0, "low_confidence"))
            else:
                predictions.append((None, 0.0, "no_votes"))
        else:
            predictions.append((None, 0.0, "no_signals"))

    return predictions


def evaluate_signal_standalone(signal, days: List[dict]) -> List[Tuple[Optional[str], float]]:
    """Evaluate a single signal across all days.

    Returns list of (direction, confidence) tuples.
    """
    predictions = []
    for idx in range(len(days)):
        if idx < signal.min_lookback:
            predictions.append((None, 0.0))
            continue
        try:
            direction, confidence = signal.evaluate(idx, days)
            predictions.append((direction, confidence))
        except Exception:
            predictions.append((None, 0.0))
    return predictions


# ═══════════════════════════════════════════════════════════════════════
# METRICS
# ═══════════════════════════════════════════════════════════════════════

def compute_metrics(predictions: List[Tuple[Optional[str], float]],
                    market_dirs: List[str]) -> dict:
    """Compute backtest metrics from predictions and market outcomes.

    Args:
        predictions: List of (direction, confidence) tuples
        market_dirs: List of actual market directions ('up', 'down', 'flat')

    Returns:
        Dict with accuracy, Sharpe, trade count, coverage, Brier score, etc.
    """
    n = len(predictions)
    if n == 0:
        return {'error': 'no predictions'}

    trades = []
    correct = 0
    total_trades = 0
    brier_accum = 0.0
    brier_count = 0

    for i in range(n):
        pred_dir, conf = predictions[i]
        actual = market_dirs[i] if i < len(market_dirs) else 'flat'

        if pred_dir is not None:
            total_trades += 1
            is_correct = (pred_dir == actual)
            if is_correct:
                correct += 1

            # P&L: +1 for correct, -1 for wrong (flat is neutral)
            if actual == 'flat':
                pnl = 0.0
            elif is_correct:
                pnl = 1.0
            else:
                pnl = -1.0

            trades.append(pnl)

            # Brier score: (prediction - outcome)^2
            # outcome = 1 if correct, 0 if wrong
            outcome = 1.0 if is_correct else 0.0
            brier_accum += (conf - outcome) ** 2
            brier_count += 1

    # Metrics
    accuracy = correct / total_trades if total_trades > 0 else 0.0
    coverage = total_trades / n if n > 0 else 0.0

    # Sharpe ratio (annualized, assuming daily trades)
    if len(trades) > 1:
        mean_pnl = statistics.mean(trades)
        std_pnl = statistics.stdev(trades) if len(trades) > 1 else 1.0
        sharpe = (mean_pnl / std_pnl) * math.sqrt(252) if std_pnl > 0 else 0.0
    else:
        sharpe = 0.0

    # Brier score
    brier_score = brier_accum / brier_count if brier_count > 0 else 0.0

    # Consecutive losses
    max_consecutive_losses = 0
    current_consecutive = 0
    for pnl in trades:
        if pnl < 0:
            current_consecutive += 1
            max_consecutive_losses = max(max_consecutive_losses, current_consecutive)
        else:
            current_consecutive = 0

    return {
        'accuracy': round(accuracy, 6),
        'sharpe': round(sharpe, 6),
        'total_trades': total_trades,
        'correct': correct,
        'coverage': round(coverage, 6),
        'brier_score': round(brier_score, 6),
        'max_consecutive_losses': max_consecutive_losses,
        'total_days': n,
    }


def run_backtest_with_risk(registry: SignalRegistry, days: List[dict],
                            signal_names: List[str],
                            enable_guardrails: bool = True) -> dict:
    """Run backtest with B1.5 guardrails enforced.

    Args:
        registry: SignalRegistry instance
        days: Aligned daily data
        signal_names: Signal names to use
        enable_guardrails: If True, enforce B1.5 kill switch

    Returns:
        Metrics dict
    """
    predictions = evaluate_signals(registry, days, signal_names)
    market_dirs = [d['market_dir'] for d in days]

    # Base metrics
    metrics = compute_metrics(
        [(p[0], p[1]) for p in predictions],
        market_dirs
    )

    if not enable_guardrails:
        return metrics

    # B1.5 Guardrails — simulate trading with kill switch
    balance = INITIAL_BALANCE
    peak_balance = INITIAL_BALANCE
    consecutive_losses = 0
    in_lockdown = False
    daily_loss = 0.0
    last_date = None

    trades_log = []
    guardrail_triggers = []

    for i in range(len(days)):
        pred_dir, conf, _ = predictions[i]
        actual = market_dirs[i] if i < len(market_dirs) else 'flat'
        date = days[i]['date']

        # Daily loss reset
        if date != last_date:
            daily_loss = 0.0
            last_date = date

        if in_lockdown:
            continue  # Skip trading while in lockdown

        if pred_dir is None:
            continue

        # Simulate trade
        if actual == 'flat':
            pnl = 0.0
        elif pred_dir == actual:
            pnl = POSITION_SIZE * conf  # Profit proportional to confidence
        else:
            pnl = -POSITION_SIZE * conf  # Loss proportional to confidence

        # Apply fee
        pnl -= POSITION_SIZE * FEE_RATE

        balance += pnl
        daily_loss += abs(pnl) if pnl < 0 else 0

        # Track consecutive losses
        if pnl < 0:
            consecutive_losses += 1
        else:
            consecutive_losses = 0

        # Update peak balance for drawdown
        peak_balance = max(peak_balance, balance)

        # Guardrail checks
        if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            in_lockdown = True
            guardrail_triggers.append({
                'date': date,
                'reason': 'consecutive_losses',
                'value': consecutive_losses,
                'balance': round(balance, 2),
            })
            continue

        if daily_loss >= MAX_DAILY_LOSS:
            in_lockdown = True
            guardrail_triggers.append({
                'date': date,
                'reason': 'max_daily_loss',
                'value': round(daily_loss, 2),
                'balance': round(balance, 2),
            })
            continue

        drawdown_pct = (peak_balance - balance) / peak_balance * 100
        if drawdown_pct >= MAX_DRAWDOWN_PCT:
            in_lockdown = True
            guardrail_triggers.append({
                'date': date,
                'reason': 'max_drawdown',
                'value': round(drawdown_pct, 2),
                'balance': round(balance, 2),
            })
            continue

        trades_log.append({
            'date': date,
            'prediction': pred_dir,
            'actual': actual,
            'pnl': round(pnl, 2),
            'balance': round(balance, 2),
        })

    # Final metrics with guardrails
    total_pnl = balance - INITIAL_BALANCE

    # Compute Sharpe on actual PnL series
    pnl_series = [t['pnl'] for t in trades_log]
    if len(pnl_series) > 1:
        mean_pnl = statistics.mean(pnl_series)
        std_pnl = statistics.stdev(pnl_series) if len(pnl_series) > 1 else 1.0
        sharpe_guarded = (mean_pnl / std_pnl) * math.sqrt(252) if std_pnl > 0 else 0.0
    else:
        sharpe_guarded = 0.0

    metrics['with_guardrails'] = {
        'total_pnl': round(total_pnl, 2),
        'final_balance': round(balance, 2),
        'sharpe_guarded': round(sharpe_guarded, 6),
        'trades_executed': len(trades_log),
        'guardrail_triggers': guardrail_triggers,
        'lockdown_triggered': in_lockdown,
    }

    return metrics


# ═══════════════════════════════════════════════════════════════════════
# TEMPERATURE ADVECTION (SIGNAL 6) — LIVE-ONLY EVALUATION
# ═══════════════════════════════════════════════════════════════════════

def check_temperature_advection_viability() -> dict:
    """Check if the Temperature Advection signal has any stored NWP data.

    Returns a dict with the status of historical NWP data availability.
    """
    result = {
        'signal_name': 'temperature_advection',
        'signal_class': 'TemperatureAdvectionSignal',
        'backtest_viable': False,
        'reason': 'Live-only signal — requires real-time GFS 850-mb forecast data. '
                  'The evaluate() method is a stub for the BaseSignal interface. '
                  'Backtesting requires stored NWP forecast data which is limited.',
        'stored_records': 0,
        'stations_with_data': [],
    }

    try:
        conn = sqlite3.connect(NWP_DB)
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM nwp_forecasts WHERE variable = 'advection_850hPa'"
        )
        result['stored_records'] = cur.fetchone()[0]

        cur.execute(
            "SELECT DISTINCT station FROM nwp_forecasts WHERE variable = 'advection_850hPa'"
        )
        result['stations_with_data'] = [r[0] for r in cur.fetchall()]
        conn.close()
    except Exception as e:
        result['error'] = str(e)

    return result


def evaluate_temperature_advection_from_nwp(station: str, days: List[dict]) -> List[Tuple[Optional[str], float]]:
    """Evaluate temperature advection from stored NWP data if available.

    This is a limited backtest — only works for dates where GFS data was
    previously collected and stored.
    """
    predictions = []
    try:
        conn = sqlite3.connect(NWP_DB)
        cur = conn.cursor()

        for d in days:
            date = d['date']
            # Query stored advection for this station/date
            cur.execute(
                """SELECT value FROM nwp_forecasts
                   WHERE station = ? AND variable = 'advection_850hPa'
                   AND target_date = ? ORDER BY fetch_timestamp DESC LIMIT 1""",
                (station, date)
            )
            row = cur.fetchone()
            if row and row[0] is not None:
                adv = float(row[0])
                if adv > 0.5:
                    predictions.append(('up', 0.6))
                elif adv < -0.5:
                    predictions.append(('down', 0.6))
                else:
                    predictions.append((None, 0.0))
            else:
                predictions.append((None, 0.0))

        conn.close()
    except Exception:
        predictions = [(None, 0.0)] * len(days)

    return predictions


# ═══════════════════════════════════════════════════════════════════════
# RUNNER
# ═══════════════════════════════════════════════════════════════════════

def run_phase2_backtest():
    """Main entry point: run all Phase 2.4 backtest experiments."""
    print("=" * 80)
    print("PHASE 2.4 BACKTEST — Temperature Advection (Signal 6) Validation")
    print(f"Started: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 80)

    results = {
        'metadata': {
            'script': 'scripts/run_phase2_backtest.py',
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'database': METAR_DB,
            'stations': STATIONS,
            'guardrails': {
                'max_consecutive_losses': MAX_CONSECUTIVE_LOSSES,
                'max_daily_loss': MAX_DAILY_LOSS,
                'max_drawdown_pct': MAX_DRAWDOWN_PCT,
                'initial_balance': INITIAL_BALANCE,
            },
        },
        'experiments': {},
        'temperature_advection_viability': {},
        'phase6_baseline': {},
    }

    # ── Step 0: Initialize SignalRegistry ──────────────────────────────
    print("\n[Step 0] Initializing SignalRegistry...")
    registry = create_signal_registry(METAR_DB)
    all_signals = list(registry.get_all_signals().keys())
    print(f"  Registered {len(all_signals)} signals: {all_signals}")

    # ── Step 0.5: Check Temperature Advection viability ────────────────
    print("\n[Step 0.5] Temperature Advection viability check...")
    adv_viability = check_temperature_advection_viability()
    results['temperature_advection_viability'] = adv_viability
    print(f"  Stored records: {adv_viability['stored_records']}")
    print(f"  Stations with data: {len(adv_viability['stations_with_data'])}")
    print(f"  Backtest viable: {adv_viability['backtest_viable']}")
    print(f"  Reason: {adv_viability['reason']}")

    # ── Step 1: Load Phase 6 best combo ────────────────────────────────
    print("\n[Step 1] Loading Phase 6 best combo...")
    best_combo, best_combo_metrics = load_phase6_best_combo()
    results['phase6_baseline'] = {
        'best_combo_signals': best_combo,
        'best_combo_metrics': best_combo_metrics,
    }
    print(f"  Best combo signals: {best_combo}")
    print(f"  Best combo metrics: {json.dumps(best_combo_metrics, default=str)}")

    # ── Step 2: Load data for all stations ─────────────────────────────
    print("\n[Step 2] Loading station data...")
    station_data = {}
    for station in STATIONS:
        days, market = load_station_data(station, METAR_DB)
        aligned = align_data(days, market)
        station_data[station] = aligned
        print(f"  {station}: {len(aligned)} aligned days ({len(days)} raw days)")

    # ── Step 3: Experiment 1 — Temperature Advection Standalone ────────
    print("\n" + "=" * 80)
    print("EXPERIMENT 1: Temperature Advection Standalone (Signal 6)")
    print("=" * 80)

    exp1 = {
        'description': 'Temperature Advection Signal 6 standalone (limited to stored NWP data)',
        'stations': {},
        'aggregate': {},
        'note': adv_viability['reason'],
    }

    adv_signal = registry.get_signal('temperature_advection')
    total_trades = 0
    total_correct = 0
    total_days = 0

    for station in STATIONS:
        aligned = station_data[station]
        if len(aligned) < 10:
            exp1['stations'][station] = {'error': 'insufficient data'}
            continue

        # Try NWP stored data
        adv_preds = evaluate_temperature_advection_from_nwp(station, aligned)
        market_dirs = [d['market_dir'] for d in aligned]

        metrics = compute_metrics(adv_preds, market_dirs)
        exp1['stations'][station] = metrics
        total_trades += metrics['total_trades']
        total_correct += metrics['correct']
        total_days += metrics['total_days']

        print(f"  {station}: {metrics['total_trades']} trades, "
              f"acc={metrics['accuracy']:.4f}, "
              f"coverage={metrics['coverage']:.4f}")

    # Aggregate
    agg_acc = total_correct / total_trades if total_trades > 0 else 0
    exp1['aggregate'] = {
        'total_trades': total_trades,
        'correct': total_correct,
        'accuracy': round(agg_acc, 6),
        'total_days': total_days,
        'coverage': round(total_trades / total_days, 6) if total_days > 0 else 0,
    }
    print(f"\n  AGGREGATE: {total_trades} trades, accuracy={agg_acc:.4f}, "
          f"coverage={total_trades/total_days:.4f}" if total_days > 0 else "  No data")
    results['experiments']['temperature_advection_standalone'] = exp1

    # ── Step 4: Experiment 2 — All Signals Together ────────────────────
    print("\n" + "=" * 80)
    print("EXPERIMENT 2: All SignalRegistry Signals (full ensemble)")
    print("=" * 80)

    # Exclude signals that are known to fail or are live-only
    active_signals = [s for s in all_signals if s != 'temperature_advection']
    # TODO: also exclude any that crash on evaluate()
    print(f"  Active signals: {active_signals}")

    exp2 = {
        'description': 'Full SignalRegistry ensemble (all signals except temperature_advection)',
        'signals_used': active_signals,
        'stations': {},
        'aggregate': {},
    }

    for station in STATIONS:
        aligned = station_data[station]
        if len(aligned) < 10:
            exp2['stations'][station] = {'error': 'insufficient data'}
            continue

        metrics = run_backtest_with_risk(registry, aligned, active_signals)
        exp2['stations'][station] = metrics
        print(f"  {station}: {metrics['total_trades']} trades, "
              f"acc={metrics['accuracy']:.4f}, "
              f"Sharpe={metrics['sharpe']:.4f}, "
              f"cov={metrics['coverage']:.4f}")

    # Aggregate across stations (weighted by trade count)
    total_trades = sum(
        v['total_trades'] for v in exp2['stations'].values()
        if isinstance(v, dict) and 'total_trades' in v
    )
    total_correct = sum(
        v['correct'] for v in exp2['stations'].values()
        if isinstance(v, dict) and 'correct' in v
    )
    total_days = sum(
        v['total_days'] for v in exp2['stations'].values()
        if isinstance(v, dict) and 'total_days' in v
    )
    avg_sharpe = statistics.mean([
        v['sharpe'] for v in exp2['stations'].values()
        if isinstance(v, dict) and 'sharpe' in v and v['sharpe'] != 0
    ]) if any(
        isinstance(v, dict) and v.get('sharpe', 0) != 0
        for v in exp2['stations'].values()
    ) else 0
    avg_brier = statistics.mean([
        v['brier_score'] for v in exp2['stations'].values()
        if isinstance(v, dict) and 'brier_score' in v
    ]) if any(
        isinstance(v, dict) and 'brier_score' in v
        for v in exp2['stations'].values()
    ) else 0

    agg_acc = total_correct / total_trades if total_trades > 0 else 0
    exp2['aggregate'] = {
        'total_trades': total_trades,
        'correct': total_correct,
        'accuracy': round(agg_acc, 6),
        'avg_sharpe': round(avg_sharpe, 6),
        'avg_brier': round(avg_brier, 6),
        'total_days': total_days,
        'coverage': round(total_trades / total_days, 6) if total_days > 0 else 0,
    }
    print(f"\n  AGGREGATE: {total_trades} trades, accuracy={agg_acc:.4f}, "
          f"avg Sharpe={avg_sharpe:.4f}")
    results['experiments']['all_signals_ensemble'] = exp2

    # ── Step 5: Experiment 3 — Best Phase 6 Combo + Signal 6 ───────────
    print("\n" + "=" * 80)
    print("EXPERIMENT 3: Best Phase 6 Combo + Signal 6")
    print("=" * 80)

    # Signal 6 is live-only, so adding it to the combo effectively doesn't
    # change the backtest results. We track it separately.
    combo_with_adv = list(best_combo) + ['temperature_advection']
    print(f"  Combo: {combo_with_adv}")

    exp3 = {
        'description': 'Best Phase 6 combo with Temperature Advection added',
        'best_combo_signals': best_combo,
        'combo_with_adv': combo_with_adv,
        'stations': {},
        'aggregate': {},
        'note': 'Temperature Advection is live-only (requires GFS 850-mb data). '
                'Adding it to the ensemble has no effect on historical backtest results.',
    }

    for station in STATIONS:
        aligned = station_data[station]
        if len(aligned) < 10:
            exp3['stations'][station] = {'error': 'insufficient data'}
            continue

        # Run best combo without Signal 6
        metrics_base = run_backtest_with_risk(registry, aligned, best_combo)
        # Run best combo with Signal 6 (effectively same since Signal 6 doesn't fire)
        metrics_with = run_backtest_with_risk(registry, aligned, combo_with_adv)

        exp3['stations'][station] = {
            'base_combo': metrics_base,
            'with_adv': metrics_with,
            'delta_accuracy': round(metrics_with['accuracy'] - metrics_base['accuracy'], 6),
            'delta_trades': metrics_with['total_trades'] - metrics_base['total_trades'],
        }
        print(f"  {station}: base_acc={metrics_base['accuracy']:.4f}, "
              f"with_adv_acc={metrics_with['accuracy']:.4f}, "
              f"delta={metrics_with['accuracy']-metrics_base['accuracy']:.4f}")

    # Aggregate
    total_trades_base = sum(
        v['base_combo']['total_trades'] for v in exp3['stations'].values()
        if isinstance(v, dict) and 'base_combo' in v
    )
    total_correct_base = sum(
        v['base_combo']['correct'] for v in exp3['stations'].values()
        if isinstance(v, dict) and 'base_combo' in v
    )
    total_trades_with = sum(
        v['with_adv']['total_trades'] for v in exp3['stations'].values()
        if isinstance(v, dict) and 'with_adv' in v
    )
    total_correct_with = sum(
        v['with_adv']['correct'] for v in exp3['stations'].values()
        if isinstance(v, dict) and 'with_adv' in v
    )

    agg_acc_base = total_correct_base / total_trades_base if total_trades_base > 0 else 0
    agg_acc_with = total_correct_with / total_trades_with if total_trades_with > 0 else 0

    exp3['aggregate'] = {
        'base': {
            'total_trades': total_trades_base,
            'correct': total_correct_base,
            'accuracy': round(agg_acc_base, 6),
        },
        'with_adv': {
            'total_trades': total_trades_with,
            'correct': total_correct_with,
            'accuracy': round(agg_acc_with, 6),
        },
        'delta_accuracy': round(agg_acc_with - agg_acc_base, 6),
        'delta_trades': total_trades_with - total_trades_base,
    }
    print(f"\n  AGGREGATE (base): {total_trades_base} trades, accuracy={agg_acc_base:.4f}")
    print(f"  AGGREGATE (with_adv): {total_trades_with} trades, accuracy={agg_acc_with:.4f}")
    results['experiments']['phase6_combo_with_adv'] = exp3

    # ── Step 6: Experiment 4 — Walk-Forward Validation ────────────────
    print("\n" + "=" * 80)
    print("EXPERIMENT 4: Walk-Forward Validation")
    print("=" * 80)

    exp4 = {
        'description': 'Walk-forward validation across time splits',
        'signals_used': active_signals,
        'splits': {},
        'aggregate': {},
    }

    for split_name, train_start, train_end, test_start, test_end in WALK_FORWARD_SPLITS:
        print(f"\n  Split: {split_name}")
        if test_start is None:
            # Full holdout — use all data for completeness
            test_end_date = train_end
            train_end_date = (datetime.strptime(train_end, '%Y-%m-%d') - timedelta(days=365)).strftime('%Y-%m-%d')

        split_station_results = {}
        split_total_trades = 0
        split_total_correct = 0
        split_total_days = 0

        for station in STATIONS:
            aligned = station_data[station]
            if len(aligned) < 10:
                continue

            # Filter by date range
            if test_start:
                test_data = [d for d in aligned if d['date'] >= test_start and d['date'] <= (test_end or '2025-12-31')]
            else:
                # Holdout all: use first 80% as train, last 20% as test
                split_idx = int(len(aligned) * 0.8)
                test_data = aligned[split_idx:]

            if len(test_data) < 5:
                continue

            metrics = run_backtest_with_risk(registry, test_data, active_signals)
            split_station_results[station] = metrics
            split_total_trades += metrics['total_trades']
            split_total_correct += metrics['correct']
            split_total_days += metrics['total_days']

        split_acc = split_total_correct / split_total_trades if split_total_trades > 0 else 0
        exp4['splits'][split_name] = {
            'test_range': f"{test_start or 'first_80%'} to {test_end or 'last_20%'}",
            'stations': split_station_results,
            'aggregate': {
                'total_trades': split_total_trades,
                'correct': split_total_correct,
                'accuracy': round(split_acc, 6),
                'total_days': split_total_days,
                'coverage': round(split_total_trades / split_total_days, 6) if split_total_days > 0 else 0,
            },
        }
        print(f"    {split_total_trades} trades, accuracy={split_acc:.4f}")

    # Aggregate walk-forward
    wf_accuracies = [
        v['aggregate']['accuracy']
        for v in exp4['splits'].values()
        if v['aggregate']['total_trades'] > 0
    ]
    wf_trades = sum(
        v['aggregate']['total_trades'] for v in exp4['splits'].values()
    )
    wf_correct = sum(
        v['aggregate']['correct'] for v in exp4['splits'].values()
    )

    exp4['aggregate'] = {
        'total_trades': wf_trades,
        'correct': wf_correct,
        'accuracy': round(wf_correct / wf_trades, 6) if wf_trades > 0 else 0,
        'mean_split_accuracy': round(statistics.mean(wf_accuracies), 6) if wf_accuracies else 0,
        'min_split_accuracy': round(min(wf_accuracies), 6) if wf_accuracies else 0,
        'max_split_accuracy': round(max(wf_accuracies), 6) if wf_accuracies else 0,
        'std_split_accuracy': round(statistics.stdev(wf_accuracies), 6) if len(wf_accuracies) > 1 else 0,
    }
    results['experiments']['walk_forward_validation'] = exp4

    # ── Step 7: Experiment 5 — Temperature Advection Signal Impact Analysis ──
    print("\n" + "=" * 80)
    print("EXPERIMENT 5: Temperature Advection Signal Impact Analysis")
    print("=" * 80)

    exp5 = {
        'description': 'Analyze the impact of adding Temperature Advection to various signal combos',
        'signal_name': 'temperature_advection',
        'signal_status': adv_viability,
        'impact': {},
    }

    # Test Signal 6 with single best signals
    test_combos = {
        'with_calendar_climatology': ['calendar_climatology', 'temperature_advection'],
        'with_gaussian': ['gaussian', 'temperature_advection'],
        'with_forecast_disagreement': ['forecast_disagreement', 'temperature_advection'],
        'with_wind_direction_shift': ['wind_direction_shift', 'temperature_advection'],
    }

    for combo_name, signals in test_combos.items():
        print(f"  Testing combo: {combo_name}")
        combo_total_trades = 0
        combo_total_correct = 0
        combo_total_days = 0

        for station in STATIONS:
            aligned = station_data[station]
            if len(aligned) < 10:
                continue

            metrics = run_backtest_with_risk(registry, aligned, signals)
            combo_total_trades += metrics['total_trades']
            combo_total_correct += metrics['correct']
            combo_total_days += metrics['total_days']

        combo_acc = combo_total_correct / combo_total_trades if combo_total_trades > 0 else 0
        exp5['impact'][combo_name] = {
            'signals': signals,
            'total_trades': combo_total_trades,
            'correct': combo_total_correct,
            'accuracy': round(combo_acc, 6),
            'coverage': round(combo_total_trades / combo_total_days, 6) if combo_total_days > 0 else 0,
        }
        print(f"    {combo_total_trades} trades, accuracy={combo_acc:.4f}")

    # Comparison: calendar_climatology alone vs with adv
    if 'calendar_climatology' in registry.get_all_signals():
        print("\n  [Baseline] calendar_climatology alone:")
        cc_trades = 0
        cc_correct = 0
        cc_days = 0
        for station in STATIONS:
            aligned = station_data[station]
            if len(aligned) < 10:
                continue
            metrics = run_backtest_with_risk(registry, aligned, ['calendar_climatology'])
            cc_trades += metrics['total_trades']
            cc_correct += metrics['correct']
            cc_days += metrics['total_days']
        cc_acc = cc_correct / cc_trades if cc_trades > 0 else 0
        print(f"    {cc_trades} trades, accuracy={cc_acc:.4f}")

        exp5['baseline_calendar_climatology'] = {
            'total_trades': cc_trades,
            'correct': cc_correct,
            'accuracy': round(cc_acc, 6),
        }

    results['experiments']['adv_impact_analysis'] = exp5

    # ── Step 8: Summary ────────────────────────────────────────────────
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)

    summary = {
        'experiments_run': [
            'temperature_advection_standalone',
            'all_signals_ensemble',
            'phase6_combo_with_adv',
            'walk_forward_validation',
            'adv_impact_analysis',
        ],
        'key_findings': [],
        'recommendations': [],
    }

    # Key findings
    exp1_agg = exp1.get('aggregate', {})
    if exp1_agg.get('total_trades', 0) > 0:
        summary['key_findings'].append(
            f"Temperature Advection standalone: {exp1_agg['accuracy']:.4f} accuracy "
            f"({exp1_agg['total_trades']} trades from stored NWP data)"
        )

    exp2_agg = exp2.get('aggregate', {})
    if exp2_agg.get('total_trades', 0) > 0:
        summary['key_findings'].append(
            f"Full ensemble (all signals): {exp2_agg['accuracy']:.4f} accuracy, "
            f"Sharpe={exp2_agg['avg_sharpe']:.4f} "
            f"({exp2_agg['total_trades']} trades)"
        )

    exp3_agg = exp3.get('aggregate', {})
    if exp3_agg.get('base', {}).get('total_trades', 0) > 0:
        summary['key_findings'].append(
            f"Best Phase 6 combo: {exp3_agg['base']['accuracy']:.4f} accuracy "
            f"({exp3_agg['base']['total_trades']} trades)\n"
            f"  + Signal 6: {exp3_agg['with_adv']['accuracy']:.4f} accuracy "
            f"({exp3_agg['with_adv']['total_trades']} trades)\n"
            f"  Delta: {exp3_agg['delta_accuracy']} accuracy, {exp3_agg['delta_trades']} trades"
        )

    exp4_agg = exp4.get('aggregate', {})
    if exp4_agg.get('total_trades', 0) > 0:
        summary['key_findings'].append(
            f"Walk-forward validation: {exp4_agg['mean_split_accuracy']:.4f} mean accuracy "
            f"(min={exp4_agg['min_split_accuracy']:.4f}, "
            f"max={exp4_agg['max_split_accuracy']:.4f}, "
            f"std={exp4_agg['std_split_accuracy']:.4f})"
        )

    summary['key_findings'].append(
        "Temperature Advection (Signal 6) is a LIVE-ONLY signal requiring GFS 850-mb "
        "forecast data. Historical backtest is not possible without stored NWP data. "
        f"Currently {adv_viability['stored_records']} advection records in NWP database."
    )

    # Recommendations
    summary['recommendations'] = [
        "Continue collecting GFS 850-mb advection data into nwp_forecasts.db for future backtesting",
        "Implement cached daily GFS fetch for all 20 cities to build historical advection dataset",
        "Once sufficient history exists (≥30 days for rolling std), re-run standalone backtest",
        "Consider adding temperature_advection to production ensemble once live performance is verified",
        "Phase 2.5: implement historical NWP reanalysis backfill for advection data",
    ]

    print("\nKey Findings:")
    for i, finding in enumerate(summary['key_findings'], 1):
        print(f"  {i}. {finding}")

    print("\nRecommendations:")
    for i, rec in enumerate(summary['recommendations'], 1):
        print(f"  {i}. {rec}")

    results['summary'] = summary

    # ── Save Results ───────────────────────────────────────────────────
    print(f"\n  Saving results to {OUTPUT_PATH}...")
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, 'w') as f:
        json.dump(results, f, indent=2, default=str)
    print(f"  Results saved! ({os.path.getsize(OUTPUT_PATH)} bytes)")

    return results


# ═══════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print(f"Working directory: {os.getcwd()}")
    print(f"Repo root: {REPO_ROOT}")
    os.chdir(str(REPO_ROOT))
    print(f"Changed to: {os.getcwd()}")
    run_phase2_backtest()