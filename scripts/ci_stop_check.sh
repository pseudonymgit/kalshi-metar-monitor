#!/usr/bin/env bash
# ci_stop_check.sh — CI/cron entry point for the governance off-switch (Workstream F).
#
# Exit codes (from scripts/stopping_record.py --check):
#   0 = proceed (state ACTIVE-BOUNDED, no A1-A4)
#   1 = stopped (A1-A4 or sunset / non-bounded)
#   2 = fail-closed (UNDETERMINED-STOP) -> treat as stopped
#
# Usage (from repo root):
#   scripts/ci_stop_check.sh
#   scripts/ci_stop_check.sh --json
#
# Wire into CI as a pre-run gate, e.g.:
#   - run: scripts/ci_stop_check.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

exec python3 scripts/stopping_record.py --check "$@"
