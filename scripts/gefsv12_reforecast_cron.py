#!/usr/bin/env python3
"""gefsv12_reforecast_cron.py — bounded incremental driver for the GEFSv12
reforecast backfill (c00 control member first).

WHY A DRIVER
  A single `collect --start 2000-01-01 --end 2019-12-31` run is ~20 h wall and
  cannot fit a cron timeout. This driver advances the archive by a bounded
  number of SECONDS each invocation: it finds the EARLIEST missing init days and
  collects them in order until the wall budget is spent, then exits 0. Scheduled
  every 6 h, it converges on the full 2000-2019 c00 archive without any single
  run exceeding its timeout.

IDEMPOTENT / RESUMABLE
  The collector's primary key is (station, init_date, member, forecast_day) and
  it pre-skips present rows, so a killed or re-run invocation loses no work and
  never double-writes.

SCOPE
  c00 only (1/5 the AWS traffic of the full 5-member product). Per
  docs/plans/GEFSV12-REFORECAST-COLLECTOR-2026-10-13.md, p01..p04 are added only
  if the analog/debias refit later shows member spread matters.

READ-ONLY on the live book. Writes ONLY data/gefsv12_reforecast.db (+ /tmp cache).

Usage
  .venv-gefs/bin/python scripts/gefsv12_reforecast_cron.py            # 40 min budget
  .venv-gefs/bin/python scripts/gefsv12_reforecast_cron.py --max-seconds 60  # smoke
  .venv-gefs/bin/python scripts/gefsv12_reforecast_cron.py --status

Exit codes: 0 ok (progress made, or already complete) · 3 preflight/import failure.
"""
from __future__ import annotations

import argparse
import importlib.util
import sqlite3
import sys
import time
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
COLLECTOR = REPO / "scripts" / "gefsv12_reforecast_collector.py"

START = date(2000, 1, 1)
END = date(2019, 12, 31)
MEMBER = "c00"


def _load_collector():
    spec = importlib.util.spec_from_file_location("gefsv12_collector", COLLECTOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _present_days(c, member: str) -> set[str]:
    return {r[0] for r in c.execute(
        "SELECT DISTINCT init_date FROM gefsv12_reforecast WHERE member=?", (member,))}


def _all_days(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def cmd_status(c, member: str):
    present = _present_days(c, member)
    total = (END - START).days + 1
    print(f"member={member} present={len(present)}/{total} days "
          f"({100.0 * len(present) / total:.1f}%) window={START}..{END}")
    # earliest gaps
    gaps = [d for d in _all_days(START, END) if d.isoformat() not in present]
    print("next gaps:", ", ".join(g.isoformat() for g in gaps[:10]))


def main():
    ap = argparse.ArgumentParser(description="GEFSv12 reforecast backfill cron driver")
    ap.add_argument("--start", default=START.isoformat())
    ap.add_argument("--end", default=END.isoformat())
    ap.add_argument("--member", default=MEMBER)
    ap.add_argument("--max-seconds", type=int, default=2400,
                    help="wall budget per invocation (default 2400 = 40 min)")
    ap.add_argument("--days", type=int, default=10, help="forecast days per init")
    ap.add_argument("--status", action="store_true", help="print progress, no fetch")
    args = ap.parse_args()

    try:
        coll = _load_collector()
    except Exception as e:  # pragma: no cover - env failure
        print(f"FATAL: cannot load collector {COLLECTOR}: {e!r}", file=sys.stderr)
        return 3

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    c = coll.db()

    if args.status:
        cmd_status(c, args.member)
        return 0

    present = _present_days(c, args.member)
    gaps = [d for d in _all_days(start, end) if d.isoformat() not in present]
    if not gaps:
        print(f"COMPLETE: member={args.member} fully present for {start}..{end}")
        return 0

    t0 = time.monotonic()
    done = 0
    rows = 0
    for d in gaps:
        if time.monotonic() - t0 > args.max_seconds:
            break
        n, note = coll.collect_init(c, d, args.member, coll.STATIONS, args.days)
        rows += n
        done += 1
        print(f"  {d} {args.member}: +{n} rows ({note})", flush=True)
    c.commit()
    c.close()

    remaining = len(gaps) - done
    print(f"progress: +{done} init-days, +{rows} rows in "
          f"{time.monotonic() - t0:.0f}s; {remaining} days remaining")
    return 0


if __name__ == "__main__":
    sys.exit(main())
