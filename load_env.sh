#!/bin/bash

# Source webhook URLs from .env.webhooks
if [[ -f .env.webhooks ]]; then
  # Read the file and export variables in the expected format
  export DISCORD_WEBHOOK_PROD=$(grep '^WEBHOOK_PROD=' .env.webhooks | cut -d '=' -f2- | sed 's/^"\\|"$//g')
  export DISCORD_WEBHOOK_DEV=$(grep '^WEBHOOK_DEV=' .env.webhooks | cut -d '=' -f2- | sed 's/^"\\|"$//g')
  export DISCORD_WEBHOOK_SBOX=$(grep '^WEBHOOK_SBOX=' .env.webhooks | cut -d '=' -f2- | sed 's/^"\\|"$//g')
  
  echo "Environment variables loaded:"
  echo "  DISCORD_WEBHOOK_PROD: ${DISCORD_WEBHOOK_PROD:+SET}"  
  echo "  DISCORD_WEBHOOK_DEV: ${DISCORD_WEBHOOK_DEV:+SET}"
  echo "  DISCORD_WEBHOOK_SBOX: ${DISCORD_WEBHOOK_SBOX:+SET}"
else
  echo "ERROR: .env.webhooks not found"
fi