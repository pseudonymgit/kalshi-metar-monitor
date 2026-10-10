#!/usr/bin/env python3
"""
regenerate_settlement_epochs.py — STAGED, DO NOT RUN until approved.

Corrected settlement-epochs regeneration for the 2026-09-17 recovery
(Option A). This script rewrites ONLY the corrupt settlement_epochs rows
using authoritative Kalshi settlement temperatures (HIGH markets) and clean
min_temp_f (LOW markets), with explicit per-row provenance labels.

⚠️  NOT AUTHORIZED TO RUN YET. Gated on:
    1. The METAR parser fix landing first (so future min_temp_f/max_temp_f
       are clean and the corruption does not recur).
    2. Dan's approval of the coverage split in
       docs/plans/SETTLEMENT-EPOCHS-REWRITE-PLAN-2026-09-17.md.

Read-only by default: this script refuses to mutate unless `--execute` is
explicitly passed AND a pre-flight backup exists.

Coverage (see plan doc for exact math):
    - 616 corrupt HIGH epochs
        - 488 recoverable via finalized Kalshi HIGH source
        -  83 recoverable via non-finalized clean Kalshi HIGH source
          (historical_backfill + live_api)
        -  45 unresolved (no clean Kalshi HIGH source)
    -   9 corrupt LOW epochs
        -   5 recoverable via historical_backfill Kalshi LOW source
        -   4 unresolved

B-Mode compliant. No AI/ML. Deterministic.

Usage (review only — nothing executes by default):
    python3 scripts/regenerate_settlement_epochs.py --dry-run
    python3 scripts/regenerate_settlement_epochs.py --coverage   # print coverage only
    python3 scripts/regenerate_settlement_epochs.py --execute    # MUTATES (gated)
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

# ── Paths ──
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_DIR = os.path.join(_REPO_ROOT, "data")
METAR_DB = os.path.join(_DATA_DIR, "metar_backfill.db")
KALSHI_DB = os.path.join(_DATA_DIR, "kalshi_settlements.db")

# ── Station alias resolution (mirrors kalshi_settlement_upkeep.STATION_ALIASES) ──
STATION_ALIASES = {
    "KORD": "KMDW",
    "KJFK": "KNYC",
    "KLGA": "KNYC",
    "KDAL": "KDFW",
}

# ── Sanity bounds for a plausible settlement temperature (°F) ──
TEMP_LOW = -60.0
TEMP_HIGH = 150.0

# ── Provenance labels (written verbatim into epoch_close_reason) ──
PROV_FINALIZED = "kalshi_finalized"
PROV_HIST_BACKFILL = "kalshi_historical_backfill"
PROV_LIVE_API = "kalshi_live_api"
PROV_MIN_TEMP = "clean_min_temp_f"
PROV_UNRESOLVED = "unresolved"


def _resolve_station(station: str) -> str:
    return STATION_ALIASES.get(station, station)


def _connect_ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _get_temp_bucket(temp_f: float) -> int:
    """Round to nearest whole degree; this is the settlement bucket."""
    return int(round(temp_f))


# ── Coverage computation ──────────────────────────────────────────────────────
def _corrupt_epochs(cur) -> Dict[str, List[Tuple[str, str]]]:
    """Return corrupt HIGH and LOW (station, local_trading_date) lists."""
    high = cur.execute(
        """
        SELECT DISTINCT station, local_trading_date FROM settlement_epochs
        WHERE market_type='HIGH'
          AND (settlement_bucket > 150 OR settlement_bucket < -50
               OR prior_settlement_bucket > 150 OR prior_settlement_bucket < -50)
        ORDER BY station, local_trading_date
        """
    ).fetchall()
    low = cur.execute(
        """
        SELECT DISTINCT station, local_trading_date FROM settlement_epochs
        WHERE market_type='LOW'
          AND (settlement_bucket > 150 OR settlement_bucket < -50
               OR prior_settlement_bucket > 150 OR prior_settlement_bucket < -50)
        ORDER BY station, local_trading_date
        """
    ).fetchall()
    return {"HIGH": high, "LOW": low}


def _kalshi_source(cur_kalshi, station: str, date: str, market_type: str) -> Optional[Tuple[float, str]]:
    """
    Find the authoritative Kalshi settlement temp for (station, date, market).

    Priority order (HIGH):
      1. source_type='finalized'   (live Kalshi API finalized settlement)
      2. source='live_api'         (live API, not yet marked finalized)
      3. source='historical_backfill' (Kalshi historical settlement API)

    For LOW: same priority but series LIKE 'KXLOW%'.

    Returns (temp_f, provenance_label) or None if no clean source.
    """
    rst = _resolve_station(station)
    series_like = "KXHIGH%" if market_type == "HIGH" else "KXLOW%"

    # Priority 1: finalized
    row = cur_kalshi.execute(
        """
        SELECT kalshi_temp FROM kalshi_settlements
        WHERE station=? AND target_date=? AND series LIKE ?
          AND source_type='finalized' AND kalshi_temp IS NOT NULL
        LIMIT 1
        """,
        (rst, date, series_like),
    ).fetchone()
    if row and TEMP_LOW <= row[0] <= TEMP_HIGH:
        return (row[0], PROV_FINALIZED)

    # Priority 2: live_api (any source_type)
    row = cur_kalshi.execute(
        """
        SELECT kalshi_temp FROM kalshi_settlements
        WHERE station=? AND target_date=? AND series LIKE ?
          AND source='live_api' AND kalshi_temp IS NOT NULL
        LIMIT 1
        """,
        (rst, date, series_like),
    ).fetchone()
    if row and TEMP_LOW <= row[0] <= TEMP_HIGH:
        return (row[0], PROV_LIVE_API)

    # Priority 3: historical_backfill
    row = cur_kalshi.execute(
        """
        SELECT kalshi_temp FROM kalshi_settlements
        WHERE station=? AND target_date=? AND series LIKE ?
          AND source='historical_backfill' AND kalshi_temp IS NOT NULL
        LIMIT 1
        """,
        (rst, date, series_like),
    ).fetchone()
    if row and TEMP_LOW <= row[0] <= TEMP_HIGH:
        return (row[0], PROV_HIST_BACKFILL)

    return None


def compute_coverage() -> Dict:
    """Compute the exact recoverable vs unresolved split. Read-only."""
    metar = _connect_ro(METAR_DB)
    kalshi = _connect_ro(KALSHI_DB)
    metar.execute(f"ATTACH DATABASE '{KALSHI_DB}' AS ks")
    cur = metar.cursor()
    cur_k = kalshi.cursor()

    corrupt = _corrupt_epochs(cur)

    result = {"HIGH": {"total": 0, "recovered": 0, "unresolved": 0, "by_source": {}, "unresolved_rows": []},
              "LOW": {"total": 0, "recovered": 0, "unresolved": 0, "by_source": {}, "unresolved_rows": []}}

    for mkt in ("HIGH", "LOW"):
        result[mkt]["total"] = len(corrupt[mkt])
        for st, dt in corrupt[mkt]:
            # Determine whether the CURRENT bucket is actually corrupt, or only
            # the prior bucket (prior-bucket corruption is fixed by the cascade
            # once the earlier corrupt row is corrected).
            if mkt == "HIGH":
                cur_corrupt = cur.execute(
                    "SELECT settlement_bucket FROM settlement_epochs WHERE station=? AND market_type=? AND local_trading_date=?",
                    (st, mkt, dt),
                ).fetchone()[0]
                cur_is_corrupt = cur_corrupt > 150 or cur_corrupt < -50
            else:
                cur_min = cur.execute(
                    "SELECT min_temp_f FROM daily_stats WHERE station=? AND date_local=?",
                    (st, dt),
                ).fetchone()
                cur_is_corrupt = not (cur_min and cur_min[0] is not None and TEMP_LOW <= cur_min[0] <= TEMP_HIGH)

            if not cur_is_corrupt:
                # Only the prior bucket was corrupt; the cascade fixes it.
                result[mkt]["recovered"] += 1
                result[mkt]["by_source"]["prior_bucket_cascade"] = result[mkt]["by_source"].get("prior_bucket_cascade", 0) + 1
                continue

            src = _kalshi_source(cur_k, st, dt, mkt)
            if src is None:
                result[mkt]["unresolved"] += 1
                result[mkt]["unresolved_rows"].append((st, dt))
            else:
                temp_f, prov = src
                result[mkt]["recovered"] += 1
                result[mkt]["by_source"][prov] = result[mkt]["by_source"].get(prov, 0) + 1

    metar.close()
    kalshi.close()
    return result


def print_coverage() -> None:
    cov = compute_coverage()
    print("=" * 60)
    print("  SETTLEMENT EPOCHS REWRITE — COVERAGE")
    print("=" * 60)
    for mkt in ("HIGH", "LOW"):
        c = cov[mkt]
        print(f"\n  {mkt}: {c['total']} corrupt")
        print(f"    recovered : {c['recovered']}")
        print(f"    unresolved: {c['unresolved']}")
        if c["by_source"]:
            print(f"    by source : {c['by_source']}")
        if c["unresolved_rows"]:
            print(f"    unresolved rows:")
            for st, dt in c["unresolved_rows"]:
                print(f"      {st} {dt}")
    print()


# ── Execution (gated) ─────────────────────────────────────────────────────────
def _backup_exists() -> bool:
    tag = datetime.now(timezone.utc).strftime("%Y%m%d")
    backup = f"{METAR_DB}.pre-settlement-fix-{tag}"
    return os.path.exists(backup)


def execute_rewrite() -> None:
    """
    Mutate settlement_epochs for the corrupt rows only. Gated on backup.
    Uses INSERT OR REPLACE keyed on the existing UNIQUE(station, market_type,
    local_trading_date) so only targeted rows change; provenance is written
    into epoch_close_reason.
    """
    if not _backup_exists():
        print("ERROR: no pre-flight backup found. Refusing to mutate.")
        print("       Run:  cp data/metar_backfill.db data/metar_backfill.db.pre-settlement-fix-$(date +%Y%m%d)")
        sys.exit(2)

    metar = sqlite3.connect(METAR_DB, timeout=30)
    kalshi = _connect_ro(KALSHI_DB)
    cur = metar.cursor()
    cur_k = kalshi.cursor()

    corrupt = _corrupt_epochs(cur)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    updated = 0
    unresolved = 0

    # Frozen-settlement invariant (2026-10-10): refuse the rewrite if it would
    # move a frozen canonical settlement value. Fail closed, before any write.
    try:
        from core.canonical_settlement import verify_regeneration_invariants
        candidates = []
        for mkt in ("HIGH", "LOW"):
            for st, dt in corrupt[mkt]:
                row = cur.execute(
                    "SELECT settlement_bucket FROM settlement_epochs "
                    "WHERE station=? AND market_type=? AND local_trading_date=?",
                    (st, mkt, dt)).fetchone()
                if row is not None:
                    candidates.append({"station": st, "market_type": mkt,
                                       "local_trading_date": dt,
                                       "settlement_bucket": row[0]})
        violations = verify_regeneration_invariants(metar, candidates)
        if violations:
            print("ERROR: regeneration refused — would move a frozen canonical "
                  "settlement value:")
            for v in violations:
                print("  " + v)
            metar.close()
            kalshi.close()
            sys.exit(3)
    except ImportError:
        pass

    for mkt in ("HIGH", "LOW"):
        for st, dt in corrupt[mkt]:
            # Determine whether CURRENT bucket is corrupt (prior-bucket-only
            # corruption is fixed by the cascade once earlier rows are corrected).
            if mkt == "HIGH":
                cur_bucket = cur.execute(
                    "SELECT settlement_bucket FROM settlement_epochs WHERE station=? AND market_type=? AND local_trading_date=?",
                    (st, mkt, dt),
                ).fetchone()[0]
                cur_is_corrupt = cur_bucket > 150 or cur_bucket < -50
            else:
                cur_min = cur.execute(
                    "SELECT min_temp_f FROM daily_stats WHERE station=? AND date_local=?",
                    (st, dt),
                ).fetchone()
                cur_is_corrupt = not (cur_min and cur_min[0] is not None and TEMP_LOW <= cur_min[0] <= TEMP_HIGH)

            src = _kalshi_source(cur_k, st, dt, mkt) if cur_is_corrupt else None

            if cur_is_corrupt and src is None:
                # Mark unresolved: set epoch_status, record provenance so
                # downstream consumers can exclude it. Bucket left untouched.
                cur.execute(
                    """
                    UPDATE settlement_epochs
                       SET epoch_status='unresolved',
                           epoch_close_reason=?,
                           epoch_close_timestamp_utc=?
                     WHERE station=? AND market_type=? AND local_trading_date=?
                    """,
                    (PROV_UNRESOLVED, now, st, mkt, dt),
                )
                unresolved += 1
                continue

            if cur_is_corrupt:
                temp_f, prov = src
                bucket = _get_temp_bucket(temp_f)
            else:
                # Prior-bucket-only corruption: current bucket already correct.
                bucket = cur.execute(
                    "SELECT settlement_bucket FROM settlement_epochs WHERE station=? AND market_type=? AND local_trading_date=?",
                    (st, mkt, dt),
                ).fetchone()[0]
                prov = PROV_MIN_TEMP  # current value is clean min_temp_f

            # Recompute reversion against the prior day's bucket (already clean
            # or previously corrected in this same pass — processed in date order).
            prev = cur.execute(
                """
                SELECT settlement_bucket FROM settlement_epochs
                 WHERE station=? AND market_type=? AND local_trading_date < ?
                 ORDER BY local_trading_date DESC LIMIT 1
                """,
                (st, mkt, dt),
            ).fetchone()
            prior_bucket = prev[0] if prev else None
            reversion = 1 if (prior_bucket is not None and bucket > prior_bucket) else 0

            cur.execute(
                """
                UPDATE settlement_epochs
                   SET settlement_bucket=?,
                       prior_settlement_bucket=?,
                       reversion_occurred=?,
                       epoch_status='closed',
                       epoch_close_reason=?,
                       epoch_close_timestamp_utc=?
                 WHERE station=? AND market_type=? AND local_trading_date=?
                """,
                (bucket, prior_bucket, reversion, prov, now, st, mkt, dt),
            )
            updated += 1

    metar.commit()
    metar.close()
    kalshi.close()
    print(f"Rewrite complete: {updated} rows corrected, {unresolved} marked unresolved.")


def main() -> None:
    p = argparse.ArgumentParser(description="Settlement epochs rewrite (staged/gated)")
    p.add_argument("--coverage", action="store_true", help="print coverage only (read-only)")
    p.add_argument("--dry-run", action="store_true", help="print coverage and exit (read-only)")
    p.add_argument("--execute", action="store_true", help="MUTATE settlement_epochs (requires backup)")
    args = p.parse_args()

    if args.execute:
        execute_rewrite()
    else:
        print_coverage()


if __name__ == "__main__":
    main()
