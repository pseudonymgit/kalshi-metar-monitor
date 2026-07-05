#!/bin/bash
# Rolling merge snapshot — creates git tag + filesystem backup, keeps last 10
set -e

REPO_DIR="/home/node/.openclaw/workspace/prototypes/weather-engine-source"
BACKUP_BASE="/home/node/.openclaw/workspace/prototypes"
TIMESTAMP=$(date +%Y%m%d-%H%M)
TAG="deploy-${TIMESTAMP}"
BACKUP_DIR="${BACKUP_BASE}/weather-engine-source-backup-${TIMESTAMP}"

cd "$REPO_DIR"

echo "=== Creating merge snapshot: $TIMESTAMP ==="

# Git tag
git tag "$TAG" -m "Merge snapshot $TIMESTAMP"
echo "Tagged: $TAG"

# Filesystem backup (exclude __pycache__, db files, .git)
mkdir -p "$BACKUP_DIR"
cp -a "$REPO_DIR"/* "$REPO_DIR"/.[!.]* "$REPO_DIR"/..?* "$BACKUP_DIR/" 2>/dev/null || true
rm -rf "$BACKUP_DIR/__pycache__" "$BACKUP_DIR/*/__pycache__" 2>/dev/null || true
find "$BACKUP_DIR" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
find "$BACKUP_DIR" -name '*.db' -type f -delete 2>/dev/null || true
echo "Backup: $BACKUP_DIR"

# Prune to last 10 filesystem backups
BACKUPS=$(ls -1dt ${BACKUP_BASE}/weather-engine-source-backup-* 2>/dev/null)
COUNT=$(echo "$BACKUPS" | wc -l)
if [ "$COUNT" -gt 10 ]; then
  TO_DELETE=$(echo "$BACKUPS" | tail -n +11)
  echo "=== Pruning old backups (keeping 10) ==="
  for dir in $TO_DELETE; do
    echo "Removing: $dir"
    rm -rf "$dir"
  done
fi

# Prune tags beyond 10
TAG_COUNT=$(git tag -l 'deploy-*' | wc -l)
if [ "$TAG_COUNT" -gt 10 ]; then
  OLD_TAGS=$(git tag -l 'deploy-*' | sort | head -n -10)
  for t in $OLD_TAGS; do
    git tag -d "$t" 2>/dev/null || true
    echo "Removed tag: $t"
  done
fi

echo "=== Snapshot complete. $(ls -1dt ${BACKUP_BASE}/weather-engine-source-backup-* 2>/dev/null | wc -l) backups retained. ==="
