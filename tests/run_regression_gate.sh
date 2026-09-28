#!/bin/bash

# Weather Engine Regression Gate
# Runs integration tests to ensure basic functionality remains intact
# Returns 0 if ALL tests pass, 1 if any test fails

set -e

echo "Starting Weather Engine Regression Gate..."

# Track test results
ALL_TESTS=("IMPORT" "SIGNAL_GEN" "RISK" "SKILL_GATE" "REGISTRY" "NWP" "CALIBRATION" "TARGET_INVARIANCE")
PASSED_TESTS=()
FAILED_TESTS=()
SKIPPED_TESTS=()  # R/T (2026-09-28): skips reported separately, never as passes

# Test 0 (GS-B3): Target-invariance gate — BLOCKING. No signal may read the
# target day's observed values (the settlement answer). Any leak fails the gate.
PYTHON="${PYTHON:-.venv-gefs/bin/python}"
if "$PYTHON" tests/test_target_invariance.py >/dev/null 2>&1; then
    PASSED_TESTS+=("TARGET_INVARIANCE")
else
    echo "TARGET_INVARIANCE: FAIL (a signal reads the target day — look-ahead leak)"
    "$PYTHON" tests/test_target_invariance.py 2>&1 | grep -E "LEAK|-" | head
    FAILED_TESTS+=("TARGET_INVARIANCE")
fi

# Test 1: Import check
if "$PYTHON" -c "from core.paper_trading_engine import PaperTrader; t = PaperTrader(); print('IMPORT: PASS')" 2>/dev/null; then
    PASSED_TESTS+=("IMPORT")
else
    echo "IMPORT: FAIL"
    FAILED_TESTS+=("IMPORT")
fi

# Test 2: Signal generation test
# P4-A (2026-09-28): test across a WINDOW of dates, not one hardcoded day. The
# agreement gate can legitimately yield 0 signals on a given day, so asserting >0
# on a single date was date-fragile (failed 2026-07-16 while 07-10 produced 2).
if "$PYTHON" -c "
from core.paper_trading_engine import PaperTrader
t = PaperTrader()
total = 0
for d in ['2026-07-10','2026-08-01','2026-06-20','2026-07-25']:
    total += len(t.generate_signals(d))
print(f'SIGNALS: {total} trades generated across window')
assert total > 0, 'No signals generated across the test window'
print('SIGNAL_GEN: PASS')
" 2>/dev/null; then
    PASSED_TESTS+=("SIGNAL_GEN")
else
    echo "SIGNAL_GEN: FAIL"
    FAILED_TESTS+=("SIGNAL_GEN")
fi

# Test 3: Risk control test
if "$PYTHON" -c "
from core.risk_controls import RiskManager, RiskConfig, TradeResult
rm = RiskManager()
for i in range(6):
    result = rm.update_after_trade(TradeResult(f'test_{i}', pnl=-1000.0, is_profitable=False, trade_date='2026-07-16'))
assert not result['passed'], 'Risk should fail after 6 consecutive losses'
print('RISK: PASS')
" 2>/dev/null; then
    PASSED_TESTS+=("RISK")
else
    echo "RISK: FAIL"
    FAILED_TESTS+=("RISK")
fi

# Test 4: Skill gate test
if "$PYTHON" -c "
from core.station_skill_gate import StationSkillGate
gate = StationSkillGate('data/metar_backfill.db')
skilled = gate.get_skilled_stations()
print(f'SKILL_GATE: {len(skilled)} skilled stations')
assert len(skilled) > 0, 'No skilled stations found'
print('SKILL_GATE: PASS')
" 2>/dev/null; then
    PASSED_TESTS+=("SKILL_GATE")
else
    echo "SKILL_GATE: FAIL"
    FAILED_TESTS+=("SKILL_GATE")
fi

# Test 5: Signal registry test
if "$PYTHON" -c "
import sqlite3
# R/T FIX (2026-09-28): the old block printed 'REGISTRY: PASS' on its EXCEPT
# branch -> any import/DB error was reported as a PASS (a false green). A registry
# failure must FAIL the gate. Also assert registry size >= 17 (silent-shrink guard).
import sqlite3
conn = sqlite3.connect('data/metar_backfill.db')
cur = conn.cursor()
cur.execute(\"SELECT name FROM sqlite_master WHERE type='table';\")
tables = [r[0] for r in cur.fetchall()]
conn.close()
if 'metar_observations' not in tables:
    raise SystemExit('REGISTRY: FAIL - missing metar_observations table')
from core.signals.wind_direction_shift import WindDirectionShiftSignal
from core.signals.nwp_analog_signal import NwpAnalogSignal
wind_sig_instance = WindDirectionShiftSignal('data/metar_backfill.db')
nwp_sig_instance = NwpAnalogSignal('data/metar_backfill.db', 'data/metar_backfill.db')
import sys as _sys
if 'core' not in _sys.path:
    _sys.path.insert(0, 'core')
from signals import SignalRegistry
_reg = SignalRegistry('data/metar_backfill.db')
_n = len(_reg.get_all_signals())
assert _n >= 17, f'registry shrank to {_n} (<17) - silent coverage loss'
print(f'REGISTRY: PASS ({_n} signals)')
" 2>/dev/null; then
    PASSED_TESTS+=("REGISTRY")
else
    echo "REGISTRY: FAIL"
    FAILED_TESTS+=("REGISTRY")
fi

# Test 6: NWP signal test
if "$PYTHON" -c "
from core.signals.nwp_analog_signal import NwpAnalogSignal
signal = NwpAnalogSignal(nwp_db_path='data/nwp_forecasts.db', metar_db_path='data/metar_backfill.db')
result = signal.compute_signal('KNYC', '2026-07-12')
assert result is not None and result.get('direction') is not None, 'NWP signal failed'
print(f'NWP: direction={result[\"direction\"]}, confidence={result[\"confidence\"]:.3f}')
print('NWP: PASS')
" 2>/dev/null; then
    PASSED_TESTS+=("NWP")
else
    # Check if this was due to data availability rather than implementation
    NWP_AVAILABLE=$("$PYTHON" -c "
try:
    import sqlite3
    conn = sqlite3.connect('data/nwp_forecasts.db')
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM forecasts LIMIT 1')
    has_data = cursor.fetchone()[0] > 0
    print('AVAIL' if has_data else 'MISSING')
    conn.close()
except Exception as e:
    print('ERROR')
" 2>/dev/null)

    # R/T FIX (2026-09-28): missing NWP data is a SKIP, not a PASS. Previously it
    # was added to PASSED_TESTS -> the gate could report 8/8 with NWP never run.
    if [ "$NWP_AVAILABLE" = "MISSING" ] || [ "$NWP_AVAILABLE" = "ERROR" ]; then
        echo "NWP: SKIP (data not available) - NOT counted as pass"
        SKIPPED_TESTS+=("NWP")
    else
        echo "NWP: FAIL"
        FAILED_TESTS+=("NWP")
    fi
fi

# Test 7: Calibration test
if "$PYTHON" -c "
from core.calibration_pipeline import CalibrationPipeline
cp = CalibrationPipeline(['calendar_climatology', 'late_day_momentum', 'nwp_analog'], ['KNYC', 'KATL'])
print('CALIBRATION: PASS')
" 2>/dev/null; then
    PASSED_TESTS+=("CALIBRATION")
else
    echo "CALIBRATION: FAIL"
    FAILED_TESTS+=("CALIBRATION")
fi

# Print Results Summary
TOTAL_TESTS=${#ALL_TESTS[@]}
PASSED_COUNT=${#PASSED_TESTS[@]}
SKIPPED_COUNT=${#SKIPPED_TESTS[@]}

echo ""
echo "REGRESSION GATE RESULTS: $PASSED_COUNT/$TOTAL_TESTS passed, $SKIPPED_COUNT skipped"
if [ $SKIPPED_COUNT -gt 0 ]; then
    echo "Skipped (NOT passes): ${SKIPPED_TESTS[*]}"
fi

# R/T FIX (2026-09-28): PASS requires EVERY test to pass; skips do not count.
if [ $PASSED_COUNT -eq $TOTAL_TESTS ]; then
    echo "REGRESSION GATE: PASS - All systems nominal"
    exit 0
else
    echo "REGRESSION GATE: FAIL - Critical components failing"
    [ ${#FAILED_TESTS[@]} -gt 0 ] && echo "Failed tests: ${FAILED_TESTS[*]}"
    [ $SKIPPED_COUNT -gt 0 ] && echo "Skipped tests (not passes): ${SKIPPED_TESTS[*]}"
    exit 1
fi
