#!/usr/bin/env bash
#
# launch_weather_instances.sh (v1.0 — 2026-07-05)
#
# Starts the weather engine paper trading instances locally.
# Each instance runs in the background with its own:
#   - DB/ledger path
#   - Discord webhook
#   - Scheduler lock file
#   - Log file
#
# Usage:
#   ./scripts/launch_weather_instances.sh [instances...]
#   ./scripts/launch_weather_instances.sh DEV SBOX       # launch DEV + SBOX
#   ./scripts/launch_weather_instances.sh DEV SBOX PROD   # launch all three
#   ./scripts/launch_weather_instances.sh                 # default: DEV SBOX
#
# To stop: kill the PIDs in data/*.pid files
#   kill $(cat data/.dev.pid data/.sbox.pid 2>/dev/null)
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# Default instances to launch
INSTANCES="${*:-DEV SBOX}"

echo "Weather Engine — Local Instance Launcher"
echo "Repository: $REPO_ROOT"
echo "Instances: $INSTANCES"
echo ""

# Ensure directories exist
mkdir -p data logs

# Check Python
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found"
    exit 1
fi

for INST in $INSTANCES; do
    INST_LOWER=$(echo "$INST" | tr '[:upper:]' '[:lower:]')
    PID_FILE="data/.${INST_LOWER}.pid"
    LOCK_FILE="data/.${INST_LOWER}.lock"
    
    # Check if already running
    if [ -f "$PID_FILE" ]; then
        OLD_PID=$(cat "$PID_FILE" 2>/dev/null || echo "")
        if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
            echo "  ⚠️ $INST: already running (PID $OLD_PID), skipping"
            continue
        fi
        rm -f "$PID_FILE"
    fi
    
    # Clear stale lock files
    rm -f "$LOCK_FILE"
    
    echo "  → Starting $INST..."
    
    # Set environment for this instance
    export PAPER_TRADING_INSTANCE="$INST"
    
    # Launch the cron wrapper in background
    # The wrapper handles: lock acquisition, health status, completion artifact, alert logging
    nohup python3 scripts/dev_paper_trading_cron.py \
        > "logs/paper_trading_${INST_LOWER}_stdout.log" 2>&1 &
    
    PID=$!
    echo "$PID" > "$PID_FILE"
    
    echo "    PID: $PID"
    echo "    Instance: $INST"
    echo "    Log: logs/paper_trading_${INST_LOWER}.log"
    echo "    Stdout: logs/paper_trading_${INST_LOWER}_stdout.log"
    echo "    Health: data/${INST_LOWER}_health.json"
    echo ""
done

echo "All instances launched."
echo ""
echo "To check status:"
echo "  python3 core/instance_config.py"
echo ""
echo "To stop:"
echo "  kill \$(cat data/.*.pid 2>/dev/null)"
echo ""
echo "To run a single instance manually:"
echo "  python3 scripts/multi_instance_paper_trader.py --instances DEV --write-completion-artifact"
