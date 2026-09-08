#!/usr/bin/env bash
# =============================================================================
# Install host-level daemon supervisor for Weather Engine intraday daemon
#
# Run this ON THE HOST (not inside the container). This creates a host cron
# entry that checks every minute whether the container's daemon is alive.
#
# Usage:
#   sudo bash scripts/install-daemon-supervisor.sh
#
# =============================================================================

set -euo pipefail

CONTAINER_NAME="openclaw-next-runtime-openclaw-gateway-1"
WATCHDOG_SCRIPT="/home/node/.openclaw/workspace/prototypes/weather-engine-source/scripts/daemon_watchdog.sh"
HEALTHCHECK_FILE="/tmp/intraday-healthcheck.json"
PID_FILE="/tmp/intraday-daemon.pid"

# ── Option A: systemd unit (better — survives reboots, has logging) ──────────

SYSTEMD_SERVICE="/etc/systemd/system/weather-engine-daemon-watchdog.service"
SYSTEMD_TIMER="/etc/systemd/system/weather-engine-daemon-watchdog.timer"

if [ "$(id -u)" -eq 0 ]; then
    echo "Installing systemd service + timer..."

    cat > "$SYSTEMD_SERVICE" << SERVICEEOF
[Unit]
Description=Weather Engine Intraday Daemon Watchdog
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
ExecStart=/usr/bin/docker exec ${CONTAINER_NAME} sh -lc 'if [ -f /tmp/intraday-daemon.pid ] && kill -0 $(cat /tmp/intraday-daemon.pid) 2>/dev/null; then exit 0; else cd /home/node/.openclaw/workspace/prototypes/weather-engine-source && nohup python3 scripts/intraday_daemon.py --interval-min 5 >> logs/intraday_daemon.log 2>&1 & echo $! > /tmp/intraday-daemon.pid; fi'
User=root
SERVICEEOF

    cat > "$SYSTEMD_TIMER" << 'TIMEREOF'
[Unit]
Description=Run WE daemon watchdog every 5 minutes

[Timer]
OnBootSec=1min
OnUnitActiveSec=5min
Persistent=false

[Install]
WantedBy=timers.target
TIMEREOF

    systemctl daemon-reload
    systemctl enable weather-engine-daemon-watchdog.timer
    systemctl start weather-engine-daemon-watchdog.timer
    echo "✅ systemd watchdog installed and started"
    echo "   Check status: systemctl status weather-engine-daemon-watchdog.timer"
    echo "   See logs: journalctl -u weather-engine-daemon-watchdog.service"

# ── Option B: host cron (simpler, no systemd dependency) ───────────────────
else
    echo "Not running as root. Installing host cron instead."
    echo ""

    CRON_LINE="* * * * * /usr/bin/docker exec ${CONTAINER_NAME} sh -c 'if [ -f ${PID_FILE} ] && kill -0 \$(cat ${PID_FILE}) 2>/dev/null; then exit 0; else cd /home/node/.openclaw/workspace/prototypes/weather-engine-source && nohup python3 scripts/intraday_daemon.py --interval-min 5 >> logs/intraday_daemon.log 2>&1 & echo \$! > ${PID_FILE}; fi'"

    # Check if already installed
    if crontab -l 2>/dev/null | grep -q "intraday_daemon"; then
        echo "Watchdog cron already exists. Updating..."
        (crontab -l 2>/dev/null | grep -v "intraday_daemon"; echo "$CRON_LINE") | crontab -
    else
        (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
    fi

    echo "✅ Host cron installed (every minute)"
    echo "   Check status: crontab -l | grep intraday_daemon"
    echo "   Logs from cron will go wherever your MAILTO is set"
fi

echo ""
echo "============================================================================="
echo "Daemon healthcheck: curl -s http://localhost:18889/.openclaw/health"
echo "Or inside container: docker exec ${CONTAINER_NAME} cat /tmp/intraday-healthcheck.json"
echo "============================================================================="