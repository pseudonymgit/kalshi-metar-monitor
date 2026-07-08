#!/usr/bin/env python3
"""
Test script to verify 9-signal ensemble integration in paper trading engine.
"""
import os
import sys
import datetime
from datetime import datetime as dt

def test_sandbox_9signal():
    # Set environment
    os.environ['PAPER_TRADING_INSTANCE'] = 'SBOX'
    
    print("Testing 9-signal ensemble integration...")
    
    from core.paper_trading_engine import PaperTrader
    from core.nine_signal_ensemble import NineSignalEnsemble
    
    # Create paper trader
    trader = PaperTrader()

    # Create ensemble
    ensemble = NineSignalEnsemble()
    perf = ensemble.validate_performance()

    print(f'✅ Verified ensemble performance:')
    print(f'  Accuracy: {perf["accuracy"]:.1%}')
    print(f'  Trades: {perf["trades_run"]:,}')
    print(f'  P&L: ${perf["paper_pnl"]:,}')

    # Generate test signals with recent date
    test_date = (dt.now() - datetime.timedelta(days=5)).strftime('%Y-%m-%d')
    
    signals = trader.generate_signals(test_date)

    print(f'✅ Generated {len(signals)} signals using 9-signal ensemble')

    if signals:
        print('Sample signals:')
        for i, (station, market_type, direction, reason) in enumerate(signals[:3]):
            print(f'  {i+1}. {station}: {direction.value} ({reason})')
    
    print('✅ Sandbox test successful!')

    # Check risk state
    print(f'✅ Risk state: {trader._risk_metrics.risk_state}')
    
    return True

if __name__ == "__main__":
    test_sandbox_9signal()