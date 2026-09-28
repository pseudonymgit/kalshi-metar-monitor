#!/usr/bin/env python3
"""
GS-C2 — Fail-fast startup preflight: deps + DB integrity + venv.

Run before any WE daemon/cron starts. Exits non-zero on the first fatal issue so
a broken environment cannot silently produce zero-output runs.

Usage: .venv-gefs/bin/python scripts/preflight_startup.py
"""
import importlib
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# WAVE-C/12 (2026-09-28): expanded so every WE entrypoint's deps are covered.
# Previously only numpy/scipy/requests were checked, so an entrypoint needing
# cdsapi/eccodes/xarray/cfgrib/pandas could pass preflight and then crash at run.
REQUIRED_MODULES = [
    "numpy", "scipy", "requests", "pandas",
    "xarray", "cfgrib", "cdsapi", "cryptography",
]
# eccodes is the GRIB C-library binding; fail soft if absent (some hosts lack it).
OPTIONAL_MODULES = ["eccodes"]
REQUIRED_DBS = ["metar_backfill.db", "gefs_archive.db", "nwp_forecasts.db", "kalshi_settlements.db"]


def main():
    failures = []

    # 1. venv sanity — must be running from a venv python
    if sys.prefix == sys.base_prefix:
        failures.append("not running inside a virtualenv (bare python3 lacks deps)")

    # 2. deps
    for m in REQUIRED_MODULES:
        try:
            importlib.import_module(m)
        except ImportError as e:
            failures.append(f"missing module: {m} ({e})")
    for m in OPTIONAL_MODULES:
        try:
            importlib.import_module(m)
        except ImportError:
            pass  # optional; absence is not fatal

    # 3. DB presence + readability + non-empty core tables
    checks = {
        "metar_backfill.db": "metar_observations",
        "gefs_archive.db": "gefs_archive",
        "nwp_forecasts.db": "nwp_forecasts",
        "kalshi_settlements.db": "kalshi_settlements",
    }
    for db, table in checks.items():
        p = ROOT / "data" / db
        if not p.exists():
            failures.append(f"missing DB: {db}")
            continue
        try:
            c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            n = c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            c.close()
            if n == 0:
                failures.append(f"empty table: {db}:{table}")
        except Exception as e:
            failures.append(f"unreadable DB {db}: {e}")

    if failures:
        print("PREFLIGHT FAILED:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print(f"PREFLIGHT OK — venv={sys.prefix}, {len(REQUIRED_MODULES)} deps, {len(checks)} DBs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
