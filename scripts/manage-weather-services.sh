#!/bin/bash
# Weather Collector Service Management Helper
# For DEV and SBOX (PROD runs on Render)
#
# Usage:
#   ./scripts/manage-weather-services.sh <command>
#
# Commands:
#   install     - One-time setup (copy files + enable on boot)
#   start       - Start both services
#   stop        - Stop both services
#   restart     - Restart both services
#   status      - Show status of both services
#   logs        - Follow logs for both services
#   logs-dev    - Follow logs for DEV only
#   logs-sbox   - Follow logs for SBOX only

set -e

SERVICE_DIR="/etc/systemd/system"
DEV_SERVICE="weather-collector-dev.service"
SBOX_SERVICE="weather-collector-sbox.service"

case "$1" in
    install)
        echo "=== Installing weather-collector services ==="
        sudo cp scripts/$DEV_SERVICE $SERVICE_DIR/
        sudo cp scripts/$SBOX_SERVICE $SERVICE_DIR/
        sudo systemctl daemon-reload
        sudo systemctl enable $DEV_SERVICE
        sudo systemctl enable $SBOX_SERVICE
        echo "Services installed and enabled for boot."
        echo "Run 'start' to start them now."
        ;;

    start)
        echo "Starting DEV and SBOX..."
        sudo systemctl start $DEV_SERVICE
        sudo systemctl start $SBOX_SERVICE
        sudo systemctl status $DEV_SERVICE --no-pager -l
        sudo systemctl status $SBOX_SERVICE --no-pager -l
        ;;

    stop)
        echo "Stopping DEV and SBOX..."
        sudo systemctl stop $DEV_SERVICE
        sudo systemctl stop $SBOX_SERVICE
        ;;

    restart)
        echo "Restarting DEV and SBOX..."
        sudo systemctl restart $DEV_SERVICE
        sudo systemctl restart $SBOX_SERVICE
        sudo systemctl status $DEV_SERVICE --no-pager -l
        sudo systemctl status $SBOX_SERVICE --no-pager -l
        ;;

    status)
        echo "=== DEV ==="
        sudo systemctl status $DEV_SERVICE --no-pager -l
        echo ""
        echo "=== SBOX ==="
        sudo systemctl status $SBOX_SERVICE --no-pager -l
        ;;

    logs)
        echo "Following logs for both services (Ctrl+C to exit)..."
        sudo journalctl -u $DEV_SERVICE -u $SBOX_SERVICE -f
        ;;

    logs-dev)
        sudo journalctl -u $DEV_SERVICE -f
        ;;

    logs-sbox)
        sudo journalctl -u $SBOX_SERVICE -f
        ;;

    *)
        echo "Usage: $0 {install|start|stop|restart|status|logs|logs-dev|logs-sbox}"
        exit 1
        ;;
esac
