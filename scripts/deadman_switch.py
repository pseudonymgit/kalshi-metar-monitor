#!/usr/bin/env python3
"""
TRACK 3 / P3-C — Dead-man switch.

The system quietly stopped trading for ~1 week and nothing alerted. This checks
that key producers/daemons have run recently; if a critical artifact is stale, it
exits NON-ZERO with a loud message so cron/announce surfaces it.

Preflight-style: presence + freshness of last-run timestamps, not just existence.

Run: .venv-gefs/bin/python scripts/deadman_switch.py
Exit 0 = all fresh; 1 = one or more stale/missing.
"""
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# R/T (2026-09-29): extended to ALL data pipelines per Dan. Two kinds of check:
#   (a) file-mtime checks for artifacts (cheap, catches producer death)
#   (b) CONTENT-freshness checks for DBs — MAX(timestamp column) inside the DB,
#       NOT the file mtime. This is the bug the old switch had: ghcn_settlements.db
#       had a fresh mtime (a write touched it) but its newest DATA was 5 days
#       stale, so file-mtime checks stayed green. Content checks catch that.
#
# (label, db_file, table, time_column, max_age_hours, kind)
#   kind=filedb  -> age from file mtime (no reliable content ts)
#   kind=content -> age from MAX(time_column) parsed as an event timestamp
#   kind=content_date -> age from MAX(time_column) parsed as YYYY-MM-DD (end-of-day)
CHECKS = [
    # ── file-mtime artifacts ──
    ("ws_p_distribution.json", "data/ws_p_distribution.json", None, None, 24 * 14, "file"),
    # ── METAR (30-min cron) ──
    ("METAR", "data/metar_backfill.db", "metar_observations", "timestamp_utc", 3, "content"),
    # ── GHCN daily backfill (cron 06:30 UTC) ──
    # R/T (2026-09-29): NOAA GHCN-Daily publishes with a ~2-day lag, so the
    # newest date is legitimately today-2. A 36h threshold fired on a healthy
    # source. 72h allows the publication lag + one missed daily cycle.
    ("GHCN", "data/ghcn_settlements.db", "ghcn_settlements", "date", 72, "content_date"),
    # ── GEFS archive (daily) ──
    ("GEFS", "data/gefs_archive.db", "gefs_archive", "target_date", 30, "content_date"),
    # ── TIGGE archive (daily) ──
    # R/T (2026-09-29): TIGGE publishes with a ~2-day embargo, so the newest
    # target_date is legitimately today-2. A 36h threshold fired on a healthy
    # source. 72h allows the embargo + one missed daily cycle before alerting.
    ("TIGGE", "data/tigge_archive.db", "tigge_archive", "target_date", 72, "content_date"),
    # ── Kalshi market snapshots (30-min/hourly) ──
    ("Kalshi-snapshots", "data/kalshi_market_snapshots.db", "market_snapshots", "snapshot_time", 3, "content"),
    # ── Kalshi settlements (daily; finalization lag expected) ──
    # R/T (2026-09-29): this checked metar_backfill.settlement_epochs, which
    # lagged at 09-25 while the ACTUAL settlements (kalshi_settlements.db) were
    # current to 09-28 — a wrong-table bug that fired a false STALE. Point at
    # the real writer's table.
    ("Kalshi-settlements", "data/kalshi_settlements.db", "kalshi_settlements", "target_date", 48, "content_date"),
    # ── IEM ASOS 1-min (hourly) ──
    ("IEM-ASOS", "data/iem_asos_1min.db", "observations", "valid", 4, "content"),
    # ── NWP forecasts (6h cron) ──
    ("NWP", "data/nwp_forecasts.db", "nwp_forecasts", "fetch_timestamp", 12, "content"),
    # ── ERA5 reanalysis (daily) ──
    ("ERA5", "data/era5_archive.db", "era5_archive", "target_date", 50, "content_date"),
    # ── WeatherAPI archive (daily) ──
    ("WeatherAPI", "data/weatherapi_archive.db", "daily", "date", 30, "content_date"),
]


def _age_hours(p: Path) -> float:
    return (time.time() - p.stat().st_mtime) / 3600.0


def _parse_ts(raw):
    """Parse a DB time value into an aware UTC datetime, or None."""
    import datetime as _dt
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # ISO with optional Z
    try:
        ts = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_dt.timezone.utc)
        return ts
    except Exception:
        pass
    # Date-only / space-separated
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            ts = _dt.datetime.strptime(s[: len(fmt) + 2].strip(), fmt)
            return ts.replace(tzinfo=_dt.timezone.utc)
        except Exception:
            continue
    return None


def _content_age_hours(db_file: Path, table: str, col: str, date_only: bool):
    """Age (hours) of the newest DATA in the DB. None if unreadable."""
    import datetime as _dt
    try:
        c = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
        raw = c.execute(f"SELECT MAX({col}) FROM {table}").fetchone()[0]
        c.close()
    except Exception as e:
        return ("ERR", str(e))
    ts = _parse_ts(raw)
    if ts is None:
        return ("ERR", f"unparseable: {raw!r}")
    now = _dt.datetime.now(_dt.timezone.utc)
    return ((now - ts).total_seconds() / 3600.0, str(raw))


def main():
    stale = []
    missing = []
    for label, rel, table, col, max_age, kind in CHECKS:
        path = REPO / rel
        if not path.exists():
            missing.append(label)
            print(f"DEADMAN: MISSING {label} ({rel})")
            continue
        if kind == "file":
            age = _age_hours(path)
            detail = f"{age:.1f}h old (file mtime)"
        else:
            res = _content_age_hours(path, table, col, kind == "content_date")
            if isinstance(res, tuple) and res and res[0] == "ERR":
                print(f"DEADMAN: WARN {label} — content read failed: {res[1]}")
                continue
            age, raw = res
            detail = f"{age:.1f}h old (max {col}={raw})"
        if age > max_age:
            stale.append((label, round(age, 1), max_age))
            print(f"DEADMAN: STALE {label} — {detail} (max {max_age}h)")
        else:
            print(f"DEADMAN: OK {label} — {detail}")

    # REVIEW FIX (F8): daemon liveness — the previous block queried the cycle
    # table, discarded the result, and never used it (dead code that looked like a
    # check). It now actually gates: if the newest daemon cycle is older than the
    # threshold, the dead-man switch fires.
    sc = REPO / "data" / "signal_cache.db"
    if sc.exists():
        try:
            c = sqlite3.connect(f"file:{sc}?mode=ro", uri=True)
            cols = [r[1] for r in c.execute("PRAGMA table_info(intraday_daemon_cycles)")]
            tcol = next((x for x in ("cycle_time", "timestamp", "created_at", "ts") if x in cols), None)
            if tcol:
                last = c.execute(f"SELECT MAX({tcol}) FROM intraday_daemon_cycles").fetchone()[0]
            else:
                last = None
            c.close()
            if last:
                import datetime as _dt
                try:
                    ts = _dt.datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=_dt.timezone.utc)
                    age_h = (_dt.datetime.now(_dt.timezone.utc) - ts).total_seconds() / 3600.0
                    if age_h > 12:
                        stale.append(("intraday_daemon_cycles", round(age_h, 1), 12))
                        print(f"DEADMAN: STALE intraday_daemon_cycles — last {age_h:.1f}h ago (max 12h)")
                    else:
                        print(f"DEADMAN: OK intraday_daemon_cycles — last {age_h:.1f}h ago")
                except Exception:
                    pass
        except Exception as e:
            print(f"DEADMAN: WARN — could not read daemon cycles: {e}")

    if stale or missing:
        print(f"\nDEADMAN: FAIL — {len(stale)} stale, {len(missing)} missing")
        return 1
    print("\nDEADMAN: PASS — all producers fresh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
