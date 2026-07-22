#!/bin/bash
# Wrapper for PROD Paper Trading Cron with Proper Environment Variables
cd /home/node/.openclaw/workspace/prototypes/weather-engine-source
source scripts/load_webhooks.sh

# Enable Discord alerts for production
export DISCORD_ENABLED_PROD=true

# Also make sure other environment variables are set appropriately
export PAPER_TRADING_INSTANCE=PROD

python3 scripts/prod_paper_trading_cron.py