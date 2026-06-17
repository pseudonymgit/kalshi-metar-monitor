#!/usr/bin/env bash
# =============================================================================
# Weather Engine — Local Alert Delivery Test
# =============================================================================
# Spins up the Flask app locally against a copy of the production DB,
# feeds it historical METAR data, and verifies alert outputs.
#
# Safe: uses KALSHI_EXECUTION_DOMAIN=replay — no live Kalshi API calls.
#       Uses a temp DB copy — never touches the original.
#
# Usage:
#   ./tools/testing/test_alert_delivery.sh [--db-path /path/to/alerts-prod.db]
#   ./tools/testing/test_alert_delivery.sh --station KDEN --date 2026-06-16
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_DIR"

DB_SOURCE=""
STATION=""
DATE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --db-path) DB_SOURCE="$2"; shift 2 ;;
        --station) STATION="$2"; shift 2 ;;
        --date) DATE="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

if [[ -z "$DB_SOURCE" ]]; then
    DB_SOURCE="$PROJECT_DIR/alerts-prod.db"
    [[ -f "$DB_SOURCE" ]] || { echo "ERROR: No DB at $DB_SOURCE"; exit 1; }
fi

STAGE_DB="/tmp/alerts-delivery-test-$$.db"
cp "$DB_SOURCE" "$STAGE_DB"

export ALERT_DB_PATH="$STAGE_DB"
export KALSHI_EXECUTION_DOMAIN="replay"
export ALERT_WEBHOOK_URL="https://example.com/test-webhook"
export METAR_STATIONS_JSON='["KDEN","KLAX","KNYC","KPHL","KMDW","KMIA","KAUS"]'
export METAR_POLL_SECONDS="999999"

echo "=== Local Alert Delivery Test ==="
echo "DB: $STAGE_DB"
echo "Domain: replay (no live Kalshi calls)"
echo ""

# =============================================================================
# 1. Extract historical observations from transition metadata
# =============================================================================
echo "--- Extracting historical observation sequences ---"
OBS_FILE="/tmp/alerts-delivery-obs-$$.json"
export OBS_FILE="$OBS_FILE"

python3 "$SCRIPT_DIR/extract_observations.py" || { echo "FAILED: observation extraction"; exit 1; }

# =============================================================================
# 2. Replay observations through the engine
# =============================================================================
echo ""
echo "--- Replaying observations through alert pipeline ---"

# Select station/date
if [[ -z "$STATION" ]]; then
    STATION="KDEN"
fi
if [[ -z "$DATE" ]]; then
    DATE=$(python3 -c "
import json
with open('$OBS_FILE') as f:
    data = json.load(f)
dates = sorted(data.get('$STATION',{}).get('by_date',{}).keys())
print(dates[-1] if dates else '')
")
    [[ -z "$DATE" ]] && { echo "ERROR: No data for station $STATION"; exit 1; }
fi

echo "Station: $STATION"
echo "Date:    $DATE"
echo ""

REPLAY_RESULT="/tmp/alerts-delivery-replay-$$.json"
export ALERT_DB_PATH="$STAGE_DB"
export OBS_FILE="$OBS_FILE"
export STATION="$STATION"
export DATE="$DATE"
export REPLAY_RESULT="$REPLAY_RESULT"

python3 "$SCRIPT_DIR/replay_observations.py" || { echo "FAILED: replay execution"; exit 1; }

# =============================================================================
# 3. Validate results
# =============================================================================
echo ""
echo "--- Validating replay results ---"

export REPLAY_RESULT="$REPLAY_RESULT"
python3 "$SCRIPT_DIR/validate_replay.py" || { echo "VALIDATION FAILED"; exit 1; }

# =============================================================================
# 4. Compare replay vs production alerts for same station/date
# =============================================================================
echo ""
echo "--- Comparing replay vs production alerts ---"

python3 -c "
import os, sqlite3, json

STAGE_DB = os.environ.get('STAGE_DB', '$STAGE_DB')
STATION = os.environ.get('STATION', 'KDEN')
DATE = os.environ.get('DATE', '2026-06-16')
REPLAY_RESULT = os.environ.get('REPLAY_RESULT', '$REPLAY_RESULT')

conn = sqlite3.connect(STAGE_DB)

prod_alerts = {}
for r in conn.execute('''
    SELECT alert_type, COUNT(*) as c
    FROM alerts
    WHERE station = ? AND substr(created_utc,1,10) = ?
    GROUP BY alert_type
''', (STATION, DATE)):
    prod_alerts[r[0]] = r[1]

conn.close()

with open(REPLAY_RESULT) as f:
    replay = json.load(f)

print(f'Production alerts for {STATION} on {DATE}:')
for at, count in sorted(prod_alerts.items()):
    print(f'  {at}: {count}')

print(f'\\nReplay summary:')
print(f'  Observations: {replay[\"observations\"]}')
print(f'  Ingested: {replay[\"ingested\"]}')
print(f'  Alerts generated: {replay[\"alerts_generated\"]}')
print(f'  Queue pending: {replay[\"pending_queue\"]}')

print('\\nNote: Exact match not expected — replay uses reconstructed observations,')
print('not the original METAR feed. Differences indicate data reconstruction gaps,')
print('not necessarily signal logic bugs.')
" || echo "Comparison completed (non-fatal)"

# Cleanup
rm -f "$STAGE_DB" "$STAGE_DB-journal" "$STAGE_DB-wal" "$OBS_FILE" "$REPLAY_RESULT"

echo ""
echo "=== Local Alert Delivery Test Complete ==="
