#!/bin/bash
# Minimal Git-Driven Deployment Script
# Follows the principle: branch → merge to main → Render auto-deploy

set -e  # Exit on any error

echo "🔬 Verifying current branch state..."
git fetch origin
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
echo "Current branch: $CURRENT_BRANCH"

echo "📋 Checking for uncommitted changes..."
UNCOMMITTED_CHANGES=$(git status --porcelain)
if [[ -n "$UNCOMMITTED_CHANGES" ]]; then
    echo "⚠️  Warning: Uncommitted changes detected:"
    echo "$UNCOMMITTED_CHANGES"
    echo "Please commit your changes before proceeding with deployment."
    exit 1
else
    echo "✅ No uncommitted changes"
fi

echo "📊 Current commit info: $(git log -1 --format="%h - %s")"

echo "🔄 Setting up tracking for remote branch if needed..."
# Attempt to set up the tracking branch if not exists
if ! git rev-parse --verify origin/$CURRENT_BRANCH > /dev/null 2>&1; then
    echo "💡 Branch '$CURRENT_BRANCH' not on origin, will push first"
    git push -u origin "$CURRENT_BRANCH"
fi

# Now sync with the remote branch
git pull origin "$CURRENT_BRANCH" || echo "(already up to date)"

echo "🔗 Fetching and merging latest main..."
git fetch origin
git checkout main
git pull origin main

echo "🔗 Merging current branch ($CURRENT_BRANCH) into main..."
git merge "$CURRENT_BRANCH" --no-ff -m "Deploy: Merge $CURRENT_BRANCH to main for 9-signal ensemble release"

echo "📤 Pushing main branch to trigger Render auto-deploy..."
git push origin main

echo "✅ Deployment pushed to main - Render auto-deploy triggered"
echo "✨ 9-signal ensemble deployed via git-driven automation"

# Show the deployment commit
DEPLOY_COMMIT=$(git log -1 --format="%h - %an, %ar (%s)")
echo "📦 Deploy commit: $DEPLOY_COMMIT"

echo "🏁 Deployment process completed successfully!"