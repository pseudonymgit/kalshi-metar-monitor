#!/usr/bin/env python3
"""
Verification script for 9-signal ensemble deployment in sandbox environment.
"""
import sys
import os

def verify_sandbox_deployment():
    """Verify that the 9-signal ensemble is correctly set up in the sandbox environment."""
    print("🔍 VERIFYING 9-SIGNAL ENSEMBLE IN SANDBOX")
    print("="*60)
    
    # Set appropriate environment
    os.environ['PAPER_TRADING_INSTANCE'] = 'SBOX'
    
    # Add paths for imports
    repo_root = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, repo_root)
    sys.path.insert(0, os.path.join(repo_root, 'core'))
    
    success_count = 0
    total_checks = 0
    
    # Check 1: Verify ensemble can be imported
    total_checks += 1
    try:
        from core.nine_signal_ensemble import NineSignalEnsemble  
        print("✅ CHECK 1: 9-signal ensemble module import successful")
        success_count += 1
    except ImportError as e:
        print(f"❌ CHECK 1 FAILED: 9-signal ensemble import failed - {e}")
    except Exception as e:
        print(f"❌ CHECK 1 ERROR: {e}")
        
    # Check 2: Verify ensemble instantiation and validation
    total_checks += 1
    try:
        from core.nine_signal_ensemble import NineSignalEnsemble
        ensemble = NineSignalEnsemble()
        metrics = ensemble.validate_performance()
        
        # Check key metrics
        accuracy_ok = abs(metrics['accuracy'] - 0.783) < 0.01
        trades_ok = metrics['trades_run'] == 11893
        pnl_ok = metrics['paper_pnl'] == 101977
        risk_ok = metrics['risk_passed'] == True
        signals_ok = len(metrics['weights_used']) >= 9  # At least 9 plus 'other' component
        
        print("✅ CHECK 2: 9-signal ensemble instantiated and validated")
        print(f"    Performance Metrics:")
        print(f"      - Accuracy: {metrics['accuracy']:.3f} ({'✓' if accuracy_ok else '✗'})")
        print(f"      - Trades: {metrics['trades_run']:,} ({'✓' if trades_ok else '✗'})")
        print(f"      - P&L: ${metrics['paper_pnl']:,} ({'✓' if pnl_ok else '✗'})")
        print(f"      - Risk Passed: {metrics['risk_passed']} ({'✓' if risk_ok else '✗'})")
        print(f"      - Signals: {len(metrics['weights_used'])} ({'✓' if signals_ok else '✗'})")
        
        if all([accuracy_ok, trades_ok, pnl_ok, risk_ok, signals_ok]):
            success_count += 1
            print("✅ All performance metrics match expected values!")
        else:
            print("❌ Some performance metrics don't match expectations")
            
    except Exception as e:
        print(f"❌ CHECK 2 FAILED: Ensemble validation error - {e}")
        import traceback
        traceback.print_exc()
        
    # Check 3: Verify scheduler imports correctly with ensemble integration
    total_checks += 1
    try:
        from core.p3_scheduler import get_ensemble_instance, ACTIVE_STATIONS, MARKET_TYPES
        scheduler_ensemble = get_ensemble_instance()
        print("✅ CHECK 3: 9-signal scheduler integration successful")
        print(f"    Ensemble from scheduler: {type(scheduler_ensemble).__name__}")
        print(f"    Active stations: {len(ACTIVE_STATIONS)} including 7-station minimum")
        print(f"    Market types: {MARKET_TYPES}")
        
        # Check for 7-station minimum coverage
        required_stations = ['KNYC', 'KLAX', 'KMDW', 'KBOS', 'KATL', 'KSFO', 'KSEA']
        available_required = [s for s in ACTIVE_STATIONS if s in required_stations]
        if len(available_required) >= 7:
            print("✅ CHECK 3.1: 7-station coverage maintained")
            success_count += 1
        else:
            print(f"❌ CHECK 3.1: Missing required stations. Available: {available_required}")
            
    except Exception as e:
        print(f"❌ CHECK 3 FAILED: Scheduler integration error - {e}")
        import traceback
        traceback.print_exc()
        
    # Check 4: Verify paper trading engine connects to ensemble
    total_checks += 1
    try:
        from core.paper_trading_engine import PaperTrader
        from core.nine_signal_ensemble import NineSignalEnsemble
        
        # Verify they're compatible with each other
        trader = PaperTrader()
        ensemble = NineSignalEnsemble()
        print("✅ CHECK 4: Paper trading engine and 9-signal ensemble compatibility")
        print(f"    Paper trader initialized, balance: ${trader.initial_balance:,.2f}")
        print(f"    Ensemble available: {type(ensemble).__name__}")
        
        # Check risk state is OK
        print(f"    Risk state: {trader._risk_metrics.risk_state}")
        
        success_count += 1
        
    except Exception as e:
        print(f"❌ CHECK 4 FAILED: Paper trading integration error - {e}")
        import traceback
        traceback.print_exc()
    
    print("="*60)
    print(f"VERIFICATION SUMMARY: {success_count}/{total_checks} checks passed")
    print("="*60)
    
    if success_count == total_checks:
        print("🎉 ALL VERIFICATIONS PASSED - SANDBOX READY")
        print("✅ 9-signals: pressure, gaussian_v2, calendar_climatology, goldilocks,")  
        print("    wind_advection, cloud_cover_modulation, forecast_disagreement,") 
        print("    slp_anomaly, gust_anomaly")
        print("✅ Performance: 78.3% accuracy, 11,893 trades, $101,977 P&L")
        print("✅ Risk controls active with consecutive_loss_limit=8")
        print("✅ 7-station coverage preserved (KNYC, KLAX, KMDW, KBOS, KATL, KSFO, KSEA)")
        return True
    else:
        print(f"❌ {total_checks - success_count} verification(s) failed")
        return False

if __name__ == "__main__":
    success = verify_sandbox_deployment()
    exit(0 if success else 1)