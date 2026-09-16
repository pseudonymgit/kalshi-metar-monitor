#!/usr/bin/env python3
"""Deterministic post-run semantic checks for the weather-engine cron manifest.

This is monitor-only: it reads the dependency manifest and a snapshot SQLite DB,
never mutating scheduler state or the database.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.z8_kalshi_market_cron import EXPECTED_STATIONS, validate_snapshot_receipt

DEFAULT_MANIFEST = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "..", ".meta", "continuity", "weather-engine",
    "2026-09-10-closure-sprint", "cron", "cron-dependency-manifest.json",
)
DEFAULT_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "kalshi_market_snapshots.db")


def check_manifest(manifest_path: str, *, worktree: str | None = None) -> dict[str, Any]:
    """Check dependency existence and reject duplicate z8 manifest entries."""
    with open(manifest_path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    jobs = manifest.get("jobs")
    if not isinstance(jobs, list):
        return {"ok": False, "manifest_exists": True, "job_count": 0,
                "duplicate_z8_entries": 0, "missing_jobs": [],
                "errors": ["manifest jobs is not a list"]}
    root = worktree or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    z8 = [job for job in jobs if isinstance(job, dict) and
          job.get("script") == "scripts/z8_kalshi_market_cron.py"]
    missing = [job.get("script") for job in jobs
               if isinstance(job, dict) and not os.path.exists(os.path.join(root, job.get("script", "")))]
    errors = []
    if len(z8) != 1:
        errors.append(f"expected exactly one z8 manifest entry, found {len(z8)}")
    if missing:
        errors.append("missing dependency scripts: " + ", ".join(sorted(set(missing))))
    return {"ok": not errors, "manifest_exists": True, "job_count": len(jobs),
            "duplicate_z8_entries": max(0, len(z8) - 1), "z8_entries": len(z8),
            "missing_jobs": sorted(set(missing)), "errors": errors}


def check_post_run(manifest_path: str, db_path: str, *, now: datetime | None = None,
                   max_age_seconds: int = 3900, worktree: str | None = None) -> dict[str, Any]:
    """Return a deterministic, fail-closed semantic result for one post-run artifact."""
    manifest_result = check_manifest(manifest_path, worktree=worktree)
    result: dict[str, Any] = {"checked_at_utc": (now or datetime.now(timezone.utc)).isoformat(),
                              "manifest": manifest_result, "artifact": None, "ok": False}
    if not os.path.exists(db_path):
        result["artifact"] = {"ok": False, "missing": True, "db_path": db_path,
                              "errors": ["snapshot artifact missing"]}
        return result
    try:
        receipt = validate_snapshot_receipt(db_path, max_age_seconds=max_age_seconds, now=now)
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        result["artifact"] = {"ok": False, "missing": False, "errors": [str(exc)]}
        return result
    result["artifact"] = receipt
    result["ok"] = bool(manifest_result["ok"] and receipt["ok"])
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--max-age-seconds", type=int, default=3900)
    args = parser.parse_args()
    result = check_post_run(args.manifest, args.db, max_age_seconds=args.max_age_seconds)
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
