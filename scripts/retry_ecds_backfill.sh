#!/bin/bash
# Retry ECDS TIGGE backfill. Runs existing script if not rate-limited.
set -e
cd "$(dirname "$0")/.."
CP="data/ecds_backfill_checkpoint.json"
if [ -f "$CP" ]; then
    STATUS=$(python3 -c "import json; c=json.load(open('$CP')); print(c.get('status',''))")
    if [ "$STATUS" = "complete" ]; then
        echo "$(date -u): ECDS backfill already complete"
        exit 0
    fi
fi
python3 -u scripts/ecds_tigge_backfill.py 2021-01-01 2026-06-30 60 2>&1
