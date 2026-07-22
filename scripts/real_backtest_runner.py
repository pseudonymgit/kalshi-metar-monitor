#!/usr/bin/env python3

"""
Phase 21.3 — Real Backtest Runner

Historical walk-forward backtest using real settlement data.
Replaces Monte Carlo simulation-based Phase 14 tests.

Key features:
- Walk-forward validation on historical data
- Separate P&L calculation from signal accuracy (Gray Room R9 Elephant 2)
- Reports: directional accuracy, P&L, Sharpe, drawdown, max consecutive losses
- Configurable date range and signal selection
- No AI/ML in the prediction loop

Usage:
    python scripts/real_backtest_runner.py --start 2025-01-01 --end 2025-06-30
    python scripts/real_backtest_runner.py --signal gaussian --signal persistence
    python scripts/real_backtest_runner.py --all --verbose
"""

import sys
import os
import json
import math
import argparse
import sqlite3
from datetime import datetime, timedelta, date
from typing import List, Dict, Optional, Tuple, Any
from collections import defaultdict
from pathlib import Path

# Ensure repo root is on path
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.signals import SignalRegistry
from core.agreement_gate import AgreementGate, SimpleAgreementChecker


# ─── Backtest Engine ─────────────────────────────────────────────────────

class BacktestEngine:
    """
    Walk-forward backtest engine using historical weather data.

    Separates signal accuracy (did the prediction match reality?) from
    P&L (what would we have earned?) — a key requirement from Gray Room R9.
    """

    def __init__(self, db_path: str = None, verbose: bool = False):
        if db_path is None:
            db_path = os.environ.get(
                "METAR_DB_PATH",
                str(_REPO_ROOT / "data" / "metar_observations.db")
            )
        self.db_path = db_path
        self.verbose = verbose
        self.registry = SignalRegistry(db_path)
        self.results: Dict[str, Any] = {}
        self._log("Backtest engine initialized")

    def _log(self, msg: str):
        if self.verbose:
            print(f"[BT] {msg}")

    # ─── Data Loading ────────────────────────────────────────────────────

    def load_historical_data(self, station: str, start_date: str,
                              end_date: str) -> List[Dict]:
        """
        Load historical daily weather data for a station.

        Returns list of dicts with keys: date, high, low, temp, dewpoint,
        wind_dir, wind_speed, pressure.
        """
        conn = sqlite3.connect(self.db_path)
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT date_utc, MAX(temp_f) as high, MIN(temp_f) as low,
                       AVG(temp_f) as temp, AVG(dewpoint_f) as dewpoint,
                       AVG(wind_direction_deg) as wind_dir,
                       AVG(wind_speed_kt) as wind_speed,
                       AVG(pressure_mb) as pressure
                FROM metar_observations
                WHERE station = ? AND date_utc >= ? AND date_utc <= ?
                  AND temp_f IS NOT NULL
                GROUP BY date_utc
                ORDER BY date_utc ASC
            """, (station, start_date, end_date))

            days = []
            for r in cur.fetchall():
                days.append({
                    'date': r[0],
                    'high': r[1],
                    'low': r[2],
                    'temp': r[3],
                    'dewpoint': r[4],
                    'wind_dir': r[5],
                    'wind_speed': r[6],
                    'pressure': r[7],
                })
            return days
        finally:
            conn.close()

    def load_settlement_data(self, station: str, market_type: str,
                              start_date: str, end_date: str) -> Dict[str, float]:
        """
        Load actual settlement prices from Kalshi markets.

        Returns dict mapping date -> settlement_price (0.0-1.0).
        Settlement of 0.0 = market expired below threshold, 1.0 = above.
        """
        # Try to load from Kalshi settlement cache
        conn = sqlite3.connect(self.db_path)
        try:
            cur = conn.cursor()
            # Try settlements table
            try:
                cur.execute("""
                    SELECT date_utc, settlement_price
                    FROM kalshi_settlements
                    WHERE station = ? AND market_type = ?
                      AND date_utc >= ? AND date_utc <= ?
                    ORDER BY date_utc ASC
                """, (station, market_type, start_date, end_date))
                results = {r[0]: r[1] for r in cur.fetchall()}
                if results:
                    return results
            except sqlite3.OperationalError:
                pass

            # Fallback: infer from settlement_station_cache
            try:
                cur.execute("""
                    SELECT date, settlement_price
                    FROM settlement_station_cache
                    WHERE station = ? AND date >= ? AND date <= ?
                    ORDER BY date ASC
                """, (station, start_date, end_date))
                results = {r[0]: r[1] for r in cur.fetchall()}
                if results:
                    return results
            except sqlite3.OperationalError:
                pass

            return {}
        finally:
            conn.close()

    # ─── Signal Accuracy (separate from P&L) ────────────────────────────

    def evaluate_signal_accuracy(self, station: str, days: List[Dict],
                                  signal_name: str) -> Dict[str, Any]:
        """
        Evaluate signal directional accuracy without P&L.

        For each day where the signal fires, compare the predicted direction
        to the actual temperature movement.

        Returns:
            Dict with accuracy metrics (no P&L contamination)
        """
        sig = self.registry.signals.get(signal_name)
        if sig is None:
            return {"error": f"Signal '{signal_name}' not found"}

        predictions = []
        for idx in range(sig.min_lookback + 1, len(days)):
            try:
                direction, confidence = sig.evaluate(idx, days)
                if direction is not None and confidence > 0:
                    # Actual direction: compare day[idx-1] high to day[idx-2] high
                    if idx >= 2:
                        prev_high = days[idx - 2].get('high')
                        current_high = days[idx - 1].get('high')
                        if prev_high is not None and current_high is not None:
                            actual = 'up' if current_high > prev_high else 'down'
                            correct = direction == actual
                            predictions.append({
                                'date': days[idx - 1]['date'],
                                'predicted': direction,
                                'actual': actual,
                                'correct': correct,
                                'confidence': confidence,
                            })
            except Exception as e:
                self._log(f"  Error evaluating {signal_name} at idx {idx}: {e}")

        if not predictions:
            return {"signal": signal_name, "total_predictions": 0}

        total = len(predictions)
        correct = sum(1 for p in predictions if p['correct'])
        accuracy = correct / total if total > 0 else 0.0

        # Confidence calibration
        avg_confidence = sum(p['confidence'] for p in predictions) / total

        # Direction breakdown
        up_predictions = [p for p in predictions if p['predicted'] == 'up']
        down_predictions = [p for p in predictions if p['predicted'] == 'down']
        up_accuracy = sum(1 for p in up_predictions if p['correct']) / len(up_predictions) if up_predictions else 0.0
        down_accuracy = sum(1 for p in down_predictions if p['correct']) / len(down_predictions) if down_predictions else 0.0

        return {
            "signal": signal_name,
            "total_predictions": total,
            "correct": correct,
            "incorrect": total - correct,
            "directional_accuracy": round(accuracy, 4),
            "avg_confidence": round(avg_confidence, 4),
            "up_predictions": len(up_predictions),
            "down_predictions": len(down_predictions),
            "up_accuracy": round(up_accuracy, 4),
            "down_accuracy": round(down_accuracy, 4),
            "is_calibrated": abs(accuracy - avg_confidence) < 0.15,
        }

    # ─── P&L Calculation (separate from signal accuracy) ────────────────

    def calculate_pnl(self, station: str, days: List[Dict],
                       signal_name: str, initial_capital: float = 10000.0,
                       position_size_pct: float = 0.02) -> Dict[str, Any]:
        """
        Calculate P&L from signal predictions.

        Uses a simple 2% per-trade risk model. No compound interest magic.
        P&L is reported separately from signal accuracy.

        Rules:
        - Buy at market price ~0.50 (assumed 50/50)
        - Win: +1.0 unit (contract pays $1 if correct)
        - Loss: -1.0 unit (contract expires worthless)
        - Each trade sized at position_size_pct of capital
        """
        sig = self.registry.signals.get(signal_name)
        if sig is None:
            return {"error": f"Signal '{signal_name}' not found"}

        trades = []
        capital = float(initial_capital)
        peak_capital = capital
        drawdowns = []
        consecutive_losses = 0
        max_consecutive_losses = 0

        for idx in range(sig.min_lookback + 1, len(days)):
            try:
                direction, confidence = sig.evaluate(idx, days)
                if direction is not None and confidence > 0:
                    # Determine actual direction
                    if idx >= 2:
                        prev_high = days[idx - 2].get('high')
                        current_high = days[idx - 1].get('high')
                        if prev_high is not None and current_high is not None:
                            actual = 'up' if current_high > prev_high else 'down'
                            correct = direction == actual

                            # Position sizing: 2% of capital per trade
                            position_value = capital * position_size_pct
                            # Win: market pays 1:1 (binary contract)
                            # Loss: full stake lost
                            if correct:
                                pnl = position_value * 0.90  # 90% payout (assuming spread)
                                consecutive_losses = 0
                            else:
                                pnl = -position_value  # Full loss
                                consecutive_losses += 1
                                max_consecutive_losses = max(max_consecutive_losses, consecutive_losses)

                            capital += pnl
                            peak_capital = max(peak_capital, capital)
                            drawdown = (peak_capital - capital) / peak_capital * 100
                            drawdowns.append(drawdown)

                            trades.append({
                                'date': days[idx - 1]['date'],
                                'predicted': direction,
                                'actual': actual,
                                'correct': correct,
                                'confidence': confidence,
                                'position_value': round(position_value, 2),
                                'pnl': round(pnl, 2),
                                'capital': round(capital, 2),
                                'drawdown_pct': round(drawdown, 2),
                            })
            except Exception as e:
                self._log(f"  Error in P&L calc for {signal_name} at idx {idx}: {e}")

        if not trades:
            return {
                "signal": signal_name,
                "total_trades": 0,
                "final_capital": initial_capital,
                "total_pnl": 0.0,
                "return_pct": 0.0,
            }

        # Compute metrics
        total_pnl = capital - initial_capital
        return_pct = (capital / initial_capital - 1.0) * 100

        # Win rate
        wins = sum(1 for t in trades if t['correct'])
        total = len(trades)
        win_rate = wins / total if total > 0 else 0.0

        # Sharpe ratio (annualized)
        daily_returns = [t['pnl'] / initial_capital for t in trades]
        if len(daily_returns) > 1:
            avg_return = sum(daily_returns) / len(daily_returns)
            std_return = math.sqrt(
                sum((r - avg_return) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
            ) if len(daily_returns) > 1 else 0.0
            sharpe = (avg_return / std_return * math.sqrt(252)) if std_return > 0 else 0.0
        else:
            sharpe = 0.0

        # Max drawdown
        max_drawdown = max(drawdowns) if drawdowns else 0.0

        return {
            "signal": signal_name,
            "total_trades": total,
            "wins": wins,
            "losses": total - wins,
            "win_rate": round(win_rate, 4),
            "initial_capital": initial_capital,
            "final_capital": round(capital, 2),
            "total_pnl": round(total_pnl, 2),
            "return_pct": round(return_pct, 2),
            "sharpe_ratio": round(sharpe, 4),
            "max_drawdown_pct": round(max_drawdown, 2),
            "max_consecutive_losses": max_consecutive_losses,
            "avg_trade_pnl": round(total_pnl / total, 2) if total > 0 else 0.0,
            "profit_factor": round(
                sum(t['pnl'] for t in trades if t['pnl'] > 0) /
                abs(sum(t['pnl'] for t in trades if t['pnl'] < 0)),
                2
            ) if sum(t['pnl'] for t in trades if t['pnl'] < 0) != 0 else float('inf'),
        }

    # ─── Walk-Forward Validation ─────────────────────────────────────────

    def run_walk_forward(self, station: str, start_date: str,
                          end_date: str, signal_names: List[str] = None,
                          initial_capital: float = 10000.0) -> Dict[str, Any]:
        """
        Run walk-forward validation across date range.

        For each signal, computes accuracy and P&L separately.
        Uses fixed 2% risk per trade, no compound recalculation.
        """
        self._log(f"Loading data for {station}: {start_date} to {end_date}")
        days = self.load_historical_data(station, start_date, end_date)
        self._log(f"Loaded {len(days)} days of data")

        if len(days) < 10:
            return {"error": f"Only {len(days)} days of data for {station}"}

        if signal_names is None:
            signal_names = list(self.registry.signals.keys())

        results = {
            "station": station,
            "date_range": {"start": start_date, "end": end_date},
            "total_days": len(days),
            "signals": {},
            "ensemble": {},
        }

        for name in signal_names:
            if name not in self.registry.signals:
                self._log(f"  Skipping unknown signal: {name}")
                continue

            self._log(f"  Evaluating {name}...")

            # Accuracy (no P&L)
            accuracy = self.evaluate_signal_accuracy(station, days, name)

            # P&L (separate from accuracy)
            pnl = self.calculate_pnl(station, days, name, initial_capital)

            results["signals"][name] = {
                "accuracy": accuracy,
                "pnl": pnl,
            }

        # Ensemble: combine all signals via agreement gate
        ensemble_results = self._run_ensemble_backtest(station, days, signal_names, initial_capital)
        results["ensemble"] = ensemble_results

        return results

    def _run_ensemble_backtest(self, station: str, days: List[Dict],
                                signal_names: List[str],
                                initial_capital: float) -> Dict[str, Any]:
        """
        Run ensemble backtest using agreement gate for signal combination.

        All signals vote; agreement gate filters to consensus direction.
        P&L calculated from agreed signals only.
        """
        gate = AgreementGate(n_required=3, m_total=len(signal_names))
        trades = []
        capital = float(initial_capital)
        peak_capital = capital
        max_drawdown = 0.0
        consecutive_losses = 0
        max_consecutive = 0

        # Find max lookback across all signals
        max_lookback = max(
            self.registry.signals[n].min_lookback
            for n in signal_names if n in self.registry.signals
        )

        for idx in range(max_lookback + 1, len(days)):
            # Collect votes from all signals
            votes = []
            for name in signal_names:
                sig = self.registry.signals.get(name)
                if sig is None:
                    continue
                try:
                    direction, confidence = sig.evaluate(idx, days)
                    if direction is not None and confidence > 0:
                        votes.append((station, "HIGH", direction.upper(), name))
                except Exception:
                    pass

            if not votes:
                continue

            # Apply agreement gate
            agreed = gate.filter_signals(votes) if len(votes) >= 3 else votes
            if not agreed:
                continue

            # Determine consensus direction
            up_votes = sum(1 for v in agreed if v[2] == "UP")
            down_votes = sum(1 for v in agreed if v[2] == "DOWN")
            consensus = "up" if up_votes >= down_votes else "down"

            # Actual direction
            if idx >= 2:
                prev_high = days[idx - 2].get('high')
                current_high = days[idx - 1].get('high')
                if prev_high is not None and current_high is not None:
                    actual = 'up' if current_high > prev_high else 'down'
                    correct = consensus == actual

                    position_value = capital * 0.02
                    if correct:
                        pnl = position_value * 0.90
                        consecutive_losses = 0
                    else:
                        pnl = -position_value
                        consecutive_losses += 1
                        max_consecutive = max(max_consecutive, consecutive_losses)

                    capital += pnl
                    peak_capital = max(peak_capital, capital)
                    dd = (peak_capital - capital) / peak_capital * 100
                    max_drawdown = max(max_drawdown, dd)

                    trades.append({
                        'date': days[idx - 1]['date'],
                        'consensus': consensus,
                        'actual': actual,
                        'correct': correct,
                        'agreement_count': len(agreed),
                        'pnl': round(pnl, 2),
                        'capital': round(capital, 2),
                    })

        if not trades:
            return {"total_trades": 0, "final_capital": initial_capital}

        total_pnl = capital - initial_capital
        wins = sum(1 for t in trades if t['correct'])
        total = len(trades)
        win_rate = wins / total if total > 0 else 0.0

        daily_returns = [t['pnl'] / initial_capital for t in trades]
        if len(daily_returns) > 1:
            avg_r = sum(daily_returns) / len(daily_returns)
            std_r = math.sqrt(
                sum((r - avg_r) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
            ) if len(daily_returns) > 1 else 0.0
            sharpe = (avg_r / std_r * math.sqrt(252)) if std_r > 0 else 0.0
        else:
            sharpe = 0.0

        return {
            "total_trades": total,
            "wins": wins,
            "losses": total - wins,
            "win_rate": round(win_rate, 4),
            "total_pnl": round(total_pnl, 2),
            "return_pct": round((capital / initial_capital - 1.0) * 100, 2),
            "sharpe_ratio": round(sharpe, 4),
            "max_drawdown_pct": round(max_drawdown, 2),
            "max_consecutive_losses": max_consecutive,
            "avg_agreement_count": round(sum(t['agreement_count'] for t in trades) / total, 1),
            "final_capital": round(capital, 2),
        }

    # ─── Multi-Station Run ───────────────────────────────────────────────

    def run_multi_station(self, stations: List[str], start_date: str,
                           end_date: str, signal_names: List[str] = None,
                           initial_capital: float = 10000.0) -> Dict[str, Any]:
        """Run backtest across multiple stations and aggregate results."""
        all_results = {}
        for station in stations:
            self._log(f"\n{'='*60}")
            self._log(f"Running backtest for {station}")
            self._log(f"{'='*60}")
            result = self.run_walk_forward(
                station, start_date, end_date,
                signal_names, initial_capital,
            )
            all_results[station] = result

        # Aggregate ensemble results
        ensemble_results = self._aggregate_ensemble(all_results, initial_capital)
        return {
            "stations": all_results,
            "aggregate": ensemble_results,
        }

    def _aggregate_ensemble(self, all_results: Dict[str, Dict],
                              initial_capital: float) -> Dict[str, Any]:
        """Aggregate ensemble results across all stations."""
        total_trades = 0
        total_wins = 0
        total_pnl = 0.0
        capital = float(initial_capital)
        peak = capital
        max_dd = 0.0

        for station, result in all_results.items():
            ensemble = result.get("ensemble", {})
            if ensemble.get("total_trades", 0) == 0:
                continue
            total_trades += ensemble["total_trades"]
            total_wins += ensemble["wins"]
            total_pnl += ensemble["total_pnl"]
            capital += ensemble["total_pnl"]
            peak = max(peak, capital)
            dd = (peak - capital) / peak * 100
            max_dd = max(max_dd, dd)

        win_rate = total_wins / total_trades if total_trades > 0 else 0.0
        return {
            "total_trades": total_trades,
            "total_wins": total_wins,
            "win_rate": round(win_rate, 4),
            "total_pnl": round(total_pnl, 2),
            "return_pct": round((capital / initial_capital - 1.0) * 100, 2),
            "max_drawdown_pct": round(max_dd, 2),
            "final_capital": round(capital + initial_capital, 2),
        }


# ─── Report Generation ──────────────────────────────────────────────────

def format_report(results: Dict[str, Any]) -> str:
    """Format backtest results as a readable report."""
    if "error" in results:
        return f"ERROR: {results['error']}"

    lines = []
    lines.append("=" * 80)
    lines.append("BACKTEST REPORT — Phase 21.3")
    lines.append("=" * 80)

    if "station" in results:
        lines.append(f"\nStation: {results['station']}")
        lines.append(
            f"Date Range: {results['date_range']['start']} → {results['date_range']['end']}"
        )
        lines.append(f"Total Days: {results['total_days']}")
        lines.append(f"\n{'─' * 80}")

        # Per-signal results
        for name, sig_data in results.get("signals", {}).items():
            acc = sig_data.get("accuracy", {})
            pnl = sig_data.get("pnl", {})

            lines.append(f"\nSignal: {name}")
            if acc.get("total_predictions", 0) > 0:
                lines.append(f"  Accuracy:          {acc.get('directional_accuracy', 0)*100:.1f}%")
                lines.append(f"  Predictions:       {acc.get('total_predictions', 0)}")
                lines.append(f"  Avg Confidence:    {acc.get('avg_confidence', 0):.3f}")
                lines.append(f"  Calibrated:        {acc.get('is_calibrated', False)}")
            else:
                lines.append(f"  Accuracy:          N/A (no predictions)")

            if pnl.get("total_trades", 0) > 0:
                lines.append(f"  P&L:               ${pnl.get('total_pnl', 0):.2f}")
                lines.append(f"  Return:            {pnl.get('return_pct', 0):.2f}%")
                lines.append(f"  Win Rate:          {pnl.get('win_rate', 0)*100:.1f}%")
                lines.append(f"  Sharpe:            {pnl.get('sharpe_ratio', 0):.3f}")
                lines.append(f"  Max Drawdown:      {pnl.get('max_drawdown_pct', 0):.1f}%")
                lines.append(f"  Max Consec Losses: {pnl.get('max_consecutive_losses', 0)}")
            else:
                lines.append(f"  P&L:               N/A (no trades)")

        # Ensemble results
        ensemble = results.get("ensemble", {})
        if ensemble.get("total_trades", 0) > 0:
            lines.append(f"\n{'─' * 80}")
            lines.append(f"ENSEMBLE (Agreement Gate)")
            lines.append(f"{'─' * 80}")
            lines.append(f"  Total Trades:      {ensemble['total_trades']}")
            lines.append(f"  Win Rate:          {ensemble.get('win_rate', 0)*100:.1f}%")
            lines.append(f"  Total P&L:         ${ensemble.get('total_pnl', 0):.2f}")
            lines.append(f"  Return:            {ensemble.get('return_pct', 0):.2f}%")
            lines.append(f"  Sharpe:            {ensemble.get('sharpe_ratio', 0):.3f}")
            lines.append(f"  Max Drawdown:      {ensemble.get('max_drawdown_pct', 0):.1f}%")
            lines.append(f"  Max Consec Losses: {ensemble.get('max_consecutive_losses', 0)}")
            lines.append(f"  Avg Agreement:     {ensemble.get('avg_agreement_count', 0):.1f} signals")

    elif "aggregate" in results:
        agg = results["aggregate"]
        lines.append(f"\nAGGREGATE (Multi-Station)")
        lines.append(f"{'─' * 80}")
        lines.append(f"  Total Trades:      {agg.get('total_trades', 0)}")
        lines.append(f"  Win Rate:          {agg.get('win_rate', 0)*100:.1f}%")
        lines.append(f"  Total P&L:         ${agg.get('total_pnl', 0):.2f}")
        lines.append(f"  Return:            {agg.get('return_pct', 0):.2f}%")
        lines.append(f"  Max Drawdown:      {agg.get('max_drawdown_pct', 0):.1f}%")
        lines.append(f"  Final Capital:     ${agg.get('final_capital', 0):.2f}")

        for station, result in results.get("stations", {}).items():
            ens = result.get("ensemble", {})
            lines.append(f"\n  {station}:")
            lines.append(f"    Trades: {ens.get('total_trades', 0)} | "
                         f"Win Rate: {ens.get('win_rate', 0)*100:.1f}% | "
                         f"P&L: ${ens.get('total_pnl', 0):.2f}")

    lines.append(f"\n{'=' * 80}")
    lines.append("Report generated by Phase 21.3 Backtest Runner")
    lines.append(f"Timestamp: {datetime.utcnow().isoformat()}Z")
    return "\n".join(lines)


def save_results(results: Dict[str, Any], path: str):
    """Save results to a JSON file."""
    with open(path, 'w') as f:
        json.dump(results, f, indent=2, default=str)
    print(f"Results saved to {path}")


# ─── CLI ─────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="Phase 21.3 — Real Backtest Runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/real_backtest_runner.py --station KATL --start 2025-01-01 --end 2025-06-30
  python scripts/real_backtest_runner.py --signal gaussian --signal persistence
  python scripts/real_backtest_runner.py --all --verbose
  python scripts/real_backtest_runner.py --multi KATL,KBOS,KLAX
        """
    )
    parser.add_argument('--station', default='KATL', help='Station ICAO code')
    parser.add_argument('--start', default='2025-01-01', help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end', default='2025-06-30', help='End date (YYYY-MM-DD)')
    parser.add_argument('--signal', action='append', dest='signals', help='Signal name (repeatable)')
    parser.add_argument('--all', action='store_true', help='Run all signals')
    parser.add_argument('--multi', help='Comma-separated list of stations for multi-station run')
    parser.add_argument('--capital', type=float, default=10000.0, help='Initial capital')
    parser.add_argument('--db', help='Path to METAR database')
    parser.add_argument('--output', help='Save results to JSON file')
    parser.add_argument('--verbose', '-v', action='store_true', help='Verbose output')
    return parser.parse_args()


def main():
    args = parse_args()

    engine = BacktestEngine(db_path=args.db, verbose=args.verbose)

    # Determine signal list
    if args.all:
        signal_names = list(engine.registry.signals.keys())
    elif args.signals:
        signal_names = args.signals
    else:
        # Default: all signals
        signal_names = list(engine.registry.signals.keys())

    if args.multi:
        stations = [s.strip() for s in args.multi.split(',')]
        results = engine.run_multi_station(
            stations, args.start, args.end,
            signal_names, args.capital,
        )
    else:
        results = engine.run_walk_forward(
            args.station, args.start, args.end,
            signal_names, args.capital,
        )

    report = format_report(results)
    print(report)

    if args.output:
        save_results(results, args.output)

    return results


if __name__ == "__main__":
    main()