#!/usr/bin/env python3
"""Validate replay results for alert delivery testing."""
import os, json, sys

REPLAY_RESULT = os.environ.get("REPLAY_RESULT", "/tmp/alerts-delivery-replay.json")

with open(REPLAY_RESULT) as f:
    result = json.load(f)

errors = []

if result["ingested"] == 0:
    errors.append("No observations ingested")
obs_count = result["observations"]
ing_count = result["ingested"]
if obs_count > 0 and ing_count == 0:
    errors.append(f"{obs_count} observations but 0 ingested")

if errors:
    print("VALIDATION FAILED:")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)

print("PASS: All validations passed")
print(f"  Station: {result['station']}")
print(f"  Date: {result['date']}")
print(f"  Observations: {result['observations']}")
print(f"  Ingested: {result['ingested']}")
print(f"  Alerts generated: {result['alerts_generated']}")
print(f"  Queue pending: {result['pending_queue']}")
