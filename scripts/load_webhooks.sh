#!/bin/bash
# Loads webhooks from .env.webhooks and exports with correct names

if [ -f .env.webhooks ]; then
    source .env.webhooks
    
    # Remap to the names the code actually expects
    export DISCORD_WEBHOOK_PROD="$WEBHOOK_PROD"
    export DISCORD_WEBHOOK_DEV="$WEBHOOK_DEV"
    export DISCORD_WEBHOOK_SBOX="$WEBHOOK_SBOX"
    
    echo "Webhooks loaded:"
    echo "  DISCORD_WEBHOOK_PROD: ${DISCORD_WEBHOOK_PROD:0:30}..."
    echo "  DISCORD_WEBHOOK_DEV:  ${DISCORD_WEBHOOK_DEV:0:30}..."
    echo "  DISCORD_WEBHOOK_SBOX: ${DISCORD_WEBHOOK_SBOX:0:30}..."
else
    echo "ERROR: .env.webhooks not found"
    exit 1
fi
