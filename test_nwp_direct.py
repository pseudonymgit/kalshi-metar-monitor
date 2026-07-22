#!/usr/bin/env python3
"""
Test script to validate the NWP Direct Signal implementation
"""
import sys
import os

# Add the project root to the path so we can import core modules
sys.path.insert(0, os.path.abspath('.'))

from core.signals.nwp_direct_signal import NwpDirectSignal

def main():
    # Initialize signal with a test database path
    # Using the standard development path for the weather engine
    db_path = 'data/nwp_forecasts.db'  # assuming this is the default location
    print("Testing NWP Direct Signal initialization...")
    
    # Test basic instantiation
    try:
        signal = NwpDirectSignal(db_path=db_path)
        print(f"✓ Signal instantiated successfully")
        print(f"  Name: {signal.name}")
        print(f"  Min lookback: {signal.min_lookback}")
    except Exception as e:
        print(f"✗ Failed to instantiate signal: {e}")
        return False
    
    # Test that evaluate_for_station can be called without immediate error
    # (Note: This may fail due to lack of db connectivity for now)
    try:
        # Test the evaluate method
        result, conf = signal.evaluate(0, [])
        print(f"✓ evaluate() method callable: returned ({result}, {conf})")
    except Exception as e:
        print(f"✗ evaluate() method failed: {e}")
        return False
        
    print("\nAll basic functionality tests passed for NWP Direct Signal!")
    return True

if __name__ == "__main__":
    success = main()
    if not success:
        sys.exit(1)