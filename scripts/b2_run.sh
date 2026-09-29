#!/bin/bash
# B2 sweep runner with per-signal timeout guard (R/T 2026-09-29).
# ROOT CAUSE of prior "hang": ensemble signals cost ~6.5s/CONFIG with no timeout,
# so 3 ensemble variants ~= 19h. Guard caps each signal at SWEEP_SIGNAL_BUDGET_SECONDS.
set -u
cd /home/node/.openclaw/workspace/prototypes/weather-engine-source
export SWEEP_SIGNAL_BUDGET_SECONDS="${SWEEP_SIGNAL_BUDGET_SECONDS:-1800}"
export SWEEP_CONFIG_TIMEOUT_SECONDS="${SWEEP_CONFIG_TIMEOUT_SECONDS:-30}"
LOG="data/logs/b2_run_$(date +%H%M%S).log"
echo "=== B2 GUARDED RUN $(date -Iseconds) budget=${SWEEP_SIGNAL_BUDGET_SECONDS}s config_timeout=${SWEEP_CONFIG_TIMEOUT_SECONDS}s ===" | tee "$LOG"
.venv-gefs/bin/python -u scripts/big_sweep.py --phase 1 2>&1 | tee -a "$LOG"
echo "=== B2 DONE $(date -Iseconds) ===" | tee -a "$LOG"
