#!/bin/bash
# graphify-snapshot.sh
#
# Archive graphify-out/ to a dated snapshot, excluding heavy artifacts.
# The key fix: we ONLY archive the contents of graphify-out/ (not the entire repo tree).
#
# Usage: ./scripts/graphify-snapshot.sh [--dry-run]
#
# Exclusions (belt-and-suspenders on top of scoping to graphify-out/ only):
#   - *.db files
#   - data/ directories
#   - graphify-out-snapshots/ (nested prior archives)
#   - graphify-snapshot* / graphify-snapshots* / .graphify-snapshots* (nested archives)
#   - .venv/, __pycache__, .git, .pytest_cache, node_modules/
#   - *.log
#
# Retention: keep last 10, remove oldest beyond that.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
GRAPHIFY_OUT="$REPO_DIR/graphify-out"
SNAPSHOT_DIR="$REPO_DIR/graphify-out-snapshots"
KEEP=10
DRY_RUN=false

if [ "${1:-}" = "--dry-run" ]; then
    DRY_RUN=true
    echo "[DRY RUN]"
fi

# --- Safety checks ---
if [ ! -d "$GRAPHIFY_OUT" ]; then
    echo "ERROR: graphify-out/ not found at $GRAPHIFY_OUT" >&2
    exit 1
fi

mkdir -p "$SNAPSHOT_DIR"

TIMESTAMP=$(date -u +%Y-%m-%dT%H%M%SZ)
SNAPSHOT_PATH="$SNAPSHOT_DIR/$TIMESTAMP"

echo ""
echo "Creating snapshot: $SNAPSHOT_PATH"
echo "Source: $GRAPHIFY_OUT"

# --- Build exclusion list for find ---
# We copy only items in graphify-out/, with explicit exclusions
# to prevent any recursive capture even if graphify-out content changes

EXCLUDE_PATTERNS=(
    -not -name 'data'
    -not -name 'graphify-out-snapshots'
    -not -name 'graphify-snapshot*'
    -not -name 'graphify-snapshots*'
    -not -name '.graphify-snapshots*'
    -not -name '.venv'
    -not -name 'node_modules'
    -not -name '.git'
    -not -name '__pycache__'
    -not -name '.pytest_cache'
)

if $DRY_RUN; then
    echo ""
    echo "Would copy these items from $GRAPHIFY_OUT/:"
    find "$GRAPHIFY_OUT" -maxdepth 1 -mindepth 1 "${EXCLUDE_PATTERNS[@]}" -print0 2>/dev/null | xargs -0 -I{} basename {} | sort
    echo ""
    echo "Excluded patterns: data/, *.db, graphify-out-snapshots/, graphify-snapshot*,"
    echo "                     .venv/, __pycache__/, .git/, .pytest_cache/, node_modules/"
else
    mkdir -p "$SNAPSHOT_PATH"

    # Copy items individually to avoid recursive capture
    find "$GRAPHIFY_OUT" -maxdepth 1 -mindepth 1 "${EXCLUDE_PATTERNS[@]}" -print0 2>/dev/null | while IFS= read -r -d '' item; do
        # Skip *.db files
        if [[ "$(basename "$item")" == *.db ]]; then
            continue
        fi
        cp -a "$item" "$SNAPSHOT_PATH/"
    done

    # --- Report snapshot size ---
    if [ -d "$SNAPSHOT_PATH" ]; then
        SNAPSHOT_SIZE=$(du -sh "$SNAPSHOT_PATH" 2>/dev/null | cut -f1)
        echo "Snapshot size: $SNAPSHOT_SIZE"

        # Double-check no *.db files leaked into snapshot
        DB_COUNT=$(find "$SNAPSHOT_PATH" -name '*.db' 2>/dev/null | wc -l)
        if [ "$DB_COUNT" -gt 0 ]; then
            echo "WARNING: $DB_COUNT .db files found in snapshot:"
            find "$SNAPSHOT_PATH" -name '*.db' 2>/dev/null
        fi
    fi
fi

# --- Retention: prune oldest beyond KEEP ---
EXCESS=$(ls -1t "$SNAPSHOT_DIR/" 2>/dev/null | tail -n +$((KEEP + 1)) || true)
if [ -n "$EXCESS" ]; then
    echo ""
    echo "Pruning old snapshots (keep=$KEEP):"
    while IFS= read -r old; do
        [ -z "$old" ] && continue
        if $DRY_RUN; then
            echo "  Would remove: $SNAPSHOT_DIR/$old"
        else
            echo "  Removing: $SNAPSHOT_DIR/$old"
            rm -rf "$SNAPSHOT_DIR/$old"
        fi
    done <<< "$EXCESS"
fi

# --- Report current snapshots ---
SNAPSHOT_COUNT=$(ls -1d "$SNAPSHOT_DIR"/*/ 2>/dev/null | wc -l || true)
echo ""
echo "Snapshots on disk ($SNAPSHOT_COUNT total):"
if [ "$SNAPSHOT_COUNT" -gt 0 ]; then
    du -sh "$SNAPSHOT_DIR"/*/ 2>/dev/null | sort -rh | head -15
fi

echo ""
echo "Done."