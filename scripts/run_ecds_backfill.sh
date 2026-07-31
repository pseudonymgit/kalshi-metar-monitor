#!/bin/bash
# ECDS TIGGE Backfill: 1 day at a time, starts from checkpoint
set -e
cd "$(dirname "$0")/.."
mkdir -p data/tigge_grib logs
exec python3 -u scripts/ecds_tigge_backfill.py "$@" 2>&1 | tee -a logs/ecds_tigge_backfill.log