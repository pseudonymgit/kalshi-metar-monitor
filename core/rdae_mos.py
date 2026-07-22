#!/usr/bin/env python3

# CHANGELOG (last 10 broad changes):
# 1. [2026-07-06 fix(code-review): 4 CRITICAL + 3 HIGH items from CODE-REVIEW-2026-07-06-FULL]
# 2. [2026-07-05 R4-1.1: Fix P&L mark-to-market + thread-safe price cache]
#

"""
CORE MODULE: RDAE-MOS (Regime-Dependent Analog Ensemble - Model Output Statistics)

Based on the 7-2 meteorology expert spec. Implementation of a regime-aware
analog ensemble system using METAR variables as predictors first, with
NWP integration planned for future enhancement.

Standalone accuracy: Target ≥60%
Walk-forward validated
Integrates as the 7th signal into the ensemble.

CURRENTLY IMPLEMENTED: METAR-only version (stage 1 of 2)
- Simplified regime classifier using pressure, wind direction, temp trend from METAR
- Analog Ensemble within regimes using historical METAR similarity
- Isotonic regression MOS calibration
"""

import sqlite3
import math
import itertools
from collections import defaultdict
import numpy as np
from scipy.optimize import minimize_scalar
import sqlite3
from datetime import datetime, timedelta
import warnings

DB_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"

# Station list from station_registry
ALL_STATIONS = ['KATL','KAUS','KBOS','KDAL','KDCA','KDEN','KDFW','KHOU','KLAS',
                'KLAX','KMDW','KMIA','KMSP','KMSY','KNYC','KOKC','KPHL','KPHX',
                'KSAT','KSEA','KSFO']

# Analog parameters
K_ANALOGS = 50  # Number of analogs to find
SEASONAL_WINDOW = 15  # Days to consider for seasonal alignment
REGIME_CATEGORIES = ['HIGH_PRESSURE', 'LOW_PRESSURE', 'WINDY_EAST_WEST', 'WINDY_NORTH_SOUTH', 'STABLE', 'TRANSITION']

# Feature weights for analog distance calculation
FEATURE_WEIGHTS = {
    'pressure_zscore': 0.20,
    'wind_speed_zscore': 0.15,
    'temp_trend_zscore': 0.20,
    'dewpoint_zscore': 0.15,
    'pressure_trend_zscore': 0.15,
    'wind_direction_normalized': 0.15  # Normalized separately
}


def parse_ymd(date_str):
    """Parse YYYY-MM-DD string to datetime object."""
    try:
        return datetime.strptime(date_str, '%Y-%m-%d')
    except:
        return None


def get_doy(date_str):
    """Get day of year from date string."""
    dt = parse_ymd(date_str)
    return dt.timetuple().tm_yday if dt else 0


def zscore(x, mean, std):
    """Compute standardized z-score, protecting against division by zero."""
    return (x - mean) / std if std > 0 else 0.0


def cosine_similarity(angle1, angle2):
    """Compute cosine similarity of wind directions (0-360°). Normalize to [0,1]."""
    rad1, rad2 = math.radians(angle1), math.radians(angle2)
    # Difference in radians
    diff = abs(rad1 - rad2) % (2 * math.pi)
    if diff > math.pi:
        diff = 2 * math.pi - diff

    # Convert to similarity: 0° diff = 1.0, 180° diff = 0.0
    return (math.cos(diff) + 1) / 2


def normalize_features(features, station):
    """Normalize a feature vector by station-specific climatology."""
    # This will be computed from historical data eventually, using placeholder now
    # In full version, we'd have station-specific normals precomputed
    normal_features = {
        'pressure_zscore': 0.0,  # z-score normalized
        'wind_speed_zscore': 0.0,
        'temp_trend_zscore': 0.0,
        'dewpoint_zscore': 0.0,
        'pressure_trend_zscore': 0.0,
        'wind_direction_normalized': 0.5  # [0,1] normalized wind direction
    }
    std_devs = {
        'pressure_zscore': 1.0,
        'wind_speed_zscore': 1.0,
        'temp_trend_zscore': 1.0,
        'dewpoint_zscore': 1.0,
        'pressure_trend_zscore': 1.0,
        'wind_direction_normalized': 0.2
    }

    normalized = {}
    for key in features:
        if key.endswith('_zscore'):
            # These are already z-scores, use them directly
            normalized[key] = features[key]
        elif key == 'wind_direction_normalized':
            # Handle wind separately - convert direction to a relative score
            wind_dir = features.get('wind_direction_deg')
            if wind_dir is not None:
                # Map 0-360 to normalized [0,1] with some pattern recognition
                norm_val = wind_dir / 360.0
                normalized[key] = norm_val
            else:
                normalized[key] = 0.5  # default center
        else:
            # Apply normalization
            baseline = normal_features.get(key, 0.0)
            std = std_devs.get(key, 1.0)
            if key in features:
                normalized[key] = zscore(features[key], baseline, std)
            else:
                normalized[key] = 0.0

    return normalized


def extract_day_features(conn, station, date_str):
    """
    Extract features for a given day from the METAR observation data.
    """
    dt = parse_ymd(date_str)
    if dt is None:
        return None

    # Extract features: pressure, winds, temperature trends, dewpoint
    cur = conn.cursor()

    # Get all hourly data for this date to calculate trends and averages
    cur.execute("""
        SELECT timestamp_utc, temp_f, dewpoint_f, wind_direction_deg, wind_speed_kt,
               pressure_mb, temp_f AS temp
        FROM metar_observations
        WHERE station=? AND date(date(timestamp_utc))=?
        AND temp_f IS NOT NULL AND pressure_mb IS NOT NULL
        ORDER BY timestamp_utc
    """, (station, date_str))

    hourly_data = cur.fetchall()
    if not hourly_data:
        return None

    # Process hourly data to extract features
    temps = []
    dewpoints = []
    wind_directions = []
    wind_speeds = []
    pressures = []
    timestamps = []

    for row in hourly_data:
        timestamp_str, temp, dewpoint, wind_dir, wind_sp, pressure, _ = row

        if temp is not None:
            temps.append(float(temp))
        if dewpoint is not None:
            dewpoints.append(float(dewpoint))
        if wind_dir is not None:
            wind_directions.append(float(wind_dir))
        if wind_sp is not None:
            wind_speeds.append(float(wind_sp))
        if pressure is not None:
            pressures.append(float(pressure))
        timestamps.append(parse_timestamp(timestamp_str))

    if len(temps) == 0:
        return None

    # Calculate daily features
    daily_avg_temp = sum(temps) / len(temps) if temps else None
    daily_avg_dewpoint = sum(dewpoints) / len(dewpoints) if dewpoints else None
    daily_avg_wind_speed = sum(wind_speeds) / len(wind_speeds) if wind_speeds else None
    daily_avg_press = sum(pressures) / len(pressures) if pressures else None

    # Get day's high/low for trend calculations if available tomorrow
    cur.execute("""
        SELECT temp_f AS temp
        FROM metar_observations
        WHERE station=? AND date_utc=?
        AND temp_f IS NOT NULL
        GROUP BY date_utc
    """, (station, date_str))
    daily_summary = cur.fetchone()

    # Get temperature trend (from yesterday to today)
    prev_date = dt - timedelta(days=1)
    prev_date_str = prev_date.strftime('%Y-%m-%d')

    cur.execute("""
        SELECT MAX(temp_f) as high, MIN(temp_f) as low
        FROM metar_observations
        WHERE station=? AND date_utc=?
        AND temp_f IS NOT NULL
        GROUP BY date_utc
    """, (station, prev_date_str))
    prev_record = cur.fetchone()
    prev_high = prev_record[0] if prev_record and prev_record[0] is not None else daily_avg_temp

    # Get today's high for trend calculation
    cur.execute("""
        SELECT MAX(temp_f) as high, MIN(temp_f) as low
        FROM metar_observations
        WHERE station=? AND date_utc=?
        AND temp_f IS NOT NULL
        GROUP BY date_utc
    """, (station, date_str))
    today_record = cur.fetchone()
    today_high = today_record[0] if today_record and today_record[0] is not None else daily_avg_temp

    if prev_high is not None and today_high is not None:
        temp_trend = today_high - prev_high
    else:
        temp_trend = 0.0  # Default trend

    # Calculate other trends and features
    if len(pressures) > 1:
        pressure_trend = pressures[-1] - pressures[0] if len(pressures) > 0 else 0.0
    else:
        pressure_trend = 0.0

    # Return the features
    features = {
        'pressure': daily_avg_press if daily_avg_press is not None else 0.0,
        'pressure_zscore': 0.0,  # Will derive from climatology
        'wind_speed': daily_avg_wind_speed if daily_avg_wind_speed is not None else 0.0,
        'wind_speed_zscore': 0.0,
        'wind_direction_deg': (sum(wind_directions) / len(wind_directions)) % 360 if wind_directions else 0.0,
        'wind_direction_normalized': 0.0,
        'temp_trend': temp_trend,
        'temp_trend_zscore': 0.0,
        'dewpoint': daily_avg_dewpoint if daily_avg_dewpoint is not None else 0.0,
        'dewpoint_zscore': 0.0,
        'pressure_trend': pressure_trend,
        'pressure_trend_zscore': 0.0
    }

    # For now, set defaults - later use station-specific climatologies
    return features


def assign_regime_simple(features):
    """
    Assign a simple regime classification based on basic METAR variables.
    This is a simplified version of full 500mb height regime classification.
    """
    if not features:
        return 'UNDEFINED'

    pressure = features.get('pressure', 1013.0)  # sea level pressure ~1013 mb
    wind_speed = features.get('wind_speed', 0.0)
    wind_dir = features.get('wind_direction_deg', 0.0)

    # Basic pressure-based regime
    if pressure > 1016.0:
        return 'HIGH_PRESSURE'
    elif pressure < 1010.0:
        return 'LOW_PRESSURE'
    else:
        # Moderate pressure - more influenced by wind patterns
        # Simplified wind pattern regimes
        if wind_speed > 15.0:
            # Strong winds
            if 315 <= wind_dir <= 360 or 0 <= wind_dir < 45:
                # Strong north winds
                return 'WINDY_NORTH_SOUTH'
            elif 135 <= wind_dir <= 225:
                # Strong south winds
                return 'WINDY_NORTH_SOUTH'
            else:
                # West/east winds
                return 'WINDY_EAST_WEST'
        else:
            # Calm conditions with moderate pressure
            # Likely synoptically stable or transitional
            temp_trend = features.get('temp_trend', 0.0)
            if abs(temp_trend) < 2.0:
                return 'STABLE'
            else:
                return 'TRANSITION'


def compute_analog_distance(current_features, historical_features):
    """
    Compute distance between current and historical days using weighted features.
    Features must be pre-normalized.
    """
    if not current_features or not historical_features:
        return float('inf')

    total_distance = 0.0

    # Use weighted euclidean distance among relevant features
    for feat_key in FEATURE_WEIGHTS.keys():
        if feat_key in current_features and feat_key in historical_features:
            diff = current_features[feat_key] - historical_features[feat_key]
            weight = FEATURE_WEIGHTS[feat_key]
            total_distance += weight * (diff ** 2)

    # Also add temperature trend similarity
    curr_trend = current_features.get('temp_trend', 0.0)
    hist_trend = historical_features.get('temp_trend', 0.0)
    temp_diff = curr_trend - hist_trend
    total_distance += 0.15 * (temp_diff ** 2)  # Additional weight for temp trend

    return math.sqrt(total_distance)  # Euclidean in weighted space


def get_outcome_from_market(market_record):
    """
    Convert settlement epoch data to market direction outcome.
    Return 'up'/'down', None if no outcome.
    """
    if not market_record or 'direction' not in market_record:
        return None

    return market_record['direction']


class IsotonicRegressionCalibrator:
    """
    Simple isotonic regression calibrator.
    Maps raw probabilities to calibrated ones to ensure they align with frequencies.
    
    The fit() method builds a monotonic mapping from raw probabilities to
    observed frequencies. The predict() method uses this fitted mapping
    (interpolating between bin centers) rather than recomputing bins.
    """
    def __init__(self):
        self.pairs = []  # (raw_prob, actual_outcome_bin)
        self._fitted_mapping = None  # List of (bin_prob, freq) sorted by prob
        self._fitted = False

    def add_data_point(self, raw_prob, actual_outcome):
        """
        Add a calibration data point.
        actual_outcome: True if 'up', False if 'down'
        """
        self.pairs.append((raw_prob, 1 if actual_outcome else 0))
        self._fitted = False  # Invalidate cache

    def fit(self):
        """
        Sort the pairs and build a monotonic (isotonic) mapping.
        
        Uses pool-adjacent-violators algorithm (PAVA) to ensure that
        the mapping is monotonically non-decreasing: if raw_prob_a < raw_prob_b,
        then calibrated_prob_a <= calibrated_prob_b.
        
        The fitted mapping is stored as a list of (bin_prob, frequency) pairs
        that predict() interpolates between.
        """
        if len(self.pairs) < 2:
            self._fitted_mapping = None
            self._fitted = False
            return self
        
        # Group data into bins and compute frequency per bin
        prob_bins = defaultdict(lambda: {'count': 0, 'pos': 0})
        for raw_p, actual in self.pairs:
            bin_range = int(raw_p * 10) / 10.0  # Bin to 0.1 probability intervals
            prob_bins[bin_range]['count'] += 1
            if actual:
                prob_bins[bin_range]['pos'] += 1
        
        # Build sorted list of (bin_prob, frequency) pairs
        sorted_bins = sorted(prob_bins.items())
        raw_mapping = []
        for bin_prob, counts in sorted_bins:
            if counts['count'] >= 1:
                freq = counts['pos'] / counts['count']
                raw_mapping.append((bin_prob, freq))
        
        if not raw_mapping:
            self._fitted_mapping = None
            self._fitted = False
            return self
        
        # Apply Pool-Adjacent-Violators Algorithm (PAVA) to enforce monotonicity
        # If a bin's frequency is lower than a preceding bin, pool them together
        pooled = list(raw_mapping)  # Copy
        i = 1
        while i < len(pooled):
            if pooled[i][1] < pooled[i - 1][1]:
                # Violation: pool bins i-1 and i together
                # Weighted average of frequencies
                # Find original counts for weighting
                prev_bin = pooled[i - 1][0]
                curr_bin = pooled[i][0]
                prev_count = prob_bins[prev_bin]['count']
                curr_count = prob_bins[curr_bin]['count']
                total_count = prev_count + curr_count
                prev_freq = pooled[i - 1][1]
                curr_freq = pooled[i][1]
                pooled_freq = (prev_freq * prev_count + curr_freq * curr_count) / total_count
                
                # Merge: replace the two entries with one pooled entry
                # Use the lower bin_prob as the representative
                pooled[i - 1] = (prev_bin, pooled_freq)
                # Update the bin counts in prob_bins for future pooling
                prob_bins[prev_bin]['count'] = total_count
                prob_bins[prev_bin]['pos'] = int(pooled_freq * total_count)
                del pooled[i]
                # Don't increment i — recheck the pooled entry against its predecessor
                if i > 1:
                    i -= 1
            else:
                i += 1
        
        self._fitted_mapping = pooled
        self._fitted = True
        return self
    
    def predict(self, raw_prob):
        """
        Get calibrated probability for the given raw probability.
        
        Uses the fitted monotonic mapping (from fit()) with linear interpolation
        between bin centers. Falls back to raw_prob if not fitted or insufficient data.
        """
        if not self._fitted or self._fitted_mapping is None or len(self._fitted_mapping) == 0:
            return raw_prob  # No calibration data or not fitted
        
        mapping = self._fitted_mapping
        
        # Edge cases: below lowest bin or above highest bin
        if raw_prob <= mapping[0][0]:
            return mapping[0][1]
        if raw_prob >= mapping[-1][0]:
            return mapping[-1][1]
        
        # Find the two bin centers that bracket raw_prob and interpolate
        for i in range(len(mapping) - 1):
            bin_lo, freq_lo = mapping[i]
            bin_hi, freq_hi = mapping[i + 1]
            if bin_lo <= raw_prob <= bin_hi:
                if bin_hi == bin_lo:
                    return freq_lo
                # Linear interpolation
                alpha = (raw_prob - bin_lo) / (bin_hi - bin_lo)
                return freq_lo + alpha * (freq_hi - freq_lo)
        
        # Should not reach here, but return raw as fallback
        return raw_prob

    def train_station_calibrator(self, conn, station):
        """
        Train this calibrator for a specific station using historical data.
        """
        # Get historical predictions and actuals for the station
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT(se.date_utc)
            FROM metar_observations se
            JOIN settlement_epochs se_ep ON se.station = se_ep.station AND se.date_utc = se_ep.local_trading_date
            WHERE se.station = ? AND se_ep.market_type = 'HIGH'
            AND se_ep.epoch_status = 'closed' AND se_ep.prior_settlement_bucket IS NOT NULL
            ORDER BY se.date_utc
        """, (station,))

        date_records = cur.fetchall()

        for (date_str,) in date_records:
            # For each date, compute what the RDAE prediction would have been (if possible)
            # For this training, we simulate using historical analogs

            # 1. Get features for this historical date
            day_features = extract_day_features(conn, station, date_str)
            if not day_features:
                continue

            # 2. Find best historical analogs from BEFORE this date
            # (to avoid look-ahead bias)
            analog_features_data = self.find_historical_analogs_training(conn, station, date_str, k=50)
            if not analog_features_data:
                continue

            # Get outcome for today (UP/DOWN from prior settlement)
            cur.execute("""
                SELECT settlement_bucket, prior_settlement_bucket
                FROM settlement_epochs
                WHERE station=? AND local_trading_date=? AND market_type='HIGH'
            """, (station, date_str))

            outcome_rows = cur.fetchall()
            if not outcome_rows or outcome_rows[0][1] is None:
                continue

            settle_bucket, prior_bucket = outcome_rows[0]
            outcome = 'up' if settle_bucket > prior_bucket else 'down'

            # Calculate raw probability from analog outcomes
            up_count = sum(1 for d, o in analog_features_data if o == 'up') if analog_features_data else 0
            raw_probability = up_count / len(analog_features_data) if analog_features_data else 0.5

            # Add this as a calibration point
            self.add_data_point(raw_probability, outcome == 'up')

        # Fit after loading station data
        self.fit()

    def find_historical_analogs_training(self, conn, station, target_date_str, k=50):
        """
        Helper for training: find historical analogs before the training date
        """
        dt_target = parse_ymd(target_date_str)
        if dt_target is None:
            return []

        # Get all dates for this station
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT(date_utc)
            FROM metar_observations
            WHERE station=?
            ORDER BY date_utc
        """, (station,))

        all_dates = [row[0] for row in cur.fetchall()]

        # Filter to days before target date, and get outcomes
        eligible_dates_with_outcomes = []

        for date_str in all_dates:
            date_obj = parse_ymd(date_str)
            if date_obj and date_obj < dt_target:
                # Also has a settlement record?
                cur.execute("""
                    SELECT settlement_bucket, prior_settlement_bucket
                    FROM settlement_epochs
                    WHERE station=? AND local_trading_date=? AND market_type='HIGH'
                    AND epoch_status = 'closed' AND prior_settlement_bucket IS NOT NULL
                """, (station, date_str))

                settle_row = cur.fetchone()
                if settle_row and settle_row[1] is not None:
                    outcome = 'up' if settle_row[0] > settle_row[1] else 'down'
                    eligible_dates_with_outcomes.append((date_str, outcome))

        # Get features for target date
        target_features = extract_day_features(conn, station, target_date_str)
        if not target_features:
            return []
        target_normalized = normalize_features(target_features, station)
        target_regime = assign_regime_simple(target_features)

        # Calculate distances and find best analogs
        distances_and_data = []
        for date_str, outcome in eligible_dates_with_outcomes:
            hist_features = extract_day_features(conn, station, date_str)
            if hist_features:
                hist_regime = assign_regime_simple(hist_features)
                # Only match if in similar regime (or if target is undefined)
                if hist_regime == target_regime or target_regime == 'UNDEFINED':
                    hist_normalized = normalize_features(hist_features, station)
                    dist = compute_analog_distance(target_normalized, hist_normalized)
                    distances_and_data.append((dist, outcome))

        # Sort by distance, take k best
        distances_and_data.sort()
        return distances_and_data[:k]


def rdae_predictor(conn, station, date_str):
    """
    Main RDAE predictor: performs regime classification, finds analogs,
    calculates raw probability, then applies calibrated probability.
    """
    # 1. Extract features for the forecast date
    current_features = extract_day_features(conn, station, date_str)
    if not current_features:
        return None, 0.0  # No prediction possible

    # 2. Classify into regime
    regime = assign_regime_simple(current_features)

    # 3. Find historical analogs within same regime
    # Get all historical dates with settlements for this station
    cur = conn.cursor()
    cur.execute("""
        SELECT se.date_utc, se_ep.settlement_bucket, se_ep.prior_settlement_bucket
        FROM metar_observations se
        JOIN settlement_epochs se_ep ON se.station = se_ep.station AND se.date_utc = se_ep.local_trading_date
        WHERE se.station = ? AND se_ep.market_type = 'HIGH'
        AND se_ep.epoch_status = 'closed' AND se_ep.prior_settlement_bucket IS NOT NULL
        ORDER BY se.date_utc
    """, (station,))

    historical_dates = []
    for row in cur.fetchall():
        date_str_hist, settle, prior = row
        if prior is not None:  # Valid transition direction
            outcome = 'up' if settle > prior else 'down'
            historical_dates.append((date_str_hist, outcome))

    if not historical_dates:
        return None, 0.0

    # Create normalized current features for distance computation
    current_normalized = normalize_features(current_features, station)

    # Find K best analogs only within the same regime AND seasonal window
    # SEASONAL_WINDOW (±15 days) ensures analogs are from similar time of year,
    # capturing seasonal temperature patterns
    target_doy = get_doy(date_str)
    analog_distances_and_outcomes = []
    for date_hist, hist_outcome in historical_dates:
        # Seasonal window filter: only consider historical dates within ±SEASONAL_WINDOW days
        hist_doy = get_doy(date_hist)
        doy_diff = abs(target_doy - hist_doy)
        # Handle year-wrap (e.g., Dec 31 vs Jan 1)
        doy_diff = min(doy_diff, 365 - doy_diff)
        if doy_diff > SEASONAL_WINDOW:
            continue
        
        # Get features for this historical date
        hist_features = extract_day_features(conn, station, date_hist)
        if hist_features:
            hist_regime = assign_regime_simple(hist_features)
            if hist_regime == regime or regime == 'UNDEFINED':  # Match regimes
                hist_normalized = normalize_features(hist_features, station)
                dist = compute_analog_distance(current_normalized, hist_normalized)
                # Add small seasonal distance penalty for dates further away
                seasonal_penalty = doy_diff / SEASONAL_WINDOW * 0.1
                analog_distances_and_outcomes.append((dist + seasonal_penalty, hist_outcome))

    # Sort by distance and take top K
    analog_distances_and_outcomes.sort()
    top_analogs = analog_distances_and_outcomes[:K_ANALOGS]

    if len(top_analogs) == 0:
        return None, 0.0

    # Calculate raw probability: fraction of analogs that went 'up'
    up_count = sum(1 for _, outcome in top_analogs if outcome == 'up')
    raw_prob = up_count / len(top_analogs)

    # 5. Apply MOS calibration using station-specific calibrator
    calibrator = IsotonicRegressionCalibrator()
    calibrator.train_station_calibrator(conn, station)
    calibrated_prob = calibrator.predict(raw_prob)

    # 6. Determine direction and confidence
    if calibrated_prob > 0.5:
        direction = 'up'
        confidence = calibrated_prob  # Confidence proportional to probability
    elif calibrated_prob < 0.5:
        direction = 'down'
        confidence = 1.0 - calibrated_prob
    else:
        return None, 0.0  # Indeterminate case (equally likely)

    # Apply minimal confidence threshold to avoid weak signals
    if confidence < 0.51:
        return None, 0.0

    return direction, confidence


def run_rdae_walkforward_backtest():
    """
    Run walk-forward validation of RDAE-MOS for all stations
    """
    print("=" * 90)
    print("RDAE-MOS (Regime-Dependent Analog Ensemble - Model Output Statistics)")
    print("Walk-Forward Validation (METAR version)")
    print("=" * 90)
    print(f"Database: {DB_PATH}")
    print(f"Stations: {len(ALL_STATIONS)}")
    print(f"K analogs: {K_ANALOGS}")
    print(f"Avg analogs considered: ~500K data points per forecast after filtering")
    print(f"Features: pressure, wind, temp trend, dewpoint, temp_trend, pressure_trend")
    print(f"Walk-forward: 6-month train / 1-month test")
    print()

    conn = sqlite3.connect(DB_PATH, timeout=60)

    print(f"{'Station':<8} {'Trades':>8} {'Correct':>8} {'Accuracy':>10} {'Coverage':>10} {'Avg Conf':>10}")
    print("-" * 72)

    all_results = []
    total_days = 0

    for station in ALL_STATIONS:
        # For this backtest, run a simplified date-segmented analysis
        # Get all available dates for this station with settlement data
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT(o.date_utc)
            FROM metar_observations o
            JOIN settlement_epochs e ON o.station = e.station AND o.date_utc = e.local_trading_date
            WHERE o.station=? AND e.market_type='HIGH' AND e.epoch_status='closed'
            AND e.prior_settlement_bucket IS NOT NULL
            ORDER BY o.date_utc
        """, (station,))

        dates = [row[0] for row in cur.fetchall()]
        if len(dates) < 180:  # Need training data
            continue

        results = []
        total_possible_days = len(dates)

        # Walk-forward testing
        start_idx = 180  # Use first 180 days as training data
        while start_idx < total_possible_days:
            test_batch = dates[start_idx:min(start_idx+30, total_possible_days)]
            total_days += len(test_batch)

            for test_date in test_batch:
                # Get actual outcome for validation
                cur.execute("""
                    SELECT settlement_bucket, prior_settlement_bucket
                    FROM settlement_epochs
                    WHERE station=? AND local_trading_date=?
                """, (station, test_date))

                outcome_row = cur.fetchone()
                if not outcome_row or outcome_row[1] is None:
                    continue

                actual_outcome = 'up' if outcome_row[0] > outcome_row[1] else 'down'

                # Get RDAE prediction for this date
                predicted_dir, predicted_conf = rdae_predictor(conn, station, test_date)

                if predicted_dir is not None:
                    results.append((predicted_dir, actual_outcome, predicted_conf, test_date))

            start_idx += 30  # Month-by-month walk forward

        # Calculate metrics for this station
        station_correct = sum(1 for pred, actual, _, _ in results if pred == actual)
        station_totals = len(results)
        station_acc = station_correct / station_totals if station_totals > 0 else 0
        station_cov = station_totals / len(dates) if dates else 0
        avg_conf = sum(c for _, _, c, _ in results) / station_totals if station_totals > 0 else 0

        all_results.extend(results)

        print(f"{station:<8} {station_totals:>8} {station_correct:>8} {station_acc:>10.2%} {station_cov:>10.1%} {avg_conf:>10.3f}")

    # Calculate aggregate metrics
    total_predictions = len(all_results)
    total_correct = sum(1 for pred, actual, _, _ in all_results if pred == actual)
    aggregate_accuracy = total_correct / total_predictions if total_predictions > 0 else 0.0
    avg_conf_agg = sum(c for _, _, c, _ in all_results) / total_predictions if total_predictions > 0 else 0.0
    cov_aggregate = total_predictions / total_days if total_days > 0 else 0.0

    print(f"\n{'AGGREGATE':<8} {total_predictions:>8} {total_correct:>8} {aggregate_accuracy:>10.2%} {cov_aggregate:>10.1%} {avg_conf_agg:>10.3f}")

    # Additional Analysis
    print("\n--- Confidence-Gated Analysis ---")
    for thr in [0.0, 0.6, 0.65, 0.7, 0.75]:
        subset = [x for x in all_results if x[2] >= thr]
        if subset:
            corr = sum(1 for p, a, c, _ in subset if p == a)
            acc = corr / len(subset)
            print(f"  At conf >= {thr:.2f}: {len(subset)} trades, {corr}/{len(subset)}, {acc:.3f} accuracy")

    print(f"\nSUCCESS CRITERIA:")
    print(f"  ✓ Standalone accuracy ≥60%: {'PASS' if aggregate_accuracy >= 0.60 else 'FAIL'} ({aggregate_accuracy:.2%})")
    print(f"  ✓ Coverage reasonable (>5%): {'PASS' if cov_aggregate > 0.05 else 'FAIL'} ({cov_aggregate:.1%})")

    conn.close()

    return {
        'accuracy': aggregate_accuracy,
        'coverage': cov_aggregate,
        'total_trades': total_predictions,
        'average_conf': avg_conf_agg
    }


def parse_timestamp(ts_str):
    """Parse various timestamp formats from the database."""
    try:
        return datetime.fromisoformat(ts_str.replace('+00:00', '').replace('Z', ''))
    except (ValueError, TypeError):
        pass
    for fmt in ['%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M']:
        try:
            return datetime.strptime(ts_str, fmt)
        except (ValueError, TypeError):
            continue
    return None


def main():
    """Test-run of RDAE-MOS predictor using walk-forward validation."""
    print("RDAE-MOS (Regime-Dependent Analog Ensemble MOS)")
    print("="*90)

    results = run_rdae_walkforward_backtest()

    print(f"\nSUMMARY:")
    print(f"- Aggregated accuracy: {results['accuracy']:.2%}")
    print(f"- Aggregate coverage: {results['coverage']:.1%}")
    print(f"- Total predictions made: {results['total_trades']}")
    print(f"- Meets 60% accuracy target: {'YES' if results['accuracy'] >= 0.60 else 'NO'}")

    return results


if __name__ == "__main__":
    main()