├── logs/
│   ├── paper_trading_dev.log
│   ├── paper_trading_sbox.log
│   ├── paper_trading_prod.log
│   ├── alerts_dev.jsonl
│   ├── alerts_sbox.jsonl
│   └── alerts_prod.jsonl
├── docs/
│   ├── DEPLOYMENT.md           # This file
│   ├── ALERT-SCHEMA-V1.0.md    # Frozen alert schema
│   └── PROMOTION-RULES.md       # Promotion checklist
└── ROADMAP.md

## Render Data Collector Setup

The central data collection runs on the Render server to ensure 24/7 availability:

```
# Start the background collection service on Render (auto-starts with app)
# The service runs continuously, collecting:
# - METAR: every 30 minutes 
# - NWP: daily at 06:00 UTC
# - Kalshi: every 15 minutes
python3 scripts/collect_all.py
```

**Critical Note:** The Render service acts as the shared cache provider for METAR/NWP/Kalshi data.
DEV and SBOX instances depend on this shared data source to function correctly when the laptop 
is offline. Without the Render collector, local instances will not have fresh weather data.

The collector writes to shared data files that all instances read from:
- `data/metar_backfill.db` — available to PROD/DEV/SBOX
- `data/nwp_forecasts.db` — available to PROD/DEV/SBOX
- Kalshi API access is available to all instances via authenticated calls from Render