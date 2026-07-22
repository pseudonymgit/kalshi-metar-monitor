"""Signal Pipeline ModuleSignal generation, analysis, and probability estimation functions.Extracted from paper_trading_engine.py during Phase 20.1 monolith decomposition."""import loggingimport osimport statisticsfrom datetime import datetime, timedelta, timezonefrom typing import Dict, List, Optional, Tuple, Anyfrom pathlib import Pathfrom .sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connectionfrom .agreement_gate import AgreementGate, SimpleAgreementCheckerfrom .adaptive_thresholds import filter_signals_by_adaptive_threshold, get_adaptive_thresholdfrom .spatial_coherence import apply_spatial_coherence_gate, STATION_REGIONS as SPATIAL_REGIONS, STATION_TO_REGIONfrom .ensemble_diversity import compute_diversity_score, apply_diversity_penaltyfrom .station_skill_gate import StationSkillGatefrom .station_time import is_within_entry_window as _is_within_entry_windowfrom .station_registry import (    get_all_stations as _get_all_stations,    get_station_mapping as _get_station_mapping,    validate_station_registry as _validate_station_registry,    get_cluster_for_station as _get_cluster_for_station,)_LOGGER = logging.getLogger(__name__)def _get_daily_metars(self, target_date):
    """Get daily METAR data for a target date from metar DB."""
    conn = get_sqlite_connection(self.metar_db)
    c = conn.cursor()

    c.execute("""
        SELECT DISTINCT(station) FROM settlement_epochs
        WHERE local_trading_date = ?
        AND epoch_status = 'closed'
        ORDER BY station
    """, (target_date,))

    stations = [row[0] for row in c.fetchall()]
    conn.close()

    return stations
def _get_settlement_data(self, station, trading_date):
    """
    Get settlement data for a given station and trading date.
    Returns settlement price (0.0-1.0) or None if not available
    """
    conn = get_sqlite_connection(self.metar_db)
    c = conn.cursor()

    c.execute("""
        SELECT settlement_bucket FROM settlement_epochs
        WHERE station = ? AND local_trading_date = ? AND epoch_status = 'closed'
    """, (station, trading_date))

    row = c.fetchone()
    conn.close()

    return row[0] / 100.0 if row and row[0] is not None else None
def _get_analytical_probability(self, station, date, signal_direction):
    """
    DETERMINISTIC: Calculate analytical fair value based on historical data.

    This is the 'brain' of the deterministic trading - no ML/AI involved.
    Uses historical frequency, climatology, and recent patterns.

    Includes integration with climatology_pillar for enhanced analytics.

    Returns tuple: (probability estimate 0-1, confidence indicator, additional_metadata)
    """
    # Use historical data to calculate conditional probability
    # P(signal_direction | current conditions) based on historical frequency
    conn = get_sqlite_connection(self.metar_db)
    c = conn.cursor()

    # Get climatology - probability of temperature moving UP/DOWN on same date
    # based on historical observations from same calendar day
    target_month_day = date[5:10]  # Extract MM-DD from YYYY-MM-DD

    c.execute("""
        SELECT
            avg(CASE WHEN settlement_bucket > prior_settlement_bucket THEN 1.0 ELSE 0.0 END) as up_rate,
            count(*) as sample_size
        FROM settlement_epochs
        WHERE station = ?
        AND substr(local_trading_date, 6, 5) = ?
        AND epoch_status = 'closed'
        AND settlement_bucket IS NOT NULL AND prior_settlement_bucket IS NOT NULL
    """, (station, target_month_day))

    climatology_row = c.fetchone()
    climatology_prob = climatology_row[0] if climatology_row and climatology_row[0] is not None else 0.5
    climatology_sample_size = climatology_row[1] if climatology_row else 0

    # Get recent trend (last 7 days)
    current_month_day = date[5:10]
    c.execute("""
        SELECT
            AVG(CASE WHEN settlement_bucket > prior_settlement_bucket THEN 1.0 ELSE 0.0 END) as trend_prob,
            COUNT(*) as trend_samples
        FROM settlement_epochs
        WHERE station = ?
        AND local_trading_date BETWEEN date(?, '-7 days') AND ?
        AND epoch_status = 'closed'
        AND settlement_bucket IS NOT NULL AND prior_settlement_bucket IS NOT NULL
    """, (station, date, date))

    trend_row = c.fetchone()
    trend_prob = trend_row[0] if trend_row and trend_row[0] is not None else climatology_prob
    trend_samples = trend_row[1] if trend_row else 0

    # Get rolling window of last 30 days for station stability.
    # SQLite has no built-in STDDEV, so fetch the flags and compute volatility in Python.
    c.execute("""
        SELECT CASE WHEN settlement_bucket > prior_settlement_bucket THEN 1.0 ELSE 0.0 END as up_flag
        FROM settlement_epochs
        WHERE station = ?
        AND local_trading_date BETWEEN date(?, '-30 days') AND ?
        AND epoch_status = 'closed'
        AND settlement_bucket IS NOT NULL AND prior_settlement_bucket IS NOT NULL
    """, (station, date, date))

    rolling_flags = [row[0] for row in c.fetchall() if row[0] is not None]
    rolling_prob = sum(rolling_flags) / len(rolling_flags) if rolling_flags else climatology_prob
    volatility = statistics.stdev(rolling_flags) if len(rolling_flags) > 1 else 0.2  # Default

    conn.close()

    # Combine all factors with weighted average
    total_weight = climatology_sample_size + trend_samples + 5  # 5 is arbitrary for rolling baseline
    combined_prob = (
        climatology_prob * climatology_sample_size +
        trend_prob * trend_samples +
        rolling_prob * 5
    ) / total_weight if total_weight > 0 else 0.5

    # Adjust based on signal direction
    if signal_direction == MarketSide.UP:
        prob = combined_prob
    elif signal_direction == MarketSide.DOWN:
        prob = 1.0 - combined_prob
    else:
        prob = 0.5

    # Confidence calculation based on sample sizes and stability
    confidence = min(0.95, max(0.3, 0.3 + min(0.65, (
        climatology_sample_size * 0.1 +  # More data = more confidence
        trend_samples * 0.2 +           # Recent data heavily weighted
        (1 - volatility) * 0.3          # Less volatility = more confidence
    ) / 10.0)))  # Normalize to reasonable range

    return prob, confidence, {
        'climat_prob': climatology_prob,
        'trend_prob': trend_prob,
        'rolling_prob': rolling_prob,
        'climat_samples': climatology_sample_size,
        'trend_samples': trend_samples,
        'volatility': volatility
    }
def _get_market_price(self, station, date, market_type='HIGH'):
    """
    Get market price from live Kalshi API. Falls back to historical
    heuristic if API is unavailable.
    """
    # Try live Kalshi API first
    try:
        price, meta = _get_live_market_price(station, market_type, date)
        if price is not None and 0.01 <= price <= 0.99:
            # Store metadata for logging
            self._last_market_price_meta = meta
            return price
        # If price is at the boundary (0 or 1), market may be settled
        if price is not None and meta.get('fallback'):
            _LOGGER.info(
                "market_price_fallback station=%s reason=%s",
                station, meta.get('source')
            )
    except Exception as e:
        _LOGGER.warning(
            "market_price_live_failed station=%s error=%s", station, e
        )

    # Fallback: historical heuristic (for offline/backtest use)
    conn = get_sqlite_connection(self.metar_db)
    c = conn.cursor()

    c.execute("""
        SELECT date(min(local_trading_date)), AVG(prior_settlement_bucket),
               AVG((settlement_bucket - prior_settlement_bucket) / 100.0) as avg_move
        FROM settlement_epochs
        WHERE station = ? AND market_type = ?
        AND local_trading_date BETWEEN date(?, '-14 days') AND ?
        AND epoch_status = 'closed'
    """, (station, market_type, date, date))

    row = c.fetchone()
    conn.close()

    if row and row[1] is not None:
        base_price = min(0.95, max(0.05, ((row[1] or 70.0) - 50) / 40.0))
        avg_move = row[2] or 0.05
        adjusted_price = base_price + avg_move
        return min(0.95, max(0.05, adjusted_price))
    else:
        return 0.5
def record_explicit_decision_output(self, station, date, market_type, signal_direction,
                                  market_price, analytical_prob, confidence,
                                  reasons=None, trade_version="v2.0", notes=""):
    """
    Record explicit decision output: market implied probability vs analytical fair value + confidence.
    This addresses P1.4 requirement: "Explicit decision output: 'market implied probability vs analytical fair value + confidence'"
    """
    conn = get_sqlite_connection(self.paper_db)
    c = conn.cursor()

    # For HIGH type market:
    # - signal_direction: which way (UP/DOWN) we think it will move
    # - market_price: market's belief of UP probability
    # - analytical_prob: our calculated probability of UP direction
    market_implied_prob = market_price  # Market price is typically interpreted as implied probability
    # The analytical fair_value depends on how we match market direction expectation
    # Convert analytical_prob to match direction of interest
    if signal_direction == MarketSide.UP:
        analytical_fair_value = analytical_prob
    else:  # DOWN
        analytical_fair_value = 1.0 - analytical_prob

    # Calculate difference and determine recommendation
    price_diff = analytical_fair_value - market_implied_prob

    # Recommendation based on analysis: buy cheap asset / sell expensive one
    if abs(price_diff) > 0.05:  # 5% threshold for action
        if signal_direction == MarketSide.UP:
            if analytical_fair_value > market_implied_prob:
                recommendation = "BUY_UP"  # Buy YES contract when we think UP prob is higher
            else:
                recommendation = "SELL_UP"  # Sell YES contract when we think UP prob is lower
        else:  # signal_direction is DOWN
            if analytical_fair_value > 1.0 - market_implied_prob:
                recommendation = "BUY_DOWN"  # Buy NO contract when we think DOWN prob is higher
            else:
                recommendation = "SELL_DOWN"
    else:
        recommendation = "HOLD"

    # Check for meaningful divergence
    divergence = abs(price_diff) > 0.10  # If 10%+ divergence detected

    c.execute("""
        INSERT INTO decision_output_log
        (decision_date_utc, station, market_type, forecast_direction, market_implied_prob,
         analytical_fair_value, confidence_level, price_difference, recommendation,
         reasons, divergence_detected, trade_version, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        date, station, market_type, signal_direction.value, market_implied_prob,
        analytical_fair_value, confidence, price_diff, recommendation,
        reasons or "", divergence, trade_version, notes or ""
    ))

    conn.commit()
    conn.close()
def generate_signals(self, date):
    """
    DETERMINISTIC: Generate daily trade signals based on fixed algorithms.
    No ML/AI/LLM involvement.

    Signals include the hourly late_day_momentum signal (threshold=1.7)
    as a first-class participant alongside the existing deterministic signals.

    Returns list of (station, market_type, signal_direction, reason) tuples
    """
    signals = []

    # Determine if we have settlement data for this date
    available_stations = self._get_daily_metars(date)

    if not available_stations:
        print(f"INFO: No settlement data available for {date}, checking for live trading signals instead...")
        available_stations = [s[0] for s in
            [('KATL', 'Atlanta'), ('KBOS', 'Boston'), ('KLAX', 'Los Angeles'),
             ('KJFK', 'New York'), ('KORD', 'Chicago'), ('KMIA', 'Miami'),
             ('KSEA', 'Seattle'), ('KSFO', 'San Francisco'), ('KHOU', 'Houston'),
             ('KPHX', 'Phoenix'), ('KDEN', 'Denver'), ('KATL', 'Atlanta')]]
        available_stations = list(set(available_stations[:6]))  # Use first 6

    # Open METAR DB connection for hourly late-day momentum signal
    metar_conn = sqlite3.connect(self.metar_db, timeout=10)

    for station in available_stations:
        # NOTE: reversion signal (Signal 1) REMOVED from ensemble per Phase 1 fix.
        # Reversion proved unreliable - it used stale fill_price for P&L and had
        # negative directional accuracy on out-of-sample data.

        # Signal 2: Calendar day pattern (climatology-based)
        climatology_direction = self._get_calendar_climatology_direction(station, date)
        if climatology_direction is not None and abs(climatology_direction) > 1.5:  # Meaningful trend over 1.5 points
            if climatology_direction > 0:
                signals.append((station, "HIGH", MarketSide.UP, "calendar_trend_up"))
            else:
                signals.append((station, "HIGH", MarketSide.DOWN, "calendar_trend_down"))

        # Signal 3: Hourly late-day momentum (first-class signal, threshold=1.7)
        ldm_direction, ldm_conf, ldm_prob = _ldm_hourly_signal(station, date, metar_conn)
        if ldm_direction is not None:
            market_side = MarketSide.UP if ldm_direction == "up" else MarketSide.DOWN
            signals.append((station, "HIGH", market_side, "late_day_momentum_hourly"))

        # Signal 4: NWP Direct (uses real NWP forecast model data from DB)
        if NWP_DIRECT_ENABLED:
            # Lazy-initialize the NWP direct signal on first use
            if self._nwp_direct is None:
                try:
                    self._nwp_direct = NwpDirectSignal()
                    _LOGGER.info("NWP direct signal initialized (lazy load)")
                except Exception as e:
                    _LOGGER.warning(f"Failed to initialize NWP Direct Signal: {e}")
            if self._nwp_direct is not None:
                try:
                    result = self._nwp_direct.compute_signal(station, date)
                    if result and result.get('direction') is not None and result.get('confidence', 0) > 0:
                        direction = result['direction']
                        market_side = MarketSide.UP if direction == 1 else MarketSide.DOWN
                        signals.append((station, "HIGH", market_side, "nwp_direct"))
                        _LOGGER.info(f"NWP direct signal fired for {station} on {date}: direction={direction}, confidence={result.get('confidence', 0)}")
                except Exception as e:
                    _LOGGER.warning(f"Failed to compute NWP direct signal for {station} on {date}: {e}")

        # Signal 5: Multi-Model Ensemble (using real NWP forecasts from database)
        if HAS_MULTI_MODEL_ENSEMBLE:
            try:
                # Need to get previous day high temperature for comparison
                prev_day_high = self._get_prev_day_high_temperature(station, date)
                direction, confidence = multi_model_ensemble_signal(station, date, prev_day_high)
                if direction is not None and confidence >= 0.7:  # Use same confidence threshold as other signals
                    market_side = MarketSide.UP if direction == 'up' else MarketSide.DOWN
                    signals.append((station, "HIGH", market_side, "multi_model_ensemble"))
                    _LOGGER.info(f"Multi-Model Ensemble signal fired for {station} on {date}: direction={direction}, confidence={confidence}")
            except Exception as e:
                _LOGGER.warning(f"Failed to compute Multi-Model Ensemble signal for {station} on {date}: {e}")

        # Signal 6: 850-mb Temperature Advection (gated behind TEMPERATURE_ADVECTION_ENABLED)
        if TEMPERATURE_ADVECTION_ENABLED and TemperatureAdvectionSignal is not None:
            if self._temp_advection is None:
                try:
                    self._temp_advection = TemperatureAdvectionSignal()
                    _LOGGER.info("Temperature Advection signal initialized (lazy load)")
                except Exception as e:
                    _LOGGER.warning(f"Failed to initialize Temperature Advection Signal: {e}")
            if self._temp_advection is not None:
                try:
                    direction, confidence = self._temp_advection.evaluate_for_station(station, date)
                    if direction is not None and confidence >= 0.25:
                        market_side = MarketSide.UP if direction == 'up' else MarketSide.DOWN
                        signals.append((station, "HIGH", market_side, "temperature_advection"))
                        _LOGGER.info(f"Temperature Advection signal fired for {station} on {date}: direction={direction}, confidence={confidence:.3f}")
                except Exception as e:
                    _LOGGER.warning(f"Failed to compute Temperature Advection signal for {station} on {date}: {e}")

        # Signal 7: Goldilocks (spike-reversion pattern) - added to signals like others
        if HAS_GOLDILOCKS_SIGNAL and self._goldilocks_signal is not None:
            try:
                direction, confidence = self._goldilocks_signal.evaluate_for_station(station, date, metar_conn)
                if direction is not None and confidence >= 0.25:
                    market_side = MarketSide.UP if direction == 'up' else MarketSide.DOWN
                    signals.append((station, "HIGH", market_side, "goldilocks_spike_reversion"))
                    _LOGGER.info(f"Goldilocks signal fired for {station} on {date}: direction={direction}, confidence={confidence:.3f}")
            except Exception as e:
                _LOGGER.warning(f"Failed to compute Goldilocks signal for {station} on {date}: {e}")

    metar_conn.close()

    # Filter signals based on station skill (T5 - per station skill gating)
    if self._skill_gate is not None:
        before_count = len(signals)
        filtered_signals = []  
        for station, market_type, signal_direction, reason in signals:
            if self._skill_gate.is_station_skilled(station, market_type):
                filtered_signals.append((station, market_type, signal_direction, reason))
            else:
                _LOGGER.debug(f"Skipped unskilled station trade: {station} for {market_type} market")
        signals = filtered_signals
        _LOGGER.info(f"Applied skill gate filter: {len(signals)} skilled out of {before_count} total signals")

    # Separate goldilocks signals if separate lane is enabled
    goldilocks_signals = []
    other_signals = []
    
    if GOLDILOCKS_SEPARATE_LANE:
        for station, market_type, direction, reason in signals:
            if reason == "goldilocks_spike_reversion":
                goldilocks_signals.append((station, market_type, direction, reason))
            else:
                other_signals.append((station, market_type, direction, reason))
        signals = other_signals
        _LOGGER.info(f"Separated Goldilocks signals for independent processing. Other signals: {len(other_signals)}, Goldilocks: {len(goldilocks_signals)}")
    else:
        # All signals remain together when separate lane is disabled
        goldilocks_signals = []
        other_signals = signals
        
    # Apply adaptive confidence thresholds BEFORE the agreement gate filtering (Phase 4.4)
    if other_signals and self._trade_journal is not None:
        # Convert signal tuples in other_signals to dictionary format for adaptive threshold filtering
        # Format from generate_signals is (station, market_type, direction, reason)
        # We need to convert to {'type': reason, 'station': station, 'confidence': DEFAULT_CONF, ...}
        # We'll assign a default confidence here before applying adaptive thresholds
        signal_dicts = []
        for station, market_type, direction, reason in other_signals:
            # For the initial confidence, use a reasonable default or assign based on signal type
            # This is important because adaptive thresholds compare confidence to threshold
            base_confidence = 0.5  # Default conservative value
            
            # Different signal types may have different baseline confidences
            if reason in ["calendar_trend_up", "calendar_trend_down"]:
                base_confidence = 0.6 
            elif reason in ["late_day_momentum_hourly"]:
                base_confidence = 0.7
            elif reason in ["nwp_direct"]:
                base_confidence = 0.75
            elif reason in ["multi_model_ensemble"]:
                base_confidence = 0.75
            elif reason in ["temperature_advection"]:
                base_confidence = 0.6
            elif reason in ["goldilocks_spike_reversion"]:
                base_confidence = 0.65
            else:
                base_confidence = 0.5
            
            signal_dict = {
                'type': reason,
                'station': station,
                'market_type': market_type,
                'direction': direction,
                'confidence': base_confidence,
                'market_side': direction  # Assuming direction and market_side are the same for this context
            }
            signal_dicts.append(signal_dict)
        
        # Apply adaptive threshold filtering
        try:
            filtered_signal_dicts = filter_signals_by_adaptive_threshold(signal_dicts, self._trade_journal)
            
            # Convert back to tuple format (station, market_type, direction, reason, adaptive_conf) 
            filtered_other_signals = []
            for sig_dict in filtered_signal_dicts:
                # Extract values
                station = sig_dict['station']
                market_type = sig_dict['market_type']
                direction = sig_dict['direction']
                reason = sig_dict['type']
                # Use the computed adaptive threshold as proxy for adjusted confidence or keep original confidence
                confidence = sig_dict['confidence']  # This would remain original confidence for now
                filtered_other_signals.append((station, market_type, direction, reason, confidence))
            
            other_signals = filtered_other_signals
            _LOGGER.info(f"Applied adaptive thresholds: {len(other_signals)} signals remained after threshold filtering out of {len(signal_dicts)} prior to agreement gate")
        except Exception as e:
            _LOGGER.error(f"Error applying adaptive threshold filtering: {e}")
            # Continue without filtering if it fails
    
    # Apply agreement gate filter ONLY to non-Goldilocks signals
    if other_signals:
        before_agreement_count = len(other_signals)
        
        # Group signals by station and market type, then apply agreement check
        grouped_signals = {}
        for station, market_type, direction, reason in other_signals:
            key = (station, market_type)
            if key not in grouped_signals:
                grouped_signals[key] = []
            grouped_signals[key].append((station, market_type, direction, reason))
        
        # Check agreement for each group and collect results
        final_other_signals = []
        for key, signal_group in grouped_signals.items():
            agreed_signals = SimpleAgreementChecker.check_agreement(signal_group, n_required=int(os.getenv("AGREEMENT_THRESHOLD", "3")))
            final_other_signals.extend(agreed_signals)
        
        other_signals = final_other_signals
        _LOGGER.info(f"Applied agreement gate: {len(other_signals)} non-Goldilocks signals passed consensus out of {before_agreement_count} post-skill-gate signals")
    
    # Combine Goldilocks signals (unfiltered) with agreeing non-Goldilocks signals
    all_final_signals = other_signals + goldilocks_signals
    _LOGGER.info(f"Total final signals: {len(all_final_signals)} (non-Goldilocks passed agreement: {len(other_signals)}, Goldilocks (unfiltered): {len(goldilocks_signals)})")
    
    signals = all_final_signals

    # Apply diversity scoring to signals from Ensemble Diversity Score module (Phase 4.5)
    # This modulates confidence after agreement gate to penalize when signals are too redundant
    if ENSEMBLE_DIVERSITY_ENABLED and len(signals) > 1:
        try:
            # Collect signal votes for diversity scoring (convert to appropriate format)
            # For diversity calculation, we need to convert signals to (direction, confidence) tuples
            signal_votes = []
            for signal_tuple in signals:
                if len(signal_tuple) >= 5:
                    # Signal tuple has confidence value: (station, market_type, direction, reason, confidence)
                    _, _, direction, _, confidence = signal_tuple
                    # Convert direction to numeric: UP=1, DOWN=-1
                    numeric_direction = 1 if direction.value == "UP" else -1
                    signal_votes.append((numeric_direction, confidence))
                else:
                    # Default confidence if not available
                    _, _, direction, _ = signal_tuple
                    # Convert direction to numeric: UP=1, DOWN=-1
                    numeric_direction = 1 if direction.value == "UP" else -1
                    signal_votes.append((numeric_direction, 0.5))

            diversity_score = compute_diversity_score(signal_votes)
            _LOGGER.info(f"Ensemble diversity score: {diversity_score:.3f} for {len(signal_votes)} signals")

            # Apply diversity penalty to all signals that have confidence values
            updated_signals = []
            for signal_tuple in signals:
                if len(signal_tuple) >= 5:
                    # Has confidence component, apply diversity penalty
                    station, market_type, direction, reason, old_confidence = signal_tuple
                    new_confidence = apply_diversity_penalty(old_confidence, diversity_score)
                    updated_signals.append((station, market_type, direction, reason, new_confidence))
                else:
                    # No confidence component, append as-is
                    updated_signals.append(signal_tuple)

            signals = updated_signals
            
        except Exception as e:
            _LOGGER.error(f"Error applying ensemble diversity scoring: {e}")

    # Also look for late-day METAR momentum patterns if date is today/tomorrow
    if self.is_recent_enough_for_late_day_analysis(date):
        signals.extend(self._analyze_late_day_momentum_signals(date))

    return signals
def is_recent_enough_for_late_day_analysis(self, date_str):
    """Check if the analysis date is recent enough to have METAR data for analysis"""
    try:
        analysis_date = datetime.fromisoformat(date_str.replace('Z', '+00:00')) if 'Z' in date_str else \
                       datetime.strptime(date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        today = datetime.now(timezone.utc).date()
        target_date = analysis_date.date()
        # Check if target date is today or recent enough to have METAR data
        return (today - target_date).days <= 1   # Allow for today and yesterday
    except Exception as e:
        return False
def _analyze_late_day_momentum_signals(self, date):
    """Analyze late-day METAR patterns for same-day signals (P1.2 late-day plateau/slope)"""
    signals = []

    # This would involve checking late-day temperature patterns for plateaus/slopes
    # Simplified implementation for now
    conn = get_sqlite_connection(self.metar_db)
    c = conn.cursor()

    for station in ['KATL', 'KBOS', 'KLAX', 'KJFK', 'KORD', 'KMIA']:
        # Look for late-day temperature changes indicating potential momentum
        c.execute("""
            SELECT timestamp_utc, temp_f
            FROM metar_observations
            WHERE station = ?
            AND date_utc = ?
            AND CAST(strftime('%H', timestamp_utc) AS INTEGER) BETWEEN 17 AND 22
            AND temp_f IS NOT NULL
            ORDER BY timestamp_utc ASC
        """, (station, date))

        temp_readings = c.fetchall()
        if len(temp_readings) >= 3:
            temp_change_rate = self.calculate_temperature_trend(temp_readings)

            # If there's sustained late-day movement, suggest continuation (momentum)
            if temp_change_rate > 1.0:  # Rising rapidly
                signals.append((station, "HIGH", MarketSide.UP, "late_day_upward_momentum"))
            elif temp_change_rate < -1.0:  # Dropping rapidly
                signals.append((station, "HIGH", MarketSide.DOWN, "late_day_downward_momentum"))

    conn.close()
    return signals
def calculate_temperature_trend(self, reading_pairs):
    """Simple linear trend calculation between late-day temperature measurements"""
    if len(reading_pairs) < 2:
        return 0

    # Extract temperature values
    temps = [r[1] for r in reading_pairs if r[1] is not None]
    if len(temps) < 2:
        return 0

    # Basic rate calculation over time period
    rate = (temps[-1] - temps[0]) / len(temps) if len(temps) > 0 else 0
    return rate
def _get_prior_day_reversion(self, station, current_date):
    """Get temperature difference between yesterday's settlement and day before."""
    # This is a simplified reversion estimator. Keep the connection open
    # through both fallback queries.
    conn = get_sqlite_connection(self.metar_db)
    c = conn.cursor()
    try:
        # Get settlements for two consecutive days
        c.execute("""
            SELECT settlement_bucket, prior_settlement_bucket
            FROM settlement_epochs
            WHERE station = ?
            AND local_trading_date BETWEEN date(?, '-1 day') AND ?
            AND epoch_status = 'closed'
            ORDER BY local_trading_date DESC
        """, (station, current_date, current_date))

        rows = c.fetchall()
        if len(rows) >= 2:
            today_settled = rows[0][0]
            yesterday_settlement_prev = rows[1][0]
            if today_settled is not None and yesterday_settlement_prev is not None:
                return today_settled - yesterday_settlement_prev

        # Alternative: just get prior-to-current move for today
        c.execute("""
            SELECT settlement_bucket, prior_settlement_bucket
            FROM settlement_epochs
            WHERE station = ? AND local_trading_date = ?
            AND epoch_status = 'closed'
        """, (station, current_date))

        row = c.fetchone()
        if row and row[0] is not None and row[1] is not None:
            return row[0] - row[1]  # Current minus prior

        return 0  # Neutral if no data available
    finally:
        conn.close()
def _get_calendar_climatology_direction(self, station, date):
    """Get historical tendency for this station on this calendar day."""
    conn = get_sqlite_connection(self.metar_db)
    c = conn.cursor()

    target_month_day = date[5:10]  # MM-DD

    c.execute("""
        SELECT
            AVG(settlement_bucket - prior_settlement_bucket) as avg_change
        FROM settlement_epochs
        WHERE station = ?
        AND substr(local_trading_date, 6, 5) = ?
        AND settlement_bucket IS NOT NULL
        AND prior_settlement_bucket IS NOT NULL
        AND epoch_status = 'closed'
    """, (station, target_month_day))

    result = c.fetchone()
    conn.close()

    return result[0] if result and result[0] is not None else None
def _get_prev_day_high_temperature(self, station, current_date):
    """
    Get the previous day's high temperature for a given station and current date.
    This is needed for the multi-model ensemble signal which compares forecasts to prior actual.

    Args:
        station: ICAO station code (e.g., 'KATL')
        current_date: current date string (YYYY-MM-DD) to get yesterday's data for

    Returns: float - previous day's high temperature or None if not available
    """
    from datetime import datetime, timedelta
    current_dt = datetime.strptime(current_date, '%Y-%m-%d')
    prev_date = (current_dt - timedelta(days=1)).strftime('%Y-%m-%d')

    conn = get_sqlite_connection(self.metar_db)
    c = conn.cursor()

    try:
        c.execute("""
            SELECT MAX(temp_f) as high_temp
            FROM metar_observations
            WHERE station = ?
            AND date_utc = ?
            AND temp_f IS NOT NULL
            GROUP BY date_utc
        """, (station, prev_date))

        result = c.fetchone()
        conn.close()

        return result[0] if result and result[0] is not None else None
    except Exception as e:
        conn.close()
        print(f"Error fetching previous day high temperature: {e}")
        return None
