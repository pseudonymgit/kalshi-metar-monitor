#!/usr/bin/env bash
# we_freshness_cron.sh — cron wrapper for the Weather Engine freshness monitor.
#
# Ownership: Ray Donovan (data/runtime lane). The monitor itself
# (scripts/data_freshness_monitor.py) is deterministic and correct; this wrapper
# only fixes how the cron harness *interprets* it.
#
# Why the wrapper exists:
#   The monitor signals STALE sources by exiting 1. A cron command job treats any
#   non-zero exit as a *command failure*, so "stale data detected" was recorded as
#   a broken job (status: error, hourly failure alerts) — indistinguishable from
#   the monitor itself crashing. That produced permanent error noise.
#
# Contract:
#   healthy            -> print nothing actionable (NO_REPLY), exit 0
#   stale, unchanged   -> NO_REPLY, exit 0   (already reported; no hourly spam)
#   stale, changed     -> print the report, exit 0   (announce delivers it once)
#   monitor broken     -> print the error, exit 2    (real failure -> failureAlert)
#
# Exit 0 is returned for the stale cases on purpose: stale data is a *reportable
# signal*, not a job malfunction. Only a genuine monitor failure returns non-zero.
set -uo pipefail

REPO="/home/node/.openclaw/workspace/prototypes/weather-engine-source"
MONITOR="scripts/data_freshness_monitor.py"
SIG_FILE="$REPO/.meta/freshness_last_sig"   # last reported stale signature

cd "$REPO" || { echo "freshness: repo not found at $REPO"; exit 2; }

report="$(python3 "$MONITOR" 2>&1)"
rc=$?

if [ "$rc" -ge 2 ]; then
  # Misconfiguration / crash — surface as a real failure.
  echo "freshness monitor failed (exit $rc):"
  printf '%s\n' "$report"
  exit 2
fi

if [ "$rc" -eq 0 ]; then
  # All fresh. Clear any stored signature so the next staleness reports again.
  rm -f "$SIG_FILE"
  echo "NO_REPLY"
  exit 0
fi

# rc == 1: at least one source stale. Build a compact signature of *what* is
# stale so an unchanged condition is reported once, not every hour.
sig="$(printf '%s\n' "$report" \
  | grep -E '^\S+ +(STALE|MISSING|EMPTY|ERROR|UNPARSEABLE) ' \
  | awk '{print $1"="$2}' | sort | tr '\n' ',')"
# Include the per-station count so a worsening condition re-alerts.
sig="${sig}stations=$(printf '%s\n' "$report" | grep -c 'h stale, thresh' || true)"

last=""
[ -f "$SIG_FILE" ] && last="$(cat "$SIG_FILE" 2>/dev/null || true)"

if [ "$sig" = "$last" ]; then
  echo "NO_REPLY"
  exit 0
fi

mkdir -p "$(dirname "$SIG_FILE")"
printf '%s' "$sig" > "$SIG_FILE"
printf '%s\n' "$report"
exit 0
