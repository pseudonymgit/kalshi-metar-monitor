#!/bin/bash
# W1-C — Required preflight wrapper for WE crons.
#
# Runs scripts/preflight_startup.py (deps + venv + DB integrity) BEFORE the
# target command. If preflight fails, the target does NOT run and the wrapper
# exits non-zero — a broken environment can never silently produce a zero-output
# "green" run.
#
# Usage (from repo root):
#   bash scripts/we_cron_preflight.sh <command...>
#   bash scripts/we_cron_preflight.sh python3 scripts/nwp_collect.py
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1

PY=".venv-gefs/bin/python"
[ -x "$PY" ] || PY="python3"

if ! "$PY" scripts/preflight_startup.py; then
  echo "WE-PREFLIGHT: BLOCKED — target command not run" >&2
  exit 2
fi

echo "WE-PREFLIGHT: OK — running: $*"
exec "$@"