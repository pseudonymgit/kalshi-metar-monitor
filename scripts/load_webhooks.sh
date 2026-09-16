#!/bin/bash
# Loads webhooks from .env.webhooks — non-fatal if missing
if [ -f .env.webhooks ]; then
    source .env.webhooks
    export DISCORD_WEBHOOK_PROD="${WEBHOOK_PROD:-}"
    export DISCORD_WEBHOOK_DEV="${WEBHOOK_DEV:-}"
    export DISCORD_WEBHOOK_SBOX="${WEBHOOK_SBOX:-}"
    echo "Webhooks loaded from .env.webhooks"
else
    echo "WARNING: .env.webhooks not found — running without webhooks"
fi
