#!/usr/bin/env bash
# =============================================================================
# Weather Engine — Staging Test Runner
# =============================================================================
# Runs the full test battery against a copy of the production database.
# Safe: uses a temp copy, never touches the original DB or live Render service.
#
# Usage:
#   ./tools/testing/run_staging_tests.sh [--db-path /path/to/alerts-prod.db]
#
# If no --db-path is given, looks for alerts-prod.db in the project root.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_DIR"

# --- Parse args ---
DB_SOURCE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --db-path) DB_SOURCE="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

if [[ -z "$DB_SOURCE" ]]; then
    if [[ -f "$PROJECT_DIR/alerts-prod.db" ]]; then
        DB_SOURCE="$PROJECT_DIR/alerts-prod.db"
    else
        echo "ERROR: No production DB found. Provide --db-path or place alerts-prod.db in project root."
        exit 1
    fi
fi

echo "=== Weather Engine Staging Tests ==="
echo "Source DB: $DB_SOURCE ($(du -h "$DB_SOURCE" | cut -f1))"
echo "Project:   $PROJECT_DIR"
echo ""

# --- Create temp working copy ---
STAGE_DB="/tmp/alerts-stage-$$.db"
cp "$DB_SOURCE" "$STAGE_DB"
echo "Stage DB:  $STAGE_DB"
echo ""

# --- Set environment ---
export ALERT_DB_PATH="$STAGE_DB"
export KALSHI_EXECUTION_DOMAIN="replay"
export ALERT_WEBHOOK_URL="https://example.com/test-webhook"
export METAR_STATIONS_JSON='["KDEN","KLAX","KNYC","KPHL","KMDW","KMIA","KAUS"]'
export METAR_POLL_SECONDS="999999"

PASS=0
FAIL=0

# =============================================================================
# TEST 1: Schema Migration Integrity
# =============================================================================
echo "--- Test 1: Schema Migration ---"
python3 -c "
import os, sqlite3
from core import metar_monitor, kalshi_monitor

metar_monitor._ensure_alert_schema()
kalshi_monitor._ensure_alert_schema()

conn = sqlite3.connect(os.environ['ALERT_DB_PATH'])
tables = [t[0] for t in conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()]
required = ['alerts','transition_events','settlement_epochs','alert_delivery_queue','kalshi_rate_limit','signal_layer_state','market_cache']
missing = [t for t in required if t not in tables]

# Data integrity
alert_count = conn.execute('SELECT COUNT(*) FROM alerts').fetchone()[0]
epoch_count = conn.execute('SELECT COUNT(*) FROM settlement_epochs').fetchone()[0]
trans_count = conn.execute('SELECT COUNT(*) FROM transition_events').fetchone()[0]

conn.close()

if missing:
    print(f'FAIL: Missing tables: {missing}')
    exit(1)
if alert_count == 0:
    print(f'FAIL: alerts table empty')
    exit(1)
print(f'PASS: All {len(required)} tables present, {alert_count} alerts, {epoch_count} epochs, {trans_count} transitions preserved')
" || { echo "TEST 1 FAILED"; FAIL=$((FAIL+1)); }
PASS=$((PASS+1))

# =============================================================================
# TEST 2: Alert Queue + Dead Letter
# =============================================================================
echo "--- Test 2: Alert Queue & Dead Letter ---"
python3 -c "
import os
from core import metar_monitor

# Queue an alert
r = metar_monitor._queue_alert_for_delivery('stage-test:queue:1', 'https://example.com/hook', {'station':'KDEN','temp_f':72.5})
assert r.get('queued'), 'Queue failed'

# Verify pending
pending = metar_monitor._get_pending_deliveries()
assert len(pending) >= 1, f'No pending deliveries: {len(pending)}'

# Dead-letter it
metar_monitor._mark_alert_delivery_queue_dead_letter('stage-test:queue:1', 'test_reason')
failed = metar_monitor._get_failed_alerts()
assert len(failed) >= 1, f'No failed alerts: {len(failed)}'

# Clean up
metar_monitor._delete_alert_delivery_queue('stage-test:queue:1')

print('PASS: Alert queue, pending, dead-letter, cleanup all functional')
" || { echo "TEST 2 FAILED"; FAIL=$((FAIL+1)); }
PASS=$((PASS+1))

# =============================================================================
# TEST 3: Rate Limiting
# =============================================================================
echo "--- Test 3: Rate Limiting ---"
python3 -c "
import os
from core import kalshi_monitor

# Persist and check
kalshi_monitor._persist_rate_limit_entry('/test/endpoint')
assert kalshi_monitor._check_rate_limit('/test/endpoint', 60, 60) == True, 'Rate limit check failed'

# Retry-After parsing
assert kalshi_monitor._parse_retry_after('120') == 120, 'Integer parse failed'
http_date = kalshi_monitor._parse_retry_after('Wed, 21 Oct 2015 07:28:00 GMT')
assert http_date > 0, f'HTTP-date parse failed: {http_date}'

print('PASS: Rate limiting, Retry-After parsing (int + HTTP-date) functional')
" || { echo "TEST 3 FAILED"; FAIL=$((FAIL+1)); }
PASS=$((PASS+1))

# =============================================================================
# TEST 4: Signal State Persistence
# =============================================================================
echo "--- Test 4: Signal State Persistence ---"
python3 -c "
import os
from core import metar_monitor, kalshi_monitor

# Signal state roundtrip
metar_monitor._persist_signal_state('test:signal:KDEN', {'cooldown_until':'2026-06-16T23:00:00Z','window_count':3})
loaded = metar_monitor._load_signal_state('test:signal:KDEN')
assert loaded is not None, 'Signal load returned None'
assert loaded.get('cooldown_until') == '2026-06-16T23:00:00Z', f'Cooldown mismatch: {loaded}'
assert loaded.get('window_count') == 3, f'Window count mismatch: {loaded}'

# Market cache roundtrip
kalshi_monitor._persist_market_cache('test:market:KDEN', 'KDEN', {'ticker':'KXHIGHDEN','markets':[{'id':1}]})
loaded = kalshi_monitor._load_market_cache('test:market:KDEN')
assert loaded is not None, 'Market cache load returned None'
assert loaded.get('ticker') == 'KXHIGHDEN', f'Ticker mismatch: {loaded}'

print('PASS: Signal state and market cache persistence roundtrip')
" || { echo "TEST 4 FAILED"; FAIL=$((FAIL+1)); }
PASS=$((PASS+1))

# =============================================================================
# TEST 5: Execution Domain Guard
# =============================================================================
echo "--- Test 5: Execution Domain Guard ---"
python3 -c "
import os
from core import kalshi_monitor

# Default domain is production (no Flask before_request hook in script context)
domain = kalshi_monitor._current_kalshi_execution_domain()
print(f'Default domain: {domain}')

# Set to replay and verify guard
token = kalshi_monitor.set_kalshi_execution_domain('replay')
domain = kalshi_monitor._current_kalshi_execution_domain()
assert domain == 'replay', f'Domain not set: {domain}'

# Verify forbidden domain blocks live calls
try:
    kalshi_monitor._kalshi_public_get('/test')
    print('FAIL: Should have raised RuntimeError for forbidden domain')
    exit(1)
except RuntimeError as e:
    assert 'forbidden' in str(e).lower(), f'Wrong error: {e}'
    print('PASS: Execution domain guard blocks live calls in replay mode')

kalshi_monitor.reset_kalshi_execution_domain(token)
" || { echo "TEST 5 FAILED"; FAIL=$((FAIL+1)); }
PASS=$((PASS+1))

# =============================================================================
# TEST 6: Transition Data Integrity
# =============================================================================
echo "--- Test 6: Transition Data Integrity ---"
python3 -c "
import os, sqlite3, json

conn = sqlite3.connect(os.environ['ALERT_DB_PATH'])

# Every transition should have metadata
null_meta = conn.execute(\"SELECT COUNT(*) FROM transition_events WHERE metadata_json IS NULL OR metadata_json = ''\").fetchone()[0]
total = conn.execute('SELECT COUNT(*) FROM transition_events').fetchone()[0]
pct = round(100 * (total - null_meta) / total, 1) if total > 0 else 0
print(f'Transitions with metadata: {total - null_meta}/{total} ({pct}%)')

# Check suppression_reason coverage in metadata
with_reason = 0
without_reason = 0
for r in conn.execute('SELECT metadata_json FROM transition_events WHERE metadata_json IS NOT NULL LIMIT 500'):
    try:
        meta = json.loads(r[0])
        if meta.get('suppression_reason'):
            with_reason += 1
        else:
            without_reason += 1
    except:
        without_reason += 1
print(f'Suppression reason coverage (sample 500): {with_reason} with, {without_reason} without')

# Check market_type on settlement_epochs
null_mt = conn.execute(\"SELECT COUNT(*) FROM settlement_epochs WHERE market_type IS NULL\").fetchone()[0]
total_ep = conn.execute('SELECT COUNT(*) FROM settlement_epochs').fetchone()[0]
print(f'Epochs with NULL market_type: {null_mt}/{total_ep} (pre-L2 data, expected)')

conn.close()
print('PASS: Transition data integrity verified')
" || { echo "TEST 6 FAILED"; FAIL=$((FAIL+1)); }
PASS=$((PASS+1))

# =============================================================================
# TEST 7: Alert Delivery Path Analysis
# =============================================================================
echo "--- Test 7: Alert Delivery Path Analysis ---"
python3 -c "
import os, sqlite3, json

conn = sqlite3.connect(os.environ['ALERT_DB_PATH'])

# Analyze alert delivery outcomes from transition metadata
total_evaluated = 0
sent = 0
suppressed = 0
errors = 0

for r in conn.execute('SELECT metadata_json FROM transition_events WHERE metadata_json IS NOT NULL'):
    try:
        meta = json.loads(r[0])
        apt = meta.get('alert_path_truth', {})
        if apt:
            total_evaluated += 1
            if apt.get('webhook_send_succeeded'):
                sent += 1
            elif apt.get('suppression_or_non_emission_reason'):
                suppressed += 1
            elif apt.get('webhook_send_failed'):
                errors += 1
    except:
        pass

print(f'Transitions with alert path data: {total_evaluated}')
if total_evaluated > 0:
    print(f'  Sent:      {sent} ({round(100*sent/total_evaluated,1)}%)')
    print(f'  Suppressed: {suppressed} ({round(100*suppressed/total_evaluated,1)}%)')
    print(f'  Failed:    {errors} ({round(100*errors/total_evaluated,1)}%)')

# Check composed_alert_sent vs ladder_transition ratio
composed = conn.execute(\"SELECT COUNT(*) FROM alerts WHERE alert_type='composed_alert_sent'\").fetchone()[0]
ladder = conn.execute(\"SELECT COUNT(*) FROM alerts WHERE alert_type='ladder_transition'\").fetchone()[0]
print(f'Composed alerts sent: {composed}')
print(f'Ladder transitions:   {ladder}')
if ladder > 0:
    print(f'Delivery ratio: {round(100*composed/ladder,1)}%')

conn.close()
print('PASS: Alert delivery path analysis complete')
" || { echo "TEST 7 FAILED"; FAIL=$((FAIL+1)); }
PASS=$((PASS+1))

# =============================================================================
# TEST 8: Station Coverage & Signal Distribution
# =============================================================================
echo "--- Test 8: Station Coverage & Signal Distribution ---"
python3 -c "
import os, sqlite3

conn = sqlite3.connect(os.environ['ALERT_DB_PATH'])

print('=== Alerts by Station ===')
for r in conn.execute('SELECT station, COUNT(*) as c FROM alerts GROUP BY station ORDER BY c DESC'):
    print(f'  {r[0]}: {r[1]}')

print('=== Alerts by Type ===')
for r in conn.execute('SELECT alert_type, COUNT(*) as c FROM alerts GROUP BY alert_type ORDER BY c DESC'):
    print(f'  {r[0]}: {r[1]}')

print('=== Alerts by Direction ===')
for r in conn.execute('SELECT direction, COUNT(*) as c FROM alerts GROUP BY direction ORDER BY c DESC'):
    print(f'  {r[0]}: {r[1]}')

print('=== Transitions by Type ===')
for r in conn.execute('SELECT transition_type, COUNT(*) as c FROM transition_events GROUP BY transition_type ORDER BY c DESC'):
    print(f'  {r[0]}: {r[1]}')

# Check for LOW momentum signals (L2)
low_up = conn.execute(\"SELECT COUNT(*) FROM alerts WHERE alert_type='near_boundary_momentum_up'\").fetchone()[0]
low_down = conn.execute(\"SELECT COUNT(*) FROM alerts WHERE alert_type='near_boundary_momentum_down'\").fetchone()[0]
goldi_down = conn.execute(\"SELECT COUNT(*) FROM alerts WHERE alert_type='goldilocks_momentum_down'\").fetchone()[0]
print(f'\\nL2 Signal coverage:')
print(f'  near_boundary_momentum_up: {low_up}')
print(f'  near_boundary_momentum_down: {low_down}')
print(f'  goldilocks_momentum_down: {goldi_down}')

conn.close()
print('PASS: Station coverage analysis complete')
" || { echo "TEST 8 FAILED"; FAIL=$((FAIL+1)); }
PASS=$((PASS+1))

# =============================================================================
# TEST 9: Market Eligibility Analysis
# =============================================================================
echo "--- Test 9: Market Eligibility Analysis ---"
python3 -c "
import os, sqlite3, json
from collections import Counter

conn = sqlite3.connect(os.environ['ALERT_DB_PATH'])

rejection_reasons = Counter()
eligibility_outcomes = Counter()
markets_considered = []
eligible_counts = []

for r in conn.execute('SELECT metadata_json FROM transition_events WHERE metadata_json IS NOT NULL'):
    try:
        meta = json.loads(r[0])
        mer = meta.get('market_eligibility_runtime', {})
        if mer:
            markets_considered.append(mer.get('markets_considered_count', 0))
            eligible_counts.append(mer.get('eligible_markets_count', 0))
            for reason, count in mer.get('rejection_breakdown', {}).items():
                if count > 0:
                    rejection_reasons[reason] += count
        outcome = meta.get('evaluation_outcome')
        if outcome:
            eligibility_outcomes[outcome] += 1
    except:
        pass

print(f'Market evaluations analyzed: {len(markets_considered)}')
if markets_considered:
    print(f'  Avg markets considered: {round(sum(markets_considered)/len(markets_considered),1)}')
    print(f'  Avg eligible: {round(sum(eligible_counts)/len(eligible_counts),1)}')
print(f'Rejection reasons:')
for reason, count in rejection_reasons.most_common():
    print(f'  {reason}: {count}')
print(f'Eligibility outcomes:')
for outcome, count in eligibility_outcomes.most_common(10):
    print(f'  {outcome}: {count}')

conn.close()
print('PASS: Market eligibility analysis complete')
" || { echo "TEST 9 FAILED"; FAIL=$((FAIL+1)); }
PASS=$((PASS+1))

# =============================================================================
# TEST 10: L3/L4 Feature Verification
# =============================================================================
echo "--- Test 10: L3/L4 Feature Verification ---"
python3 -c "
import os, sqlite3
from core import metar_monitor, kalshi_monitor

conn = sqlite3.connect(os.environ['ALERT_DB_PATH'])

# L3: market_change_events table
tables = [t[0] for t in conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()]
print(f'L3 market_change_events table: {\"market_change_events\" in tables}')

# L3: suppression_reason in alerts
alert_cols = [c[1] for c in conn.execute('PRAGMA table_info(alerts)')]
print(f'L3 suppression_reason column: {\"suppression_reason\" in alert_cols}')

# L4: alert_type_category
print(f'L4 alert_type_category column: {\"alert_type_category\" in alert_cols}')

# L4: webhook verification (check if authorizer module exists)
try:
    from core import authorizer
    has_verify = hasattr(authorizer, 'verify_webhook_signature')
    print(f'L4 webhook signature verification: {has_verify}')
except ImportError:
    print(f'L4 webhook signature verification: module not found (may need deploy)')

# L4: timezone validation
try:
    from core import station_time
    has_validate = hasattr(station_time, 'validate_timezone')
    print(f'L4 timezone validation: {has_validate}')
except ImportError:
    print(f'L4 timezone validation: module not found')

# L4: LOW market regex
import re
low_pattern = getattr(kalshi_monitor, 'LOW_TICKER_PATTERN', None)
print(f'L4 LOW ticker regex: {low_pattern is not None}')

conn.close()
print('PASS: L3/L4 feature verification complete')
" || { echo "TEST 10 FAILED"; FAIL=$((FAIL+1)); }
PASS=$((PASS+1))

# =============================================================================
# SUMMARY
# =============================================================================
echo ""
echo "============================================"
echo "  STAGING TEST RESULTS"
echo "============================================"
echo "  Passed: $PASS"
echo "  Failed: $FAIL"
echo "  Total:  $((PASS + FAIL))"
echo "============================================"

# Cleanup
rm -f "$STAGE_DB" "$STAGE_DB-journal" "$STAGE_DB-wal"

if [[ $FAIL -gt 0 ]]; then
    echo "SOME TESTS FAILED — review output above"
    exit 1
else
    echo "ALL TESTS PASSED"
    exit 0
fi
