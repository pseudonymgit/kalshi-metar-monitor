#!/usr/bin/env python3
"""Official NOAA GEFS full-panel pilot acquisition, one fixture at a time."""
from __future__ import annotations
import argparse
from pathlib import Path
import gefs_temperature_pilot as pilot

FULL_MEMBERS = ("gec00",) + tuple(f"gep{i:02d}" for i in range(1, 31))

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--fixture", required=True, choices=[x["id"] for x in pilot.FIXTURES])
    ap.add_argument("--retry-from", type=Path)
    args = ap.parse_args()
    fx = next(x for x in pilot.FIXTURES if x["id"] == args.fixture)
    old = pilot.MEMBERS
    pilot.MEMBERS = FULL_MEMBERS
    try:
        result = pilot.acquire(args.output, prior=args.retry_from, retry_version="gefs-temperature-pilot-full-panel-v1", fixtures=(fx,))
    finally:
        pilot.MEMBERS = old
    print({"requests": len(result["requests"]), "failed": len(result["failed"]), "fixture": args.fixture, "members": len(FULL_MEMBERS), "output": str(args.output)})
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
