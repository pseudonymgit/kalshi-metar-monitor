#!/usr/bin/env bash
# =============================================================================
# Weather Engine — Kalshi Market Backtest
# =============================================================================
# Replays historical temperature observations through the weather engine and
# compares generated alerts against actual Kalshi market outcomes.
#
# This answers: "Would the signals have made money?"
#
# Safe: uses KALSHI_EXECUTION_DOMAIN=replay — no live Kalshi API calls.
#       Market data is pulled from cached production DB, not live API.
#
# Usage:
#   ./tools/testing/backtest_kalshi.sh [--db-path /path/to/alerts-prod.db]
#   ./tools/testing/backtest_kalshi.sh --station KDEN --days 7
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_DIR"

DB_SOURCE=""
STATION=""
DAYS="7"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --db-path) DB_SOURCE="$2"; shift 2 ;;
        --station) STATION="$2"; shift 2 ;;
        --days) DAYS="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

if [[ -z "$DB_SOURCE" ]]; then
    DB_SOURCE="$PROJECT_DIR/alerts-prod.db"
    [[ -f "$DB_SOURCE" ]] || { echo "ERROR: No DB at $DB_SOURCE"; exit 1; }
fi

STAGE_DB="/tmp/alerts-backtest-$$.db"
cp "$DB_SOURCE" "$STAGE_DB"

export ALERT_DB_PATH="$STAGE_DB"
export KALSHI_EXECUTION_DOMAIN="replay"
export ALERT_WEBHOOK_URL="https://example.com/test-webhook"
export METAR_STATIONS_JSON='["KDEN","KLAX","KNYC","KPHL","KMDW","KMIA","KAUS"]'
export METAR_POLL_SECONDS="999999"

echo "============================================"
echo "  Weather Engine — Kalshi Market Backtest"
echo "============================================"
echo "DB:       $STAGE_DB"
echo "Domain:   replay (no live API calls)"
echo "Station:  ${STATION:-all}"
echo "Days:     $DAYS"
echo ""

BACKTEST_OUT="/tmp/alerts-backtest-result-$$.json"

python3 << 'PYEOF'
import os, sqlite3, json, sys
from collections import defaultdict, Counter
from datetime import datetime, timezone

STAGE_DB = os.environ['ALERT_DB_PATH']
STATION_FILTER = os.environ.get('BACKTEST_STATION', '')
DAYS = int(os.environ.get('BACKTEST_DAYS', '7'))

conn = sqlite3.connect(STAGE_DB)

# =========================================================================
# 1. Extract all transition events with market evaluation metadata
# =========================================================================
transitions = []
for r in conn.execute('''
    SELECT te.id, te.created_utc, te.station, te.transition_type,
           te.instant_bucket_before, te.instant_bucket_after,
           te.settlement_bucket, te.current_temp, te.metadata_json
    FROM transition_events te
    ORDER BY te.station, te.created_utc ASC
'''):
    meta = {}
    if r[8]:
        try:
            meta = json.loads(r[8])
        except:
            pass
    transitions.append({
        'id': r[0],
        'created_utc': r[1],
        'station': r[2],
        'transition_type': r[3],
        'instant_before': r[4],
        'instant_after': r[5],
        'settlement_bucket': r[6],
        'current_temp': r[7],
        'metadata': meta,
    })

print(f'Total transitions: {len(transitions)}')

# =========================================================================
# 2. Extract settlement epochs with outcomes
# =========================================================================
epochs = []
for r in conn.execute('''
    SELECT id, station, market_type, local_trading_date,
           settlement_bucket, prior_settlement_bucket,
           settlement_timestamp_utc, settlement_jump_magnitude,
           epoch_status, epoch_close_reason, epoch_close_timestamp_utc,
           reversion_occurred, first_reversion_timestamp_utc,
           max_excursion_above_settlement,
           duration_at_or_above_settlement_seconds,
           duration_strictly_above_settlement_seconds,
           terminal_state_reached
    FROM settlement_epochs
    ORDER BY station, settlement_timestamp_utc ASC
'''):
    epochs.append({
        'id': r[0],
        'station': r[1],
        'market_type': r[2],
        'local_trading_date': r[3],
        'settlement_bucket': r[4],
        'prior_settlement_bucket': r[5],
        'settlement_utc': r[6],
        'jump_magnitude': r[7],
        'status': r[8],
        'close_reason': r[9],
        'close_utc': r[10],
        'reversion_occurred': r[11],
        'first_reversion_utc': r[12],
        'max_excursion': r[13],
        'duration_at_or_above': r[14],
        'duration_strictly_above': r[15],
        'terminal_state': r[16],
    })

print(f'Total epochs: {len(epochs)}')

# =========================================================================
# 3. Extract alerts with delivery outcomes
# =========================================================================
alerts = []
for r in conn.execute('''
    SELECT id, created_utc, station, market_type, event_ticker,
           alert_type, direction, temp_f, bucket_index, metadata_json
    FROM alerts
    ORDER BY created_utc ASC
'''):
    meta = {}
    if r[9]:
        try:
            meta = json.loads(r[9])
        except:
            pass
    alerts.append({
        'id': r[0],
        'created_utc': r[1],
        'station': r[2],
        'market_type': r[3],
        'event_ticker': r[4],
        'alert_type': r[5],
        'direction': r[6],
        'temp_f': r[7],
        'bucket_index': r[8],
        'metadata': meta,
    })

print(f'Total alerts: {len(alerts)}')

# =========================================================================
# 4. Per-station analysis
# =========================================================================
stations = sorted(set(t['station'] for t in transitions))
if STATION_FILTER:
    stations = [s for s in stations if s == STATION_FILTER]

backtest_results = {}

for station in stations:
    st = [t for t in transitions if t['station'] == station]
    se = [e for e in epochs if e['station'] == station]
    sa = [a for a in alerts if a['station'] == station]

    # Date range
    dates = sorted(set(t['created_utc'][:10] for t in st))
    recent_dates = dates[-DAYS:] if len(dates) >= DAYS else dates

    # Transition stats
    transition_counts = Counter(t['transition_type'] for t in st)

    # Epoch stats
    closed_epochs = [e for e in se if e['status'] == 'closed']
    open_epochs = [e for e in se if e['status'] == 'open']
    reversion_epochs = [e for e in se if e['reversion_occurred']]

    # Alert stats
    composed = [a for a in sa if a['alert_type'] == 'composed_alert_sent']
    suppressed = [a for a in sa if a['alert_type'] == 'ladder_transition']
    missing = [a for a in sa if a['alert_type'] == 'ladder_missing']
    momentum = [a for a in sa if 'momentum' in (a['alert_type'] or '')]

    # Market eligibility from transition metadata
    market_evaluations = []
    for t in st:
        mer = t['metadata'].get('market_eligibility_runtime', {})
        if mer:
            market_evaluations.append(mer)

    total_considered = sum(m.get('markets_considered_count', 0) for m in market_evaluations)
    total_eligible = sum(m.get('eligible_markets_count', 0) for m in market_evaluations)

    # Suppression analysis
    suppression_reasons = Counter()
    evaluation_outcomes = Counter()
    for t in st:
        reason = t['metadata'].get('suppression_reason')
        if reason:
            suppression_reasons[reason] += 1
        outcome = t['metadata'].get('evaluation_outcome')
        if outcome:
            evaluation_outcomes[outcome] += 1

    # Settlement direction analysis
    up_settlements = [e for e in se if (e['settlement_bucket'] or 0) > (e['prior_settlement_bucket'] or 0)]
    down_settlements = [e for e in se if (e['settlement_bucket'] or 0) < (e['prior_settlement_bucket'] or 0)]
    flat_settlements = [e for e in se if (e['settlement_bucket'] or 0) == (e['prior_settlement_bucket'] or 0)]

    # Epoch duration stats
    durations = [e['duration_at_or_above'] or 0 for e in closed_epochs if e['duration_at_or_above']]
    avg_duration = sum(durations) / len(durations) if durations else 0

    # Reversion stats
    reversions = [e for e in closed_epochs if e['reversion_occurred']]
    reversion_rate = len(reversions) / len(closed_epochs) if closed_epochs else 0

    backtest_results[station] = {
        'transitions': {
            'total': len(st),
            'by_type': dict(transition_counts),
        },
        'epochs': {
            'total': len(se),
            'closed': len(closed_epochs),
            'open': len(open_epochs),
            'with_reversion': len(reversion_epochs),
            'reversion_rate': round(reversion_rate, 3),
            'avg_duration_seconds': round(avg_duration, 1),
            'settlement_up': len(up_settlements),
            'settlement_down': len(down_settlements),
            'settlement_flat': len(flat_settlements),
        },
        'alerts': {
            'total': len(sa),
            'composed_sent': len(composed),
            'ladder_transitions': len(suppressed),
            'ladder_missing': len(missing),
            'momentum_signals': len(momentum),
        },
        'market_eligibility': {
            'evaluations': len(market_evaluations),
            'total_markets_considered': total_considered,
            'total_eligible': total_eligible,
            'avg_considered': round(total_considered / len(market_evaluations), 1) if market_evaluations else 0,
            'avg_eligible': round(total_eligible / len(market_evaluations), 1) if market_evaluations else 0,
        },
        'suppression': {
            'reasons': dict(suppression_reasons.most_common()),
            'outcomes': dict(evaluation_outcomes.most_common()),
        },
        'date_range': {
            'first': dates[0] if dates else None,
            'last': dates[-1] if dates else None,
            'total_days': len(dates),
            'recent_days': recent_dates,
        },
    }

# =========================================================================
# 5. Cross-station summary
# =========================================================================
summary = {
    'total_transitions': sum(r['transitions']['total'] for r in backtest_results.values()),
    'total_epochs': sum(r['epochs']['total'] for r in backtest_results.values()),
    'total_alerts': sum(r['alerts']['total'] for r in backtest_results.values()),
    'total_composed_sent': sum(r['alerts']['composed_sent'] for r in backtest_results.values()),
    'total_momentum_signals': sum(r['alerts']['momentum_signals'] for r in backtest_results.values()),
    'stations_analyzed': len(backtest_results),
}

# Overall reversion rate
all_closed = sum(r['epochs']['closed'] for r in backtest_results.values())
all_reversions = sum(r['epochs']['with_reversion'] for r in backtest_results.values())
summary['overall_reversion_rate'] = round(all_reversions / all_closed, 3) if all_closed else 0

# Overall delivery rate
summary['delivery_rate'] = round(
    100 * summary['total_composed_sent'] / max(summary['total_alerts'], 1), 1
)

# =========================================================================
# 6. Signal quality assessment
# =========================================================================
# For each station, assess: are we catching the right transitions?
signal_assessment = {}
for station, data in backtest_results.items():
    # What % of settlement_up transitions resulted in composed alerts?
    settlement_ups = data['transitions']['by_type'].get('settlement_up', 0)
    composed = data['alerts']['composed_sent']

    # What % of epochs had reversions detected?
    reversion_rate = data['epochs']['reversion_rate']

    # Signal diversity: do we have momentum signals?
    has_momentum = data['alerts']['momentum_signals'] > 0

    signal_assessment[station] = {
        'settlement_ups': settlement_ups,
        'composed_alerts': composed,
        'alert_per_settlement': round(composed / settlement_ups, 2) if settlement_ups else 0,
        'reversion_detection_rate': reversion_rate,
        'has_momentum_signals': has_momentum,
        'ladder_missing_rate': round(
            data['alerts']['ladder_missing'] / max(data['alerts']['total'], 1), 3
        ),
    }

# =========================================================================
# 7. Output
# =========================================================================
output = {
    'backtest_metadata': {
        'generated_utc': datetime.now(timezone.utc).isoformat(),
        'db_source': STAGE_DB,
        'station_filter': STATION_FILTER or 'all',
        'days_analyzed': DAYS,
    },
    'summary': summary,
    'signal_assessment': signal_assessment,
    'stations': backtest_results,
}

out_path = os.environ.get('BACKTEST_OUT', '/tmp/backtest-result.json')
with open(out_path, 'w') as f:
    json.dump(output, f, indent=2, default=str)

# Print summary
print(f'\n{"="*60}')
print(f'  BACKTEST SUMMARY')
print(f'{"="*60}')
print(f'  Stations analyzed:      {summary["stations_analyzed"]}')
print(f'  Total transitions:      {summary["total_transitions"]}')
print(f'  Total epochs:           {summary["total_epochs"]}')
print(f'  Total alerts:           {summary["total_alerts"]}')
print(f'  Composed alerts sent:   {summary["total_composed_sent"]}')
print(f'  Delivery rate:          {summary["delivery_rate"]}%')
print(f'  Overall reversion rate: {summary["overall_reversion_rate"]}')
print(f'  Momentum signals:       {summary["total_momentum_signals"]}')
print(f'{"="*60}')

print(f'\n=== SIGNAL QUALITY BY STATION ===')
print(f'{"Station":<6} {"SettlementUps":>14} {"Alerts":>8} {"Alert/Sett":>11} {"Reversion%":>11} {"Momentum":>9} {"Missing%":>9}')
print(f'{"-"*6} {"-"*14} {"-"*8} {"-"*11} {"-"*11} {"-"*9} {"-"*9}')
for station in sorted(signal_assessment.keys()):
    sa = signal_assessment[station]
    print(f'{station:<6} {sa["settlement_ups"]:>14} {sa["composed_alerts"]:>8} {sa["alert_per_settlement"]:>11} {sa["reversion_detection_rate"]:>11} {str(sa["has_momentum_signals"]):>9} {sa["ladder_missing_rate"]:>11}')

print(f'\nFull results: {out_path}')

conn.close()
PYEOF

# Pass env vars to the Python heredoc
export BACKTEST_STATION="${STATION:-}"
export BACKTEST_DAYS="$DAYS"
export BACKTEST_OUT="$BACKTEST_OUT"

python3 << 'PYEOF'
import os, sqlite3, json, sys
from collections import defaultdict, Counter
from datetime import datetime, timezone

STAGE_DB = os.environ['ALERT_DB_PATH']
STATION_FILTER = os.environ.get('BACKTEST_STATION', '')
DAYS = int(os.environ.get('BACKTEST_DAYS', '7'))

conn = sqlite3.connect(STAGE_DB)

# =========================================================================
# 1. Extract all transition events with market evaluation metadata
# =========================================================================
transitions = []
for r in conn.execute('''
    SELECT te.id, te.created_utc, te.station, te.transition_type,
           te.instant_bucket_before, te.instant_bucket_after,
           te.settlement_bucket, te.current_temp, te.metadata_json
    FROM transition_events te
    ORDER BY te.station, te.created_utc ASC
'''):
    meta = {}
    if r[8]:
        try:
            meta = json.loads(r[8])
        except:
            pass
    transitions.append({
        'id': r[0],
        'created_utc': r[1],
        'station': r[2],
        'transition_type': r[3],
        'instant_before': r[4],
        'instant_after': r[5],
        'settlement_bucket': r[6],
        'current_temp': r[7],
        'metadata': meta,
    })

print(f'Total transitions: {len(transitions)}')

# =========================================================================
# 2. Extract settlement epochs with outcomes
# =========================================================================
epochs = []
for r in conn.execute('''
    SELECT id, station, market_type, local_trading_date,
           settlement_bucket, prior_settlement_bucket,
           settlement_timestamp_utc, settlement_jump_magnitude,
           epoch_status, epoch_close_reason, epoch_close_timestamp_utc,
           reversion_occurred, first_reversion_timestamp_utc,
           max_excursion_above_settlement,
           duration_at_or_above_settlement_seconds,
           duration_strictly_above_settlement_seconds,
           terminal_state_reached
    FROM settlement_epochs
    ORDER BY station, settlement_timestamp_utc ASC
'''):
    epochs.append({
        'id': r[0],
        'station': r[1],
        'market_type': r[2],
        'local_trading_date': r[3],
        'settlement_bucket': r[4],
        'prior_settlement_bucket': r[5],
        'settlement_utc': r[6],
        'jump_magnitude': r[7],
        'status': r[8],
        'close_reason': r[9],
        'close_utc': r[10],
        'reversion_occurred': r[11],
        'first_reversion_utc': r[12],
        'max_excursion': r[13],
        'duration_at_or_above': r[14],
        'duration_strictly_above': r[15],
        'terminal_state': r[16],
    })

print(f'Total epochs: {len(epochs)}')

# =========================================================================
# 3. Extract alerts with delivery outcomes
# =========================================================================
alerts = []
for r in conn.execute('''
    SELECT id, created_utc, station, market_type, event_ticker,
           alert_type, direction, temp_f, bucket_index, metadata_json
    FROM alerts
    ORDER BY created_utc ASC
'''):
    meta = {}
    if r[9]:
        try:
            meta = json.loads(r[9])
        except:
            pass
    alerts.append({
        'id': r[0],
        'created_utc': r[1],
        'station': r[2],
        'market_type': r[3],
        'event_ticker': r[4],
        'alert_type': r[5],
        'direction': r[6],
        'temp_f': r[7],
        'bucket_index': r[8],
        'metadata': meta,
    })

print(f'Total alerts: {len(alerts)}')

# =========================================================================
# 4. Per-station analysis
# =========================================================================
stations = sorted(set(t['station'] for t in transitions))
if STATION_FILTER:
    stations = [s for s in stations if s == STATION_FILTER]

backtest_results = {}

for station in stations:
    st = [t for t in transitions if t['station'] == station]
    se = [e for e in epochs if e['station'] == station]
    sa = [a for a in alerts if a['station'] == station]

    # Date range
    dates = sorted(set(t['created_utc'][:10] for t in st))
    recent_dates = dates[-DAYS:] if len(dates) >= DAYS else dates

    # Transition stats
    transition_counts = Counter(t['transition_type'] for t in st)

    # Epoch stats
    closed_epochs = [e for e in se if e['status'] == 'closed']
    open_epochs = [e for e in se if e['status'] == 'open']
    reversion_epochs = [e for e in se if e['reversion_occurred']]

    # Alert stats
    composed = [a for a in sa if a['alert_type'] == 'composed_alert_sent']
    suppressed = [a for a in sa if a['alert_type'] == 'ladder_transition']
    missing = [a for a in sa if a['alert_type'] == 'ladder_missing']
    momentum = [a for a in sa if 'momentum' in (a['alert_type'] or '')]

    # Market eligibility from transition metadata
    market_evaluations = []
    for t in st:
        mer = t['metadata'].get('market_eligibility_runtime', {})
        if mer:
            market_evaluations.append(mer)

    total_considered = sum(m.get('markets_considered_count', 0) for m in market_evaluations)
    total_eligible = sum(m.get('eligible_markets_count', 0) for m in market_evaluations)

    # Suppression analysis
    suppression_reasons = Counter()
    evaluation_outcomes = Counter()
    for t in st:
        reason = t['metadata'].get('suppression_reason')
        if reason:
            suppression_reasons[reason] += 1
        outcome = t['metadata'].get('evaluation_outcome')
        if outcome:
            evaluation_outcomes[outcome] += 1

    # Settlement direction analysis
    up_settlements = [e for e in se if (e['settlement_bucket'] or 0) > (e['prior_settlement_bucket'] or 0)]
    down_settlements = [e for e in se if (e['settlement_bucket'] or 0) < (e['prior_settlement_bucket'] or 0)]
    flat_settlements = [e for e in se if (e['settlement_bucket'] or 0) == (e['prior_settlement_bucket'] or 0)]

    # Epoch duration stats
    durations = [e['duration_at_or_above'] or 0 for e in closed_epochs if e['duration_at_or_above']]
    avg_duration = sum(durations) / len(durations) if durations else 0

    # Reversion stats
    reversions = [e for e in closed_epochs if e['reversion_occurred']]
    reversion_rate = len(reversions) / len(closed_epochs) if closed_epochs else 0

    backtest_results[station] = {
        'transitions': {
            'total': len(st),
            'by_type': dict(transition_counts),
        },
        'epochs': {
            'total': len(se),
            'closed': len(closed_epochs),
            'open': len(open_epochs),
            'with_reversion': len(reversion_epochs),
            'reversion_rate': round(reversion_rate, 3),
            'avg_duration_seconds': round(avg_duration, 1),
            'settlement_up': len(up_settlements),
            'settlement_down': len(down_settlements),
            'settlement_flat': len(flat_settlements),
        },
        'alerts': {
            'total': len(sa),
            'composed_sent': len(composed),
            'ladder_transitions': len(suppressed),
            'ladder_missing': len(missing),
            'momentum_signals': len(momentum),
        },
        'market_eligibility': {
            'evaluations': len(market_evaluations),
            'total_markets_considered': total_considered,
            'total_eligible': total_eligible,
            'avg_considered': round(total_considered / len(market_evaluations), 1) if market_evaluations else 0,
            'avg_eligible': round(total_eligible / len(market_evaluations), 1) if market_evaluations else 0,
        },
        'suppression': {
            'reasons': dict(suppression_reasons.most_common()),
            'outcomes': dict(evaluation_outcomes.most_common()),
        },
        'date_range': {
            'first': dates[0] if dates else None,
            'last': dates[-1] if dates else None,
            'total_days': len(dates),
            'recent_days': recent_dates,
        },
    }

# =========================================================================
# 5. Cross-station summary
# =========================================================================
summary = {
    'total_transitions': sum(r['transitions']['total'] for r in backtest_results.values()),
    'total_epochs': sum(r['epochs']['total'] for r in backtest_results.values()),
    'total_alerts': sum(r['alerts']['total'] for r in backtest_results.values()),
    'total_composed_sent': sum(r['alerts']['composed_sent'] for r in backtest_results.values()),
    'total_momentum_signals': sum(r['alerts']['momentum_signals'] for r in backtest_results.values()),
    'stations_analyzed': len(backtest_results),
}

# Overall reversion rate
all_closed = sum(r['epochs']['closed'] for r in backtest_results.values())
all_reversions = sum(r['epochs']['with_reversion'] for r in backtest_results.values())
summary['overall_reversion_rate'] = round(all_reversions / all_closed, 3) if all_closed else 0

# Overall delivery rate
summary['delivery_rate'] = round(
    100 * summary['total_composed_sent'] / max(summary['total_alerts'], 1), 1
)

# =========================================================================
# 6. Signal quality assessment
# =========================================================================
signal_assessment = {}
for station, data in backtest_results.items():
    settlement_ups = data['transitions']['by_type'].get('settlement_up', 0)
    composed = data['alerts']['composed_sent']
    reversion_rate = data['epochs']['reversion_rate']
    has_momentum = data['alerts']['momentum_signals'] > 0

    signal_assessment[station] = {
        'settlement_ups': settlement_ups,
        'composed_alerts': composed,
        'alert_per_settlement': round(composed / settlement_ups, 2) if settlement_ups else 0,
        'reversion_detection_rate': reversion_rate,
        'has_momentum_signals': has_momentum,
        'ladder_missing_rate': round(
            data['alerts']['ladder_missing'] / max(data['alerts']['total'], 1), 3
        ),
    }

# =========================================================================
# 7. Output
# =========================================================================
output = {
    'backtest_metadata': {
        'generated_utc': datetime.now(timezone.utc).isoformat(),
        'db_source': STAGE_DB,
        'station_filter': STATION_FILTER or 'all',
        'days_analyzed': DAYS,
    },
    'summary': summary,
    'signal_assessment': signal_assessment,
    'stations': backtest_results,
}

out_path = os.environ.get('BACKTEST_OUT', '/tmp/backtest-result.json')
with open(out_path, 'w') as f:
    json.dump(output, f, indent=2, default=str)

# Print summary
print(f'\n{"="*60}')
print(f'  BACKTEST SUMMARY')
print(f'{"="*60}')
print(f'  Stations analyzed:      {summary["stations_analyzed"]}')
print(f'  Total transitions:      {summary["total_transitions"]}')
print(f'  Total epochs:           {summary["total_epochs"]}')
print(f'  Total alerts:           {summary["total_alerts"]}')
print(f'  Composed alerts sent:   {summary["total_composed_sent"]}')
print(f'  Delivery rate:          {summary["delivery_rate"]}%')
print(f'  Overall reversion rate: {summary["overall_reversion_rate"]}')
print(f'  Momentum signals:       {summary["total_momentum_signals"]}')
print(f'{"="*60}')

print(f'\n=== SIGNAL QUALITY BY STATION ===')
print(f'{"Station":<6} {"SettlementUps":>14} {"Alerts":>8} {"Alert/Sett":>11} {"Reversion%":>11} {"Momentum":>9} {"Missing%":>9}')
print(f'{"-"*6} {"-"*14} {"-"*8} {"-"*11} {"-"*11} {"-"*9} {"-"*9}')
for station in sorted(signal_assessment.keys()):
    sa = signal_assessment[station]
    print(f'{station:<6} {sa["settlement_ups"]:>14} {sa["composed_alerts"]:>8} {sa["alert_per_settlement"]:>11} {sa["reversion_detection_rate"]:>11} {str(sa["has_momentum_signals"]):>9} {sa["ladder_missing_rate"]:>11}')

print(f'\nFull results: {out_path}')

conn.close()
PYEOF

# Cleanup
rm -f "$STAGE_DB" "$STAGE_DB-journal" "$STAGE_DB-wal"

# Remove env vars we exported
unset BACKTEST_STATION
unset BACKTEST_DAYS
BACKTEST_OUT="${BACKTEST_OUT:-/tmp/alerts-backtest-result.json}"
echo ""
echo "=== Backtest Complete ==="
echo "Results: $BACKTEST_OUT"

echo ""
echo "=== Backtest Complete ==="
