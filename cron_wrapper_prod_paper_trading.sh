#!/bin/bash
# Wrapper for PROD Paper Trading Cron with Proper Environment Variables
cd /home/node/.openclaw/workspace/prototypes/weather-engine-source
source scripts/load_webhooks.sh
python3 scripts/prod_paper_trading_cron.py