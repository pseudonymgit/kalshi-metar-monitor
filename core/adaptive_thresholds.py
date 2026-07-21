"""
Adaptive Confidence Thresholds Module
Adjusts signal confidence thresholds based on rolling accuracy data.
"""
import logging
from typing import List, Dict, Tuple, Optional
from .trade_journal import TradeJournal

# Configuration
ADAPTIVE_THRESHOLDS_ENABLED = True
ADAPTIVE_WINDOW_DAYS = 30
ADAPTIVE_BOOST_THRESHOLD = 0.70  # Accuracy above this → threshold decrease
ADAPTIVE_PENALTY_THRESHOLD = 0.50  # Accuracy below this → threshold increase
DEFAULT_THRESHOLD = 0.25
THRESHOLD_FLOOR = 0.15  # Never lower than this
THRESHOLD_CEILING = 0.7  # Never higher than this
THRESHOLD_ADJUSTMENT = 0.1  # Amount to adjust threshold by


class SignalData:
    """
    Holds information about a specific signal type at a given station.
    """
    def __init__(self, signal_type: str, station_code: str):
        self.signal_type = signal_type
        self.station_code = station_code
    
    def __hash__(self):
        return hash((self.signal_type, self.station_code))
    
    def __eq__(self, other):
        if not isinstance(other, SignalData):
            return False
        return (self.signal_type == other.signal_type and 
                self.station_code == other.station_code)


def get_adaptive_threshold(signal_name: str, station: str, trade_journal: TradeJournal) -> float:
    """
    Get the adaptive threshold for a specific signal at a given station.
    
    Args:
        signal_name: Name/type of the signal
        station: Station code
        trade_journal: TradeJournal instance to get accuracy data from
    
    Returns:
        float: Adaptive threshold value between THRESHOLD_FLOOR and THRESHOLD_CEILING
    """
    if not ADAPTIVE_THRESHOLDS_ENABLED:
        return DEFAULT_THRESHOLD
    
    try:
        # Get all signal accuracy data
        accuracy_data = trade_journal.get_accuracy_by_signal()
        
        # Check if the specific signal exists in the data
        if signal_name in accuracy_data:
            signal_stats = accuracy_data[signal_name]
            # Extract directional accuracy percentage and convert to decimal
            directional_acc_pct = signal_stats.get('directional_accuracy_pct', 0.0)
            accuracy = directional_acc_pct / 100.0
        else:
            # If no data for this signal type, use default threshold
            return DEFAULT_THRESHOLD
        
        # Start with default threshold
        current_threshold = DEFAULT_THRESHOLD
        
        # Adjust based on recent accuracy performance
        if accuracy > ADAPTIVE_BOOST_THRESHOLD:
            # High accuracy - lower threshold to make it easier to fire
            current_threshold = max(THRESHOLD_FLOOR, current_threshold - THRESHOLD_ADJUSTMENT)
        elif accuracy < ADAPTIVE_PENALTY_THRESHOLD:
            # Low accuracy - raise threshold to make it harder to fire
            current_threshold = min(THRESHOLD_CEILING, current_threshold + THRESHOLD_ADJUSTMENT)
        
        # Log the adjustment for monitoring purposes
        logging.info(
            f"Signal {signal_name} at {station}: "
            f"Accuracy={accuracy:.3f}, Base Threshold={DEFAULT_THRESHOLD:.3f}, "
            f"Adaptive Threshold={current_threshold:.3f}"
        )
        
        return current_threshold
        
    except Exception as e:
        logging.warning(f"Error getting adaptive threshold for {signal_name} at {station}: {str(e)}")
        # Return default threshold if there's an error
        return DEFAULT_THRESHOLD


def filter_signals_by_adaptive_threshold(signals: List[Dict], trade_journal: TradeJournal) -> List[Dict]:
    """
    Filter a list of signals based on their adaptive thresholds.
    Only signals with confidence greater than their adaptive threshold will pass.
    
    Args:
        signals: List of signal dictionaries, each containing 'type', 'station', 'confidence' etc.
        trade_journal: TradeJournal instance to get accuracy data from
    
    Returns:
        List: Filtered list with only signals that passed the adaptive threshold check
    """
    if not ADAPTIVE_THRESHOLDS_ENABLED or not signals:
        return signals.copy()
    
    filtered_signals = []
    
    for signal in signals:
        signal_type = signal.get('type', '')
        station = signal.get('station', '')
        confidence = signal.get('confidence', 0.0)
        
        if not signal_type or not station:
            # Skip signals that don't have required fields
            continue
        
        # Get adaptive threshold for this specific signal-station combination
        adaptive_threshold = get_adaptive_threshold(signal_type, station, trade_journal)
        
        # Only add to result if signal's confidence exceeds adaptive threshold
        if confidence >= adaptive_threshold:
            # Include adaptive threshold in signal for transparency
            signal_copy = signal.copy()
            signal_copy['adaptive_threshold'] = adaptive_threshold
            filtered_signals.append(signal_copy)
        
        logging.debug(
            f"Signal filter: {signal_type}@{station}, "
            f"confidence={confidence:.3f}, "
            f"adaptive_threshold={adaptive_threshold:.3f}, "
            f"status={'PASS' if confidence >= adaptive_threshold else 'REJECT'}"
        )
    
    total_count = len(signals)
    filtered_count = len(filtered_signals)
    logging.info(
        f"Adaptive threshold filtering: {total_count} signals in, "
        f"{filtered_count} signals out ({filtered_count/total_count*100:.1f}%)"
    ) if total_count > 0 else logging.info(
        "Adaptive threshold filtering: no signals to process"
    )
    
    return filtered_signals