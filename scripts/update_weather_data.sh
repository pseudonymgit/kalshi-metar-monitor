#!/bin/bash
# update_weather_data.sh — Periodic weather data collection and push
# Runs every 6 hours via host cron. Collects live data, commits, and pushes.
# This triggers Render auto-deploy for the PROD instance.
#
# Host cron entry (expected):
#   0 */6 * * * /home/gaddams/.openclaw-next/workspace/prototypes/weather-engine-source/scripts/update_weather_data.sh >> /home/gaddams/.openclaw-next/workspace/prototypes/weather-engine-source/logs/update_weather_data.log 2>&1
#
# NOTE: This script is designed to run EITHER from the host OR inside the container.
# It auto-detects the container environment.

set -e

# Determine the script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
LOG_DIR="${REPO_DIR}/logs"
TIMESTAMP="$(date +%Y-%m-%d_%H:%M:%S_UTC)"
COLLECTOR_LOG="${LOG_DIR}/update_weather_data.log"

mkdir -p "$LOG_DIR"

echo "=== update_weather_data.sh — $TIMESTAMP ==="
echo "Repo dir: $REPO_DIR"
echo ""

cd "$REPO_DIR"

# ─── Step 1: Collect METAR data ─────────────────────────────────────────────
echo "--- Step 1: METAR collection ---"
python3 scripts/metar_collect_live.py 2>&1 || echo "WARNING: METAR collection failed, continuing..."
echo ""

# ─── Step 2: Collect NWP data ───────────────────────────────────────────────
echo "--- Step 2: NWP forecast collection ---"
python3 scripts/nwp_collect.py 2>&1 || echo "WARNING: NWP collection failed, continuing..."
echo ""

# ─── Step 3: Collect forecast disagreement data ─────────────────────────────
echo "--- Step 3: Forecast disagreement collection ---"
if [ -f scripts/forecast_disagreement_collector.py ]; then
    python3 scripts/forecast_disagreement_collector.py 2>&1 || echo "WARNING: Forecast disagreement collection failed, continuing..."
else
    echo "Skipping (not found)"
fi
echo ""

# ─── Step 4: Commit and push updated DBs ────────────────────────────────────
echo "--- Step 4: Git commit and push ---"

# Check if there are any changes to commit
if git diff --quiet -- data/; then
    echo "No data changes to commit."
else
    git add data/
    git commit -m "auto: weather data update ${TIMESTAMP}" --quiet 2>&1 || echo "Nothing to commit"
    echo "Pushing to origin..."
    if git push origin main 2>&1; then
        echo "Push successful — will trigger Render auto-deploy."
    else
        echo "WARNING: Push failed (network? auth?). Check git config."
    fi
fi
echo ""

# ─── Step 5: Create rolling merge snapshot ──────────────────────────────────
echo "--- Step 5: Rolling merge snapshot ---"
if [ -f "${REPO_DIR}/.git-snapshot.sh" ]; then
    bash "${REPO_DIR}/.git-snapshot.sh" 2>&1 || echo "WARNING: Snapshot script failed, continuing..."
else
    echo "Skipping (not found)"
fi
echo ""

echo "=== Complete: $TIMESTAMP ==="
echo ""