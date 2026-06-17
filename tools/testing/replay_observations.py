#!/usr/bin/env python3
"""Replay observations through the alert pipeline for testing."""
import os, sys, json

# Explicitly set project directory
PROJECT_DIR = "/home/node/.openclaw/workspace/prototypes/weather-engine-source"

# Add project root to path
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

# Set up environment from passed args
os.environ["ALERT_DB_PATH"] = os.environ.get("ALERT_DB_PATH", "/tmp/alerts-test.db")
os.environ["KALSHI_EXECUTION_DOMAIN"] = "replay"
os.environ["ALERT_WEBHOOK_URL"] = "https://example.com/test-webhook"

# Import after env is set
from core import metar_monitor

# Load observations file
OBS_FILE = os.environ.get("OBS_FILE", "/tmp/alerts-delivery-obs.json")
STATION = os.environ.get("STATION", "KDEN")
DATE = os.environ.get("DATE", "2026-06-16")

with open(OBS_FILE) as f:
    data = json.load(f)

events = data.get(STATION, {}).get("by_date", {}).get(DATE, [])
if not events:
    print(f"No events for {STATION} on {DATE}")
    sys.exit(1)

print(f"Replaying {len(events)} observations for {STATION} on {DATE}")

# Build observation list
obs_list = []
for e in sorted(events, key=lambda x: x["obs_time"]):
    obs_list.append({
        "obs_time": e["obs_time"],
        "temp_f": float(e["temp_f"]),
    })

# Run through ingestion pipeline
cfg = metar_monitor.get_default_config()
cfg["stations"] = [STATION]
cfg["poll_seconds"] = 999999

metar_monitor.ensure_state_loaded()

delivery_results = []
ingested, alerts = metar_monitor._ingest_obs(
    icao=STATION,
    new_obs=obs_list,
    cfg=cfg,
    allow_alert_delivery=True,
    persist_cache=True,
    delivery_results=delivery_results,
)

print(f"Ingested: {ingested}")
print(f"Alerts generated: {alerts}")

pending = metar_monitor._get_pending_deliveries()
print(f"Pending in retry queue: {len(pending)}")

# Write results
REPLAY_RESULT = os.environ.get("REPLAY_RESULT", "/tmp/alerts-delivery-replay.json")
result = {
    "station": STATION,
    "date": DATE,
    "observations": len(obs_list),
    "ingested": ingested,
    "alerts_generated": alerts,
    "pending_queue": len(pending),
}
with open(REPLAY_RESULT, "w") as f:
    json.dump(result, f, indent=2, default=str)

print(f"\nResults written to {REPLAY_RESULT}")
