"""
Spatial Coherence Gate for Weather Engine
Groups stations into NOAA climate regions and applies confidence modulation
based on regional consensus to enhance signal quality.
"""

from typing import List, Tuple, Dict, Set
import logging

# Station to region mapping based on NOAA climate divisions
STATION_REGIONS = {
    "Northeast": ["KBOS", "KNYC", "KPHL", "KDCA"],
    "Southeast": ["KATL", "KMIA", "KMSY", "KHOU"],
    "Midwest": ["KORD", "KMDW", "KMSP", "KOKC"],
    "South Central": ["KDFW", "KAUS", "KSAT"],
    "West": ["KDEN", "KPHX", "KLAS"],
    "Pacific Coast": ["KLAX", "KSFO", "KSEA"],
}

# Reverse lookup: from station to region
STATION_TO_REGION = {}
for region, stations in STATION_REGIONS.items():
    for station in stations:
        STATION_TO_REGION[station] = region


def get_region_for_station(station: str) -> str:
    """Get the climate region for a given station."""
    return STATION_TO_REGION.get(station)


def apply_spatial_coherence_gate(
    signals: List[Tuple[str, str, str, str]], 
    date: str, 
    metar_db
) -> List[Tuple[str, str, str, str, float]]:
    """
    Apply spatial coherence filtering to enhance signal confidence based on regional consensus.
    
    Args:
        signals: List of (station, market_type, direction, reason) tuples
        date: Trading date
        metar_db: METAR database instance
    
    Returns:
        Updated list of signals with confidence scores applied (station, market_type, direction, reason, confidence)
    """
    # Convert existing signals to include confidence (default 0.5)
    signals_with_confidence = []
    for signal in signals:
        if len(signal) >= 5:
            # Signal already has confidence, use it
            station, market_type, direction, reason, confidence = signal
            signals_with_confidence.append((station, market_type, direction, reason, confidence))
        else:
            # Add default confidence of 0.5
            station, market_type, direction, reason = signal
            signals_with_confidence.append((station, market_type, direction, reason, 0.5))
    
    # Group signals by region
    region_signals = {}
    for signal in signals_with_confidence:
        station, market_type, direction, reason, confidence = signal
        region = get_region_for_station(station)
        
        if region:
            if region not in region_signals:
                region_signals[region] = []
            region_signals[region].append(signal)
    
    # Apply spatial coherence adjustment
    adjusted_signals = []
    for signal in signals_with_confidence:
        station, market_type, direction, reason, original_confidence = signal
        region = get_region_for_station(station)
        
        if not region:
            # Station not in any defined region, keep original confidence
            adjusted_signals.append((station, market_type, direction, reason, original_confidence))
            continue
        
        # Calculate regional consensus
        region_signal_list = region_signals.get(region, [])
        
        # Separate current station from others in region
        other_signals_in_region = [
            s for s in region_signal_list 
            if s[0] != station
        ]
        
        if not other_signals_in_region:
            # Only this station in region firing, no consensus possible
            adjusted_signals.append((station, market_type, direction, reason, original_confidence))
            continue
        
        # Calculate consensus direction among other stations in the same region
        same_direction_count = sum(1 for s in other_signals_in_region if s[2] == direction)
        opposite_direction_count = len(other_signals_in_region) - same_direction_count
        
        consensus_exists = len(other_signals_in_region) > 0
        if consensus_exists:
            consensus_direction = direction if same_direction_count > opposite_direction_count else (
                "DOWN" if same_direction_count < opposite_direction_count else None
            )
            
            # Check if current station agrees with regional consensus
            if consensus_direction and consensus_direction == direction:
                # Station agrees with regional consensus, BOOST confidence
                new_confidence = min(original_confidence * 1.15, 0.95)
            elif consensus_direction and consensus_direction != direction:
                # Station disagrees with regional consensus, REDUCE confidence
                new_confidence = max(original_confidence * 0.60, 0.10)
            else:
                # Split vote, no strong consensus
                new_confidence = original_confidence
        else:
            # No consensus available
            new_confidence = original_confidence
        
        adjusted_signals.append((station, market_type, direction, reason, new_confidence))
    
    # Check for regional super-consensus - if ALL stations in a region agree, promote to sure thing
    return apply_regional_super_consensus(adjusted_signals)


def apply_regional_super_consensus(signals: List[Tuple[str, str, str, str, float]]) -> List[Tuple[str, str, str, str, float]]:
    """
    If all stations in a region agree on a direction, and confidence is > 0.70, promote to sure thing lane.
    This identifies 'sure thing' opportunities where entire regions are aligned.
    """
    # Group signals by region with their directions
    region_to_directions = {}
    
    for signal in signals:
        station, market_type, direction, reason, confidence = signal
        region = get_region_for_station(station)
        
        if region:
            if region not in region_to_directions:
                region_to_directions[region] = []
            region_to_directions[region].append((direction, confidence))
    
    # Identify regions where all signals agree directionally
    updated_signals = []
    for signal in signals:
        station, market_type, direction, reason, original_confidence = signal
        region = get_region_for_station(station)
        
        if region and region in region_to_directions:
            # Check if all signals in this region agree directionally
            region_signals = region_to_directions[region]
            
            # Get the actual stations that have signals in this region
            region_stations_with_signals = [s[0] for s in signals if get_region_for_station(s[0]) == region]
            actual_region_stations = STATION_REGIONS[region]
            
            # Check if all stations from this region have signals AND they all agree
            all_have_signals = set(actual_region_stations).issubset(set(region_stations_with_signals))
            all_agree_direction = len(set(s[0] for s in region_signals)) == 1  # All directions are the same
            
            current_signal_belongs_to_complete_agreement = any(s[0] == station for s in signals)
            
            if all_have_signals and all_agree_direction and current_signal_belongs_to_complete_agreement:
                # Apply sure thing boost if confidence is already high enough
                if original_confidence > 0.70:
                    new_confidence = min(original_confidence * 1.10, 0.98)  # Small additional boost
                    updated_signals.append((station, f"{market_type}_SURE_THING", direction, f"{reason} | REGIONAL_SUPER_CONSENSUS", new_confidence))
                else:
                    updated_signals.append(signal)  # Don't boost low confidence even with super consesus
            else:
                updated_signals.append(signal)
        else:
            updated_signals.append(signal)
    
    return updated_signals