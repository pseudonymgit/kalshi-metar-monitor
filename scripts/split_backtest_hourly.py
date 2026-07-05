#!/usr/bin/env python3
"""
Split-Backtest Harness (v2.0)

Isolates individual trading signals/methods and backtests each independently
against the settlement_epochs data with NEW capability for NWP integration.
Enhanced with improved metrics, cross-validation, and support for regime-conditioned signals.

Each signal is tested in isolation:
  1. Forecast Disagreement (Edge 5) - when models disagree, bet on reversion
  2. Temperature Reversion (Edge 6) - extreme temps revert to climatology
  3. Pressure Signal (Edge 8) - pressure tendency → weather regime change
  4. Time-of-Day Decay (Edge 2) - forecast skill decays with lead time
  5. Late-day METAR Momentum (P1.2) - plateau/slope detection for same-day trading
  6. Ensemble Agreement Gate (P2) - model consensus gate
  7. Extended Climatology Analysis (P1.1) - regime-conditioned historical patterns
  8. Cross-platform Divergence (P1.3) - Kalshi vs Polymarket pricing differences

Metrics per signal (enhanced):
  - Directional accuracy (%) + confidence intervals
  - Brier score + decomposition (resolution, reliability, uncertainty)
  - Sharpe ratio (simulated P&L with fees)
  - Drawdown metrics
  - Coverage (% of days signal fires)
  - Profit factor + Calmar ratio
  - Statistical significance (binomial p-value)

⚠️  STANDALONE SCRIPT - DO NOT RUN VIA AI/AGENT.
    Run manually: python3 scripts/split_backtest.py [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--model]
"""

import sqlite3
import math
import sys
import argparse
import numpy as np
from datetime import datetime, timedelta
from collections import defaultdict
from scipy import stats
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
METAR_DB = str(REPO_ROOT / "data" / "metar_backfill.db")
NWP_DB = str(REPO_ROOT / "data" / "nwp_forecasts.db")
CLIMATE_DATA_DIR = str(REPO_ROOT / "data" / "climate_indices")

# Import the hourly late-day momentum signal
sys.path.insert(0, str(REPO_ROOT / "core"))
from late_day_momentum_hourly import late_day_momentum_hourly as _ldm_hourly_signal

# All 20 Kalshi stations
STATIONS = [
    'KATL', 'KAUS', 'KBOS', 'KDCA', 'KDEN', 'KDFW', 'KHOU', 'KLAS',
    'KLAX', 'KMDW', 'KMIA', 'KMSP', 'KMSY', 'KNYC', 'KOKC', 'KPHL',
    'KPHX', 'KSAT', 'KSEA', 'KSFO'
]

# Minimum trade count for reliable stats
MIN_TRADES = 20  # Reduced for development
CONFIDENCE_INTERVAL = 0.95


class SignalResult:
    """Enhanced container for per-signal backtest results."""
    def __init__(self, name):
        self.name = name
        self.trades = []       # list of (station, date, predicted_dir, actual_dir, confidence, raw_prob)
        self.correct = 0
        self.total = 0
        self.coverage = 0      # days signal fired
        self.total_days = 0    # total days evaluated
        self.pnl_stream = []   # Profit/loss history for Sharpe, drawdown calculation
        self.fee_rate = 0.001  # Trading fee per fill as proportion
        self.non_trades = 0    # evaluated records where this signal did not fire

    @property
    def round_trip_fee(self):
        """Fee paid on entry and exit for one binary-contract round trip."""
        return 2 * self.fee_rate

    def record_no_trade(self):
        """Record an evaluated day where the signal explicitly did not trade.

        No-trade observations are useful for coverage, but they must never enter
        the win/loss denominator or P&L stream. Counting skipped days as wins was
        the source of the prior degenerate 100% / infinite-PF metrics for sparse
        signals.
        """
        self.non_trades += 1

    def add_trade(self, station, date, predicted_dir, actual_dir, confidence=0.5, raw_prob=0.5):
        """Add a completed trade to results."""
        if predicted_dir is None or predicted_dir == 'flat':
            self.record_no_trade()
            return
        if actual_dir is None or actual_dir == 'flat':
            self.record_no_trade()
            return

        self.trades.append((station, date, predicted_dir, actual_dir, confidence, raw_prob))
        self.total += 1
        if predicted_dir == actual_dir:
            self.correct += 1
            # Gain of 1.0 for correct direction
            self.pnl_stream.append(1.0 - self.round_trip_fee)  # Fee applies both entry/exit
        else:
            # Loss of 1.0 for incorrect direction
            self.pnl_stream.append(-1.0 - self.round_trip_fee)

    def _calculate_drawdown(self):
        """Calculate maximum drawdown from P&L stream."""
        if not self.pnl_stream:
            return 0.0

        cumulative = [0]
        for pnl in self.pnl_stream:
            cumulative.append(cumulative[-1] + pnl)

        peak = 0
        max_dd = 0
        for value in cumulative[1:]:
            if value > peak:
                peak = value
            drawdown = peak - value
            if drawdown > max_dd:
                max_dd = drawdown
        return max_dd

    def _calculate_calmar_ratio(self):
        """Calculate Calmar ratio (return / max drawdown)."""
        if not self.pnl_stream or len(self.pnl_stream) == 0:
            return 0.0

        total_return = sum(self.pnl_stream)
        max_dd = self._calculate_drawdown()
        return total_return / max_dd if max_dd > 0 else total_return

    def compute_metrics(self):
        """Calculate all metrics for the signal."""
        if self.total == 0 or self.total_days == 0:
            return self._empty_metrics()

        accuracy = self.correct / self.total

        # Calculate confidence interval for accuracy
        if self.total >= MIN_TRADES:  # Enough data for CI
            z_score = 1.96 if CONFIDENCE_INTERVAL == 0.95 else 2.576
            ci_radius = z_score * math.sqrt((accuracy * (1 - accuracy)) / self.total)
            ci_lower = max(0.0, accuracy - ci_radius)
            ci_upper = min(1.0, accuracy + ci_radius)
        else:
            ci_lower = ci_upper = accuracy

        # Brier score with decomposition: BS = (F - O)2 where F=forecast, O=outcome
        brier_sum = 0
        reliability = 0
        resolution = 0
        uncertainty = 0
        total_obs = 0

        if self.trades:
            # Calculate outcome base rate for uncertainty component
            outcome_base_rate = sum(1 for _, _, _, actual, _, _ in self.trades if actual == 'up') / len(self.trades)
            uncertainty = outcome_base_rate * (1 - outcome_base_rate)

            for _, _, pred, actual, conf, prob in self.trades:
                actual_binary = 1 if actual == 'up' else 0
                # Use raw prob for brier, conf as modifier for calibration
                pred_prob = prob if pred == 'up' else (1 - prob)
                error = (pred_prob - actual_binary) ** 2
                brier_sum += error

                total_obs += 1

            brier = brier_sum / len(self.trades) if self.trades else 0.5

            # Brier decomposition requires bucketing by forecast probability
            # Simplified version: reliability reflects how close forecasts are to realized frequencies
            reliability = max(0, brier - uncertainty)  # Brier = reliability - resolution + uncertainty
            resolution = max(0, uncertainty - brier)

        # Sharpe ratio: mean pnl / std dev pnl, with annualization
        if self.pnl_stream and len(self.pnl_stream) > 1:
            avg_pnl = sum(self.pnl_stream) / len(self.pnl_stream)
            var_pnl = sum((p - avg_pnl) ** 2 for p in self.pnl_stream) / max(1, len(self.pnl_stream) - 1)
            std_pnl = math.sqrt(var_pnl)
            sharpe = (avg_pnl / std_pnl * math.sqrt(252)) if std_pnl > 0 else 0
        else:
            sharpe = 0
            avg_pnl = 0

        # Coverage (percentage of evaluated days where signal activated).
        # Use trades only. Skipped/non-trade days are intentionally excluded
        # from accuracy, P&L, and profit factor.
        evaluated_days = self.total + self.non_trades
        denominator = evaluated_days if evaluated_days > 0 else self.total_days
        coverage = self.total / denominator if denominator else 0.0

        # Profit factor: gains / losses
        gains = sum(p for p in self.pnl_stream if p > 0)
        losses = abs(sum(p for p in self.pnl_stream if p < 0))
        if losses > 0:
            profit_factor = gains / losses
        elif gains > 0:
            profit_factor = float('inf')
        else:
            profit_factor = 0.0

        # Calmar ratio
        calmar = self._calculate_calmar_ratio()

        # Statistical significance: binomial test
        # Null hypothesis: accuracy = 0.5 (coin flip)
        signif_p = 0.5  # default
        if len(self.pnl_stream) >= MIN_TRADES and accuracy != 0.5:
            successes = self.correct
            trials = self.total
            if accuracy > 0.5:
                # P(X >= successes) where X ~ Binomial(trials, 0.5)
                signif_p = 1 - stats.binom.cdf(successes - 1, trials, 0.5)
            else:
                # P(X <= successes)
                signif_p = stats.binom.cdf(successes, trials, 0.5)

        return {
            'name': self.name,
            'trades': self.total,
            'accuracy': accuracy,
            'accuracy_ci': (ci_lower, ci_upper),
            'brier': brier or 0.0,
            'brier_decomp': {
                'reliability': reliability or 0.0,
                'resolution': resolution or 0.0,
                'uncertainty': uncertainty or 0.0
            },
            'sharpe': sharpe,
            'coverage': coverage,
            'evaluated_days': evaluated_days,
            'non_trades': self.non_trades,
            'profit_factor': profit_factor,
            'calmar_ratio': calmar,
            'max_drawdown': self._calculate_drawdown(),
            'statistical_significance_p': min(signif_p * 2, 1.0),  # two-tailed
            'avg_pnl': avg_pnl
        }

    def _empty_metrics(self):
        """Return empty metrics template."""
        return {
            'name': self.name,
            'trades': 0,
            'non_trades': self.non_trades,
            'evaluated_days': self.non_trades,
            'accuracy': 0.0,
            'accuracy_ci': (0.0, 0.0),
            'brier': 0.5,
            'brier_decomp': {'reliability': 0.0, 'resolution': 0.0, 'uncertainty': 0.0},
            'sharpe': 0.0,
            'coverage': 0.0,
            'profit_factor': 0.0,
            'calmar_ratio': 0.0,
            'max_drawdown': 0.0,
            'statistical_significance_p': 1.0,
            'avg_pnl': 0.0
        }


def load_settlement_data(start_date=None, end_date=None):
    """Load settlement epochs and daily_stats from METAR database."""
    conn = sqlite3.connect(METAR_DB, timeout=10)
    c = conn.cursor()

    query = """
        SELECT ds.station, ds.date_utc, ds.max_temp_f, ds.min_temp_f,
               se.settlement_bucket, se.prior_settlement_bucket,
               se.local_trading_date
        FROM daily_stats ds
        JOIN settlement_epochs se
        ON ds.date_utc = se.local_trading_date AND ds.station = se.station
        WHERE se.market_type = 'HIGH'
        AND se.epoch_status = 'closed'
        AND se.settlement_bucket IS NOT NULL
        AND se.prior_settlement_bucket IS NOT NULL
        AND ds.max_temp_f IS NOT NULL
    """
    params = []
    if start_date:
        query += " AND ds.date_utc >= ?"
        params.append(start_date)
    if end_date:
        query += " AND ds.date_utc <= ?"
        params.append(end_date)

    query += " ORDER BY ds.date_utc, ds.station"  # Changed order for chronological flow

    c.execute(query, params)
    rows = c.fetchall()
    conn.close()

    return rows


def load_climate_indices():
    """Load climate indices: ENSO, NAO, AO, PDO for regime conditioning."""
    indices = {'onidx': {}, 'nao': {}, 'ao': {}}

    # Try to load ONI (Oceanic Niño Index) from saved data
    try:
        with open(f"{CLIMATE_DATA_DIR}/oni.txt", 'r') as f:
            lines = f.readlines()
        for line in lines[1:]:  # Skip header
            parts = line.strip().split()
            if len(parts) >= 4:
                season = parts[0]  # e.g., 'JFM'
                year = parts[1]    # e.g., '2020'
                anom_str = parts[3]
                try:
                    anomaly = float(anom_str)
                    indices['onidx'][f"{season}-{year}"] = anomaly
                except (ValueError, IndexError):
                    continue
    except FileNotFoundError:
        print("WARNING: ONI climate indices not found")

    # Try to load NAO (North Atlantic Oscillation)
    try:
        with open(f"{CLIMATE_DATA_DIR}/nao.dat", 'r') as f:
            lines = f.readlines()
        for line in lines[5:]:  # Skip initial comment lines
            line = line.strip()
            parts = line.split()
            if len(parts) >= 3:
                try:
                    year = parts[0]
                    month = parts[1]  # 1-12
                    anom = float(parts[2])
                    indices['nao'][f"{year}-{month.zfill(2)}"] = anom  # Store as year-month
                except (ValueError, IndexError):
                    continue
    except FileNotFoundError:
        print("WARNING: NAO climate indices not found")

    # Try to load AO (Arctic Oscillation)
    try:
        with open(f"{CLIMATE_DATA_DIR}/ao.dat", 'r') as f:
            lines = f.readlines()
        for line in lines[5:]:  # Skip initial comment lines
            parts = line.strip().split()
            if len(parts) >= 3:
                try:
                    year = parts[0]
                    month = parts[1]  # 1-12
                    anom = float(parts[2])
                    indices['ao'][f"{year}-{month.zfill(2)}"] = anom
                except (ValueError, IndexError):
                    continue
    except FileNotFoundError:
        print("WARNING: AO climate indices not found")

    return indices


def get_actual_direction(settlement, prior):
    """Determine actual direction from settlement data."""
    if settlement > prior:
        return 'up'
    elif settlement < prior:
        return 'down'
    else:
        return 'flat'


def get_regime_condition(date_str, climate_indices):
    """Determine ENSO/AO/NAO regime for a given date."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        year = dt.year
        month = dt.month

        # For ENSO, we often need seasonal values since El Niño/La Niña
        # develops over seasons. Use DJF (Dec-Jan-Feb) or similar.
        # For simplicity, use monthly values and define custom rules
        enso_key = f"DJF-{year}" if month <= 2 else f"JFM-{year}" if month <= 3 else f"FMA-{year}"
        if month in [6, 7, 8]:
            enso_key = f"JJA-{year}"  # Summer
        elif month in [12, 1, 2]:
            enso_key = f"DJF-{year}"  # Winter

        oni = climate_indices.get('onidx', {}).get(enso_key, 0.0)

        nao_key = f"{year}-{month:02d}"
        nao = climate_indices.get('nao', {}).get(nao_key, 0.0)

        ao_key = f"{year}-{month:02d}"
        ao = climate_indices.get('ao', {}).get(ao_key, 0.0)

        return {
            'enso_phase': 'el_nino' if oni > 0.5 else 'la_nina' if oni < -0.5 else 'neutral',
            'nao_phase': 'positive' if nao > 0.5 else 'negative' if nao < -0.5 else 'neutral',
            'ao_phase': 'positive' if ao > 0.5 else 'negative' if ao < -0.5 else 'neutral'
        }

    except:
        # Default to neutral if can't parse date
        return {'enso_phase': 'neutral', 'nao_phase': 'neutral', 'ao_phase': 'neutral'}


# ─── Signal Implementations ──────────────────────────────────────────────

def signal_reversion(station, date, max_temp, settlement, prior):
    """Edge 6: Temperature reversion - extreme temps tend to revert."""
    # Use both current settlement difference and historical climatology
    if prior is None or settlement is None:
        return None, 0.0, 0.5

    # How far from normal is "extreme"? Based on seasonal patterns
    # Use a simplified historical average per station/month as reference
    # For real-world deployment, would use full climatology module
    seasonal_norm = 68.0  # Simplified base for development
    # For real implementation, use climatology_pillar historical data
    diff_from_norm = abs(settlement - seasonal_norm)

    # If settlement is very far from historical norm, expect reversion
    if settlement > 82:  # Hot day → predict DOWN reversion
        return 'down', 0.58, 0.62
    elif settlement < 45:  # Cold day → predict UP reversion
        return 'up', 0.57, 0.61
    elif diff_from_norm > 12:  # Far from seasonal norm
        # Base direction on prior-to-settlement move
        direction = 'down' if settlement > prior else 'up'
        return direction, 0.55, 0.57
    else:
        # Mild temps → stick with trend (momentum)
        return 'up' if settlement > prior else 'down', 0.50, 0.50


def load_nwp_forecasts(start_date=None, end_date=None):
    """Load NWP model forecasts from the NWP database.

    Returns a dict keyed by (station, target_date) → {model: {variable: value}}.
    Used by the forecast_disagreement signal to detect model-to-model divergence.
    """
    if not Path(NWP_DB).exists():
        return {}

    conn = sqlite3.connect(NWP_DB, timeout=10)
    c = conn.cursor()

    query = "SELECT station, target_date, model, variable, value FROM nwp_forecasts WHERE variable = 'temperature_2m_max'"
    params = []
    if start_date:
        query += " AND target_date >= ?"
        params.append(start_date)
    if end_date:
        query += " AND target_date <= ?"
        params.append(end_date)

    c.execute(query, params)
    rows = c.fetchall()
    conn.close()

    nwp = defaultdict(lambda: defaultdict(dict))
    for station, target_date, model, variable, value in rows:
        nwp[(station, target_date)][model] = value
    return nwp


def signal_forecast_disagreement(station, date, max_temp, settlement, prior, nwp_data=None):
    """Edge 5: Forecast disagreement - when NWP models disagree, bet on reversion.

    Uses actual multi-model NWP forecasts when available.  When no NWP data
    exists for this station-date (e.g. historical backtest before NWP coverage),
    the signal does not fire - this is intentional and honest.
    """
    if nwp_data is None:
        return None, 0.0, 0.5

    model_forecasts = nwp_data.get((station, date))
    if not model_forecasts or len(model_forecasts) < 2:
        return None, 0.0, 0.5

    temps = list(model_forecasts.values())
    spread = max(temps) - min(temps)
    if spread < 2.0:  # Models agree within 2°F - no meaningful disagreement
        return None, 0.0, 0.5

    # When models disagree, bet on reversion toward the model consensus mean
    consensus = sum(temps) / len(temps)
    if prior is not None:
        if consensus > prior:
            conf = 0.55 + min(0.15, spread / 20.0)
            return 'up', conf, 0.50 + spread / 100.0
        else:
            conf = 0.55 + min(0.15, spread / 20.0)
            return 'down', conf, 0.50 + spread / 100.0

    return None, 0.0, 0.5


def signal_pressure(station, date, max_temp, settlement, prior):
    """Edge 8: Pressure tendency → weather regime change (simplified)."""
    # Without pressure data in DB, use temperature change as proxy (which isn't ideal)
    # In a real scenario, we'd access real pressure data
    if prior is not None and settlement is not None:
        change = settlement - prior
        daily_volatility = 5.5  # Average temp swing per day (typical)

        # Large changes are often followed by reversion
        if abs(change) > daily_volatility:  # Extreme move → revert
            return 'down' if change > 0 else 'up', 0.60, 0.65
        # Small changes may have more persistence
        elif abs(change) < 1:
            return 'up', 0.52, 0.51  # Default upward bias in seasonal patterns
        else:  # Medium changes follow trend
            return 'up' if change > 0 else 'down', 0.53, 0.55

    return 'up' if settlement is not None and prior is not None and settlement > prior else 'down', 0.5, 0.5


def signal_late_day_momentum(station, date, max_temp, settlement, prior, metar_conn=None):
    """
    Edge P1.2: Late-day METAR momentum signal — HOURLY ONLY.

    Uses actual hourly METAR observations in the 17:00–22:00 UTC window
    to detect temperature momentum and project continuation into the next
    settlement period.

    No daily-aggregate fallback — the signal only fires when hourly data
    shows a meaningful trend (|slope| ≥ threshold).
    """
    if metar_conn is not None:
        direction, confidence, raw_prob = _ldm_hourly_signal(station, date, metar_conn)
        if direction is not None:
            return direction, confidence, raw_prob

    # No hourly data or signal didn't fire — no trade
    return None, 0.0, 0.5


def signal_time_decay(station, date, max_temp, settlement, prior):
    """Edge 2: Time-of-day decay - forecast skill decays after certain times/holidays."""
    # Without specific trading date/time data, use day-of-week and day-of-month effects as proxies
    try:
        dt = datetime.strptime(date, "%Y-%m-%d")
        dayweek = dt.weekday()  # 0=Monday, 6=Sunday
        daymonth = dt.day

        # Weekend effects (Sat/Sun) - lower liquidity and possibly more noise in prices
        # Market may be more inefficient but also harder to predict with low info flow
        if dayweek >= 5:  # Weekend
            # On weekends with less trading news, use historical climatology more than recent trends
            if settlement > prior:
                return 'up', 0.48, 0.52  # Slight bias to follow trend with caution
            else:
                return 'down', 0.48, 0.48
        else:  # Weekday - regular market activity
            # Use primary analysis
            return 'up' if settlement > prior else 'down', 0.53, 0.55
    except:
        pass

    # Default direction with low confidence since we couldn't parse date
    return 'up' if settlement is not None and settlement > (prior or 0) else 'down', 0.45, 0.5


def signal_climate_conditioned(station, date, max_temp, settlement, prior, climate_indices):
    """
    P1.1: Climatology Pillar with regime conditioning
    Incorporate ENSO, NAO, AO, seasonal patterns to adjust base forecasts
    """
    if settlement is None or prior is None:
        return None, 0.0, 0.5

    # Get the current climate regime
    regime = get_regime_condition(date, climate_indices)

    # Base direction from historical patterns
    direction, base_conf, base_prob = signal_reversion(station, date, max_temp, settlement, prior)
    if direction is None:
        return None, 0.0, 0.5

    # Modify based on climate regime
    modifier = 0.0

    # ENSO effects: el_nino often brings warmer winters to northern US, cooler summers
    # la_nina opposite pattern, though varies by region and season
    if regime['enso_phase'] == 'el_nino':
        # Simplified effect: warmer weather bias toward ups, especially in northern cities
        if station in ['KBOS', 'KORD', 'KSEA', 'KMSP', 'KEWR']:
            modifier += 0.03  # Slightly more up bias
        elif station in ['KLAX', 'PHX']:  # May be more dry in SW
            modifier -= 0.02
    elif regime['enso_phase'] == 'la_nina':
        # Opposite effects (inversely)
        if station in ['KBOS', 'KORD', 'KSEA']:
            modifier -= 0.03  # Less up bias
        elif station in ['KLAX']:
            modifier += 0.02

    # NAO effects: positive NAO generally means colder East Coast, milder Western US
    if regime['nao_phase'] == 'positive' and station in ['KBOS', 'EKWB', 'KIAD', 'KDCA']:
        modifier -= 0.04  # More cold weather (Down bias for HIGH markets)

    # AO effects: positive AO generally milder Arctic, affecting US weather patterns
    # More negative AO brings polar vortex conditions
    if regime['ao_phase'] == 'negative' and station in ['KORD', 'KDTW', 'KMSP']:
        modifier -= 0.03  # More likelihood of cold extremes = Down bias

    # Apply climate modifications
    new_conf = max(0.40, min(0.90, base_conf + modifier))
    new_prob = max(0.20, min(0.80, base_prob + modifier/5.0))  # Prob changes more slowly than confidence

    return direction, new_conf, new_prob


def signal_ensemble_agreement(station, date, max_temp, settlement, prior, climate_indices=None):
    """P2: 3-of-4 ensemble agreement gate based on multiple weak signals."""
    signals = []

    # Generate signal candidates with confidence
    sig1 = signal_reversion(station, date, max_temp, settlement, prior)
    if sig1[0] is not None: signals.append(sig1)

    sig2 = signal_pressure(station, date, max_temp, settlement, prior)
    if sig2[0] is not None: signals.append(sig2)

    sig4 = signal_time_decay(station, date, max_temp, settlement, prior)
    if sig4[0] is not None: signals.append(sig4)

    # Only condition on climate if provided
    if climate_indices:
        sig3 = signal_climate_conditioned(station, date, max_temp, settlement, prior, climate_indices)
        if sig3[0] is not None: signals.append(sig3)
    else:
        # Use simpler 4th signal since no climate data
        sig3 = signal_late_day_momentum(station, date, max_temp, settlement, prior)
        if sig3[0] is not None: signals.append(sig3)

    # Check for supermajority direction agreement
    if len(signals) >= 3:  # At least 3 signals active
        up_signals = sum(1 for s in signals if s[0] == 'up')
        down_signals = sum(1 for s in signals if s[0] == 'down')

        if up_signals >= 3:  # 3+ signals agree UP
            # Aggregate confidences and probabilities
            avg_conf = sum(s[1] for s in signals[:3]) / 3
            avg_prob = sum(s[2] for s in signals[:3]) / 3
            return 'up', min(0.9, avg_conf + 0.05), avg_prob
        elif down_signals >= 3:  # 3+ signals agree DOWN
            avg_conf = sum(s[1] for s in signals[:3]) / 3
            avg_prob = sum(1.0 - s[2] for s in signals[:3]) / 3  # Flip direction probabilities
            return 'down', min(0.9, avg_conf + 0.05), avg_prob

    # Not enough agreement - no trade signal
    return None, 0.0, 0.5


def signal_cross_platform_divergence(station, date, max_temp, settlement, prior, market_prices=None):
    """
    P1.3: Cross platform pricing divergence detector.
    In production, would compare Kalshi vs Polymarket prices, looking for inefficiency.
    """
    # Since we don't have live market price streams in this context,
    # we'll simulate by considering if settlement prices seem extreme vs historical range
    if settlement is None or prior is None:
        return None, 0.0, 0.5

    # For demo purpose: if settlement is extremely different from typical levels for this station
    # it might represent pricing inefficiency that can be arbitraged against historical norms
    # (In reality would be between exchange prices, not vs history, but concept is similar for finding inefficiencies)

    # Use a simple approach - is settlement extremely out of line with prior (more than 2SD away)?
    # This would indicate a possible divergence worth investigating
    historical_volatility = 4.0  # Assumed typical day-to-day change
    change = settlement - prior
    expected_range = historical_volatility * 1.96  # 95% confidence interval

    if abs(change) > expected_range:  # Significant move outside normal range - possible inefficiency
        # This represents a kind of "pricing divergence" to exploit
        return 'down' if change > 0 else 'up', 0.58, 0.52  # Expect reversion
    elif abs(change) < max(1.0, historical_volatility * 0.5):  # Very small move - possible underreaction
        # In small change environment, momentum might persist
        return 'up' if change >= 0 else 'down', 0.52, 0.53

    return None, 0.0, 0.5


# ─── Main Backtest ───────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Split-backtest harness for weather signals')
    parser.add_argument('--start', type=str, help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end', type=str, help='End date (YYYY-MM-DD)')
    parser.add_argument('--station', type=str, help='Single station (default: all)')
    parser.add_argument('--verbose', action='store_true', help='Show detailed output')
    args = parser.parse_args()

    print("=" * 100)
    print("Enhanced Split-Backtest Harness - Individual Signal Isolation (v2.0)")
    print(f"Timestamp: {datetime.now().isoformat()}")
    print("=" * 100)
    print()

    # Load data - settlement epochs with associated METAR data
    print("Loading settlement data...")
    rows = load_settlement_data(args.start, args.end)
    print(f"Loaded {len(rows)} settlement records across {len(set(row[0] for row in rows))} stations")

    if not rows:
        print("ERROR: No data found")
        sys.exit(1)

    # Load climate indices for regime-conditioned signals
    print("Loading climate indices...")
    climate_indices = load_climate_indices()
    print(f"Loaded climate regimes: {len(climate_indices.get('onidx', {}))} ONI entries, "
          f"{len(climate_indices.get('nao', {}))} NAO entries, {len(climate_indices.get('ao', {}))} AO entries")

    # Load NWP forecasts for forecast_disagreement signal
    print("Loading NWP forecasts...")
    nwp_data = load_nwp_forecasts(args.start, args.end)
    print(f"Loaded NWP forecasts for {len(nwp_data)} station-date combinations")

    # Open a shared METAR DB connection for the hourly late-day momentum signal
    metar_conn = sqlite3.connect(METAR_DB, timeout=10)
    print(f"METAR DB connected for hourly late_day_momentum signal")
    print()

    # Initialize signal results
    signal_functions = {
        'reversion': lambda s, d, m, se, pr: signal_reversion(s, d, m, se, pr),
        'forecast_disagreement': lambda s, d, m, se, pr, nwp=nwp_data: signal_forecast_disagreement(s, d, m, se, pr, nwp),
        'pressure': lambda s, d, m, se, pr: signal_pressure(s, d, m, se, pr),
        'time_decay': lambda s, d, m, se, pr: signal_time_decay(s, d, m, se, pr),
        # --- Late-day momentum: uses hourly METAR via metar_conn ---
    'late_day_momentum': lambda s, d, m, se, pr: signal_late_day_momentum(s, d, m, se, pr, metar_conn=metar_conn),
        'ensemble_agreement': lambda s, d, m, se, pr: signal_ensemble_agreement(s, d, m, se, pr),
        'climate_conditioned': lambda s, d, m, se, pr, ci=climate_indices:
                               signal_climate_conditioned(s, d, m, se, pr, ci),
        'cross_platform_divergence': lambda s, d, m, se, pr: signal_cross_platform_divergence(s, d, m, se, pr)
    }

    results = {name: SignalResult(name) for name in signal_functions}

    # Group data by station for processing
    by_station = defaultdict(list)
    for row in rows:
        by_station[row[0]].append(row)

    # Run backtest for each station
    all_stations = [args.station] if args.station else STATIONS
    processed_stations = [s for s in all_stations if s in by_station]

    if args.verbose:
        print(f"Processing {len(processed_stations)}/{len(all_stations)} selected stations")

    for station_idx, station in enumerate(processed_stations):
        if args.verbose:
            print(f"Processing station {station_idx+1}/{len(processed_stations)}: {station}")

        station_rows = by_station[station]
        total_days = len(station_rows)

        # Initialize total days counters
        for name, result in results.items():
            result.total_days += total_days

        # Process each trading day for this station. Signals are generated from
        # this settled day and scored against the next settled direction for the
        # same station. Scoring settlement-vs-prior against the same row leaks
        # the answer and creates degenerate 100% / infinite-PF metrics for
        # signals that encode the current move.
        for idx, row in enumerate(station_rows[:-1]):
            if args.verbose and idx % 500 == 0:  # Show progress for large datasets
                print(f"  Processing row {idx+1}/{len(station_rows)} for {station}")

            st, date, max_temp, min_temp, settlement, prior, trade_date = row

            # Determine realized direction for the next tradable observation.
            next_row = station_rows[idx + 1]
            _, _, _, _, next_settlement, next_prior, _ = next_row
            actual_dir = get_actual_direction(next_settlement, next_prior)
            if actual_dir == 'flat': continue  # Skip when no clear direction

            # Get predictions from each signal and record trades
            for name, func in signal_functions.items():
                try:
                    if name == 'climate_conditioned':
                        pred_dir, confidence, raw_prob = func(st, date, max_temp, settlement, prior, climate_indices)
                    else:
                        pred_dir, confidence, raw_prob = func(st, date, max_temp, settlement, prior)

                    # If signal activated, record the trade
                    if pred_dir is not None and pred_dir != 'flat':
                        results[name].add_trade(st, date, pred_dir, actual_dir, confidence, raw_prob)
                    else:
                        results[name].record_no_trade()

                except Exception as e:
                    results[name].record_no_trade()
                    if args.verbose:
                        print(f"ERROR in signal {name}: {e}")

    # Compute and display metrics for each signal
    print(f"\n{'Signal':<25} {'Trades':>6} {'NoTrd':>6} {'Acc':>6} {'CI':>12} {'Brier':>7} {'Sharpe':>8} {'Cov':>6} {'PF':>7} {'AvgPnL':>8} {'P-Value':>9}")
    print("-" * 100)

    all_metrics = []

    for name in signal_functions:
        m = results[name].compute_metrics()
        all_metrics.append((m['name'], m))

        # Format CI for display
        ci_str = f"{m['accuracy_ci'][0]:.2%}-{m['accuracy_ci'][1]:.2%}"
        pval_str = f"p<{0.05 if m['statistical_significance_p'] < 0.05 else 0.01 if m['statistical_significance_p'] < 0.01 else 'ns'}"

        pf_str = "inf" if math.isinf(m['profit_factor']) else f"{m['profit_factor']:.2f}"

        print(f"{m['name']:<25} {m['trades']:>6} {m['non_trades']:>6} {m['accuracy']:>6.2%} {ci_str:>12} {m['brier']:>7.4f} "
              f"{m['sharpe']:>8.2f} {m['coverage']:>6.2%} {pf_str:>7} {m['avg_pnl']:>8.3f} {pval_str:>9}")

    print("-" * 100)
    print()

    # Generate recommendations based on metrics
    print("=== Signal Recommendations ===")

    for name in signal_functions:
        m = results[name].compute_metrics()

        # Determine if signal is worth including
        is_significant = m['statistical_significance_p'] < 0.05
        has_good_accuracy = m['accuracy'] > 0.55
        has_reasonable_sharpe = m['sharpe'] > 0.3
        has_sufficient_trades = m['trades'] >= MIN_TRADES
        has_positive_expectancy = m['avg_pnl'] > 0.0
        good_profit_factor = m['profit_factor'] > 1.2

        if m['trades'] < MIN_TRADES:
            print(f"  {name:25s}: INSUFFICIENT DATA ({m['trades']} trades, need at least {MIN_TRADES})")
        elif has_good_accuracy and is_significant and has_reasonable_sharpe:
            performance_desc = f"ACC={m['accuracy']:.2%}, SH={m['sharpe']:.2f}, PF={m['profit_factor']:.1f}"
            print(f"  {name:25s}: ✅ KEEP - Strong Performer - {performance_desc}")
        elif m['accuracy'] > 0.53 and has_positive_expectancy:
            performance_desc = f"ACC={m['accuracy']:.2%}, AVG_PNL={m['avg_pnl']:.3f}"
            print(f"  {name:25s}: ⚠ PROMISING - Marginal Performer - {performance_desc}")
        elif has_sufficient_trades and not is_significant and m['accuracy'] < 0.5:
            performance_desc = f"ACC={m['accuracy']:.2%}, p={m['statistical_significance_p']:.3f}"
            print(f"  {name:25s}: ❌ AVOID - Anti-performer - {performance_desc}")
        elif m['accuracy'] < 0.5 or (has_sufficient_trades and not has_positive_expectancy):
            performance_desc = f"ACC={m['accuracy']:.2%}, AVG_PNL={m['avg_pnl']:.3f}"
            print(f"  {name:25s}: ❌ SKIP - Poor Performer - {performance_desc}")
        else:
            performance_desc = f"ACC={m['accuracy']:.2%}, SH={m['sharpe']:.2f}"
            print(f"  {name:25s}: ❓ EVALUATE - Neutral/Mixed - {performance_desc}")

    # Summary stats
    print("\n=== Summary ===")
    print(f"Date Range: {args.start or '[earliest]'} to {args.end or '[latest]'}")
    print(f"Stations: {len(processed_stations)} of {len(all_stations)} selected ({'all' if not args.station else 'filtered'})")
    print(f"Signal Types Tested: {len(signal_functions)}")
    print(f"Total Records Processed: {len(rows)}")

    print(f"\nBest Performing Signal: {max(((name, m) for name, m in all_metrics if m['trades'] >= MIN_TRADES), key=lambda x: x[1]['accuracy'], default=('[no data]', {'accuracy':0}))}")

    # Close the METAR connection
    metar_conn.close()

    print("\nNote: This script integrates hourly METAR late-day momentum, NWP data, and regime analysis.")
    print("After running this backtest with current data, consider:")
    print("1. Run nwp_backfill_30d.py to extend historical forecasts")
    print("2. Re-run this script to incorporate model-to-model disagreement analysis")
    print("3. Use outputs to focus ensemble efforts on high-performing signals")


if __name__ == "__main__":
    main()
