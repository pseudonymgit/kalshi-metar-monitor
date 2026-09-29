#!/bin/bash
# Agent Run-Failure Alert (R/T, 2026-09-28)
#
# WHY: a subagent that hard-fails (stopReason: error, 0 tokens) emits NO
# completion handoff, so the parent just sees "no reply" and the failure is
# invisible (this is how 5+ Gilfoyle dispatches looked silent). This watcher
# scans recent agent sessions for failed/timeout runs and surfaces them loudly.
#
# Run every 10 min via cron. Silence-first: only prints when a NEW failure is
# seen (state file remembers what was already alerted).
#
# Usage: bash scripts/agent_run_failure_alert.sh

set -uo pipefail

STATE_FILE="/tmp/agent-run-failures.seen"
WINDOW_MIN="${WINDOW_MIN:-30}"
NOW=$(date +%s)
CUTOFF_MS=$(( (NOW - WINDOW_MIN*60) * 1000 ))

AGENTS="gilfoyle gerri-kellman marty-byrde peggy-olson stringer-bell mike-ehrmantraut saul-goodman ray-donovan miles-obrien"

touch "$STATE_FILE"
new_failures=""

for agent in $AGENTS; do
    json=$(timeout 30 openclaw sessions list --agent "$agent" --json 2>/dev/null) || continue
    [ -z "$json" ] && continue

    # Extract "status|updatedAt|key" for runs that failed/timed out or aborted.
    rows=$(printf '%s' "$json" | python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: sys.exit(0)
s=d if isinstance(d,list) else d.get('sessions',[])
for x in s:
    st=x.get('status'); ab=x.get('abortedLastRun'); upd=x.get('updatedAt') or 0
    if st in ('failed','timeout','error','aborted') or ab is True:
        print(f\"{st or 'aborted'}|{upd}|{x.get('key','')}\")
" 2>/dev/null)

    while IFS='|' read -r st upd key; do
        [ -z "$key" ] && continue
        # only recent runs
        if [ "${upd:-0}" -lt "$CUTOFF_MS" ] 2>/dev/null; then continue; fi
        sig="${key}:${upd}"
        if ! grep -qF "$sig" "$STATE_FILE"; then
            echo "$sig" >> "$STATE_FILE"
            new_failures="${new_failures}${agent} :: ${st} :: ${key} (updated $(date -u -d @$((upd/1000)) '+%H:%MZ' 2>/dev/null || echo '?'))\n"
        fi
    done <<< "$rows"
done

# keep the state file bounded
tail -500 "$STATE_FILE" > "${STATE_FILE}.tmp" 2>/dev/null && mv "${STATE_FILE}.tmp" "$STATE_FILE"

if [ -n "$new_failures" ]; then
    echo "⚠️ AGENT RUN FAILURES (last ${WINDOW_MIN}m):"
    printf "%b" "$new_failures"
    echo ""
    echo "Action: check the model/fallback chain — a hard-failed run emits no completion, so this is the only signal."
    exit 1
fi

# silence-first: nothing to say
exit 0
