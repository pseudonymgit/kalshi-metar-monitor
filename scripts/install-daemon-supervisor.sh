#!/usr/bin/env bash
# =============================================================================
# Install host-level daemon supervisor for Weather Engine intraday daemon
#
# Run this ON THE HOST (not inside the container). This creates a host
# systemd timer (or host cron) that checks the container's daemon every minute.
#
# IMPORTANT — this installer DELEGATES restart to the container's own
# scripts/daemon_watchdog.sh, which is the single source of truth for:
#   - the correct interpreter (.venv-gefs/bin/python — has numpy+scipy)
#   - correct PID handling (the daemon writes its own PID; NO `$!` pre-write)
#   - the restart rate-limiter (6 restarts / 3600s -> STUCK, exit 1)
#
# Reimplementing the restart inline is what made the previous installer a
# no-op: it used bare `python3` (no numpy/scipy -> silent start failure) and
# pre-wrote `$!` into the PID file, so the daemon's own _check_pid() read it
# back as "already running" and self-exited. Do NOT inline a restart here.
# See docs/plans/DUAL-SUPERVISOR-CONSOLIDATION-2026-10-03.md.
#
# Usage:
#   sudo bash scripts/install-daemon-supervisor.sh
#
# =============================================================================

set -euo pipefail

CONTAINER_NAME="openclaw-next-runtime-openclaw-gateway-1"
CONTAINER_WORK_DIR="/home/node/.openclaw/workspace/prototypes/weather-engine-source"
WATCHDOG_REL="scripts/daemon_watchdog.sh"

# The one canonical restart action, run INSIDE the container by delegating to
# the watchdog script (so venv, PID handling, and rate-limit are inherited).
CONTAINER_CMD="cd ${CONTAINER_WORK_DIR} && bash ${WATCHDOG_REL}"

SYSTEMD_SERVICE="/etc/systemd/system/weather-engine-daemon-watchdog.service"
SYSTEMD_TIMER="/etc/systemd/system/weather-engine-daemon-watchdog.timer"

# ── Option A: systemd unit (better — survives reboots, has logging) ──────────

if [ "$(id -u)" -eq 0 ]; then
    echo "Installing systemd service + timer..."

    cat > "$SYSTEMD_SERVICE" << SERVICEEOF
[Unit]
Description=Weather Engine Intraday Daemon Watchdog
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
# Delegate to the container's daemon_watchdog.sh (correct venv, no PID
# pre-write, rate-limited). Do NOT inline a restart command here.
ExecStart=/usr/bin/docker exec ${CONTAINER_NAME} sh -lc '${CONTAINER_CMD}'
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

    CRON_LINE="* * * * * /usr/bin/docker exec ${CONTAINER_NAME} sh -lc '${CONTAINER_CMD}'"

    # Check if already installed
    if crontab -l 2>/dev/null | grep -q "intraday_daemon\|daemon_watchdog"; then
        echo "Watchdog cron already exists. Updating..."
        (crontab -l 2>/dev/null | grep -v "intraday_daemon\|daemon_watchdog"; echo "$CRON_LINE") | crontab -
    else
        (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
    fi

    echo "✅ Host cron installed (every minute)"
    echo "   Check status: crontab -l | grep daemon_watchdog"
    echo "   Logs from cron will go wherever your MAILTO is set."
fi

echo ""
echo "============================================================================="
echo "Daemon healthcheck: curl -s http://localhost:18889/.openclaw/health"
echo "Or inside container: docker exec ${CONTAINER_NAME} cat /tmp/intraday-healthcheck.json"
echo "============================================================================="
