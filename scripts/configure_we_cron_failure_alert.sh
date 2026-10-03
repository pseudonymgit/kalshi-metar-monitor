#!/usr/bin/env bash
# =============================================================================
# Configure `failureAlert` on the surviving WE watchdog cron
#
# Context: DUAL-SUPERVISOR-CONSOLIDATION-2026-10-03.md §3.4 / deliverable #4.
# The container cron `WE-daemon-watchdog` currently has `delivery.mode=none`,
# so a restart-rate-limit STUCK state reaches only logs/daemon_watchdog.log
# (nobody reads it). This wires an explicit failureAlert route so a STUCK /
# repeated-failure state reaches a channel.
#
# Safety: DRY-RUN by default. Pass --apply to actually mutate the cron.
# This does NOT change the cron's ROLE (it stays the primary restart
# authority); it only attaches an alert route. Role demotion happens later,
# only after the host supervisor receipt (Dan) — see §6 item 5.
#
# Route values mirror the proven config landed 2026-10-02 for other WE crons
# (mode=announce, accountId=default, to=channel:<id>):
#   .meta/continuity/weather-engine/2026-10-02-remediation-sprint.md
# =============================================================================
set -euo pipefail

CRON_ID="5ac14c0d-d5f6-4d1b-8662-0c9f83d230a1"   # WE-daemon-watchdog
AFTER="2"                                          # consecutive errors before alert
COOLDOWN="1h"                                      # min time between alerts
MODE="announce"
CHANNEL="discord"                                  # channel plugin id
TO="channel:1526244467403919490"                   # resolves to the WE discord channel
ACCOUNT="default"

APPLY=0
for a in "$@"; do
    case "$a" in
        --apply) APPLY=1 ;;
        --dry-run) APPLY=0 ;;
        *) echo "unknown arg: $a" >&2; exit 2 ;;
    esac
done

CMD=(openclaw cron edit "$CRON_ID"
    --failure-alert
    --failure-alert-after "$AFTER"
    --failure-alert-cooldown "$COOLDOWN"
    --failure-alert-mode "$MODE"
    --failure-alert-channel "$CHANNEL"
    --failure-alert-to "$TO"
    --failure-alert-account-id "$ACCOUNT")

if [ "$APPLY" -eq 0 ]; then
    echo "[dry-run] would run:"
    printf '  %q' "${CMD[@]}"; echo
    echo "[dry-run] re-run with --apply to attach the failureAlert route."
    exit 0
fi

echo "[apply] attaching failureAlert to $CRON_ID ..."
"${CMD[@]}"
echo "[apply] done. Verify:"
echo "  openclaw cron get $CRON_ID | grep -iA6 failureAlert"
