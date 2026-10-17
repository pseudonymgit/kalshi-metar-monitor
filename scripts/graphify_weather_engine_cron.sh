#!/bin/bash
# graphify_weather_engine_cron.sh
#
# Deterministic cron payload for the weather-engine knowledge-graph refresh.
# No LLM / no agentTurn. Two steps, in order:
#   1. archive current graphify-out/ -> dated snapshot (keep last 10)
#   2. regenerate the graph: graphify extract . --code-only --no-cluster (AST-only)
#
# Exits non-zero if either step fails so the cron run reports `error`.
#
# Usage: bash scripts/graphify_weather_engine_cron.sh [--skip-archive]

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

echo "[graphify-cron] repo: $REPO_DIR"
echo "[graphify-cron] start: $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# --- Resolve the graphify CLI (PATH first, then the known user-local install) ---
GRAPHIFY_BIN="$(command -v graphify || true)"
if [ -z "$GRAPHIFY_BIN" ] && [ -x "$HOME/.local/bin/graphify" ]; then
    GRAPHIFY_BIN="$HOME/.local/bin/graphify"
fi
if [ -z "$GRAPHIFY_BIN" ]; then
    echo "[graphify-cron] ERROR: graphify CLI not found on PATH or ~/.local/bin" >&2
    exit 1
fi
echo "[graphify-cron] graphify: $GRAPHIFY_BIN ($("$GRAPHIFY_BIN" --version 2>/dev/null || echo 'version unknown'))"

# --- Step 1: archive current graphify-out/ (own script keeps last 10) ---
if [ "${1:-}" != "--skip-archive" ]; then
    echo "[graphify-cron] step 1: archiving graphify-out/ -> graphify-out-snapshots/"
    bash "$REPO_DIR/scripts/graphify-snapshot.sh"
else
    echo "[graphify-cron] step 1: SKIPPED (--skip-archive)"
fi

# --- Step 2: regenerate the graph (AST-only, deterministic, no API key) ---
echo "[graphify-cron] step 2: graphify extract . --code-only --no-cluster"
"$GRAPHIFY_BIN" extract . --code-only --no-cluster

# --- Verify the artifact actually landed ---
if [ ! -s "$REPO_DIR/graphify-out/graph.json" ]; then
    echo "[graphify-cron] ERROR: graphify-out/graph.json missing or empty after extract" >&2
    exit 1
fi

echo "[graphify-cron] graph.json: $(du -h "$REPO_DIR/graphify-out/graph.json" | cut -f1)"
echo "[graphify-cron] done: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
