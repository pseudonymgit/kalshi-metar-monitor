#!/usr/bin/env python3
"""Deterministic B2 real-price sweep.

This script is deliberately fail-closed. It reads only persisted calibrated
probabilities and real z8 bid/ask/mid snapshots; it never creates prices or
calls a model. It refuses to produce a performance report before 72 hours of
snapshot history and a valid calibration input are present.
"""

import argparse
import csv
import datetime as dt
import json
import sqlite3
from pathlib import Path


def inspect_inputs(snapshot_db: str, calibrated_csv: str) -> dict:
    with sqlite3.connect(f"file:{snapshot_db}?mode=ro", uri=True) as conn:
        timestamps = [r[0] for r in conn.execute("SELECT DISTINCT snapshot_time FROM market_snapshots ORDER BY snapshot_time")]
        rows = conn.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0]
    hours = {ts[:13] for ts in timestamps}
    calibrated = Path(calibrated_csv)
    calibration_rows = 0
    if calibrated.exists():
        with calibrated.open(newline="") as handle:
            calibration_rows = sum(1 for _ in csv.DictReader(handle))
    return {
        "snapshot_rows": rows,
        "distinct_snapshots": len(timestamps),
        "distinct_hours": len(hours),
        "calibration_file": str(calibrated),
        "calibration_rows": calibration_rows,
        "eligible": len(hours) >= 72 and calibration_rows > 0,
    }


def run(snapshot_db: str, calibrated_csv: str, output: str) -> dict:
    inputs = inspect_inputs(snapshot_db, calibrated_csv)
    receipt = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "mode": "real_price_only_no_ai",
        "inputs": inputs,
        "status": "eligible" if inputs["eligible"] else "gated",
        "reason": None if inputs["eligible"] else "requires >=72 distinct snapshot hours and calibrated probability rows",
    }
    Path(output).write_text(json.dumps(receipt, indent=2) + "\n")
    if not inputs["eligible"]:
        raise SystemExit(2)
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot-db", required=True)
    parser.add_argument("--calibrated-csv", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.snapshot_db, args.calibrated_csv, args.output), indent=2))
