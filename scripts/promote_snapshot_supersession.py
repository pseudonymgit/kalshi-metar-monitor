#!/usr/bin/env python3
"""
promote_snapshot_supersession.py — A4 one-time promotion: annotate provisional
frozen snapshot rows as SUPERSEDED BY CANONICAL truth, without ever overwriting
the frozen value.

Ruling: `.meta/continuity/weather-engine/governance-provenance-drift-disposition-2026-10-10.md`
(A1/A2 annotate · A3 block provisional writes · **A4 provisional→canonical promotion**).

Context
-------
`data/metar_backfill.db::settlement_snapshot` is write-once (UPDATE/DELETE
triggers abort). On 2026-10-09 all 20 HIGH rows were frozen from the `weatherapi`
provisional source before Kalshi finalized the day. The A2 pass annotated them
`provisional_source` (pending). Kalshi has since FINALIZED Oct 9 — canonical
truth now exists (e.g. KATL 73, KDFW 91, KSEA 58, KSFO 71) — but the frozen
provisional rows cannot self-heal, and the settle writer must never book them.

This tool records the promotion in an ADDITIVE side table keyed by the snapshot
primary key (`station, market_type, local_trading_date`) — the same pattern as
A1/A2 (`settlement_snapshot_annotation`). The frozen snapshot rows are never
UPDATE/DELETE-ed; the tool asserts a byte-identical value checksum before/after.
The A2 `provisional_source` annotation is preserved (this is a distinct table);
the two together show "pending" -> "superseded by canonical".

Promotion source: a finalized `kalshi_settlements` row
(`station`/`target_date`/`source_type='finalized'`). No live network call.

Deterministic. No AI/ML. No historical P&L touched. DRY-RUN unless `--execute`.

Usage:
    python3 scripts/promote_snapshot_supersession.py                 # dry-run
    python3 scripts/promote_snapshot_supersession.py --execute
    python3 scripts/promote_snapshot_supersession.py --json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from core.canonical_settlement import (  # noqa: E402
    CANONICAL_TEMP_SOURCES,
    is_canonical_temp_source,
    resolve_finalized_settlement,
)

SNAPSHOT_DB = REPO / "data" / "metar_backfill.db"
KALSHI_DB = REPO / "data" / "kalshi_settlements.db"

SUPERSESSION_TABLE = "settlement_snapshot_supersession"
SUPERSESSION_DDL = f"""
CREATE TABLE IF NOT EXISTS {SUPERSESSION_TABLE} (
    station            TEXT NOT NULL,
    market_type        TEXT NOT NULL,
    local_trading_date TEXT NOT NULL,
    annotation         TEXT NOT NULL,   -- superseded_by_canonical
    frozen_temp_f      REAL,            -- the frozen (immutable) value
    frozen_temp_source TEXT,            -- non-canonical source on the row
    canonical_temp_f   REAL,            -- finalized canonical value
    canonical_source   TEXT,            -- kalshi_finalized
    drift_f            REAL,            -- canonical - frozen
    detail             TEXT,
    noted_at_utc       TEXT NOT NULL,
    PRIMARY KEY (station, market_type, local_trading_date)
);
"""
SUPERSESSION_INDEX = (
    f"CREATE INDEX IF NOT EXISTS idx_{SUPERSESSION_TABLE}_annotation "
    f"ON {SUPERSESSION_TABLE}(annotation);"
)


def _ro(p: Path) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _rw(p: Path) -> sqlite3.Connection:
    c = sqlite3.connect(str(p), timeout=30)
    c.execute("PRAGMA busy_timeout=30000")
    return c


def load_snapshot(path: Path) -> List[Dict[str, Any]]:
    """All snapshot rows read-only; [] when the table is absent."""
    c = _ro(path)
    try:
        return [dict(r) for r in c.execute(
            "SELECT station, market_type, local_trading_date, settlement_temp_f, "
            "temp_source FROM settlement_snapshot ORDER BY local_trading_date, station")]
    except sqlite3.OperationalError:
        return []
    finally:
        c.close()


def frozen_checksum(rows: List[Dict[str, Any]]) -> str:
    """Order-independent sha256 of the frozen (station,date,value,source) set.

    Proves the promotion pass never moved a frozen row (before == after).
    """
    parts = sorted(
        f"{r['station']}|{r['market_type']}|{r['local_trading_date']}|"
        f"{r['settlement_temp_f']}|{r['temp_source']}" for r in rows)
    return f"n={len(parts)};sha256=" + hashlib.sha256(
        "\n".join(parts).encode("utf-8")).hexdigest()


def build_promotions(rows: List[Dict[str, Any]], *,
                     kalshi_db: Path = KALSHI_DB) -> List[Dict[str, Any]]:
    """Resolution for every non-canonical snapshot row.

    A row whose temp_source is already canonical is skipped. A row with a
    finalized `kalshi_settlements` truth becomes `superseded_by_canonical`; one
    without stays absent (still provisional, not yet promotable).
    """
    out: List[Dict[str, Any]] = []
    for r in rows:
        if is_canonical_temp_source(r["temp_source"]):
            continue
        fin = resolve_finalized_settlement(
            r["station"], r["market_type"], r["local_trading_date"],
            kalshi_db=str(kalshi_db))
        if fin is None:
            continue
        frozen = float(r["settlement_temp_f"])
        canon = float(fin["settlement_temp_f"])
        out.append({
            "station": r["station"], "market_type": r["market_type"],
            "local_trading_date": r["local_trading_date"],
            "annotation": "superseded_by_canonical",
            "frozen_temp_f": frozen, "frozen_temp_source": r["temp_source"],
            "canonical_temp_f": canon, "canonical_source": fin["temp_source"],
            "drift_f": round(canon - frozen, 4),
            "detail": (f"finalized canonical {canon} supersedes frozen "
                       f"{frozen} ({r['temp_source']}); frozen value retained "
                       f"byte-identical, no restatement"),
        })
    return out


def run(*, execute: bool, snapshot_db: Path = SNAPSHOT_DB,
        kalshi_db: Path = KALSHI_DB, json_out: bool = False) -> int:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = load_snapshot(snapshot_db)
    if not rows:
        print(f"[supersession] no settlement_snapshot rows found in {snapshot_db}")
        return 0
    before = frozen_checksum(rows)
    noncanon = [r for r in rows if not is_canonical_temp_source(r["temp_source"])]
    recs = build_promotions(rows, kalshi_db=kalshi_db)

    print(f"[supersession] snapshot rows={len(rows)} non-canonical={len(noncanon)} "
          f"-> promotable(superseded_by_canonical)={len(recs)}")
    print(f"[supersession] canonical sources: {sorted(CANONICAL_TEMP_SOURCES)}")
    print(f"[supersession] frozen checksum before: {before}")
    for r in recs:
        print(f"  {r['annotation']:<24} {r['station']:<5} {r['local_trading_date']} "
              f"frozen={r['frozen_temp_f']}({r['frozen_temp_source']}) "
              f"-> canonical={r['canonical_temp_f']}({r['canonical_source']}) "
              f"drift={r['drift_f']}")

    if execute:
        conn = _rw(snapshot_db)
        try:
            conn.executescript(SUPERSESSION_DDL)
            conn.executescript(SUPERSESSION_INDEX)
            for r in recs:
                conn.execute(
                    f"INSERT OR REPLACE INTO {SUPERSESSION_TABLE} "
                    "(station, market_type, local_trading_date, annotation, "
                    " frozen_temp_f, frozen_temp_source, canonical_temp_f, "
                    " canonical_source, drift_f, detail, noted_at_utc) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (r["station"], r["market_type"], r["local_trading_date"],
                     r["annotation"], r["frozen_temp_f"], r["frozen_temp_source"],
                     r["canonical_temp_f"], r["canonical_source"], r["drift_f"],
                     r["detail"], now))
            conn.commit()
        finally:
            conn.close()

    after_rows = load_snapshot(snapshot_db)
    after = frozen_checksum(after_rows)
    if before != after:
        print("[supersession] FATAL: frozen snapshot checksum changed — aborting",
              file=sys.stderr)
        return 2
    print(f"[supersession] frozen checksum after:  {after} (unchanged: yes)")
    if not execute:
        print("[supersession] dry-run: no writes performed; re-run with --execute")
    if json_out:
        print(json.dumps({
            "snapshot_rows": len(rows), "non_canonical": len(noncanon),
            "promotable": len(recs), "frozen_checksum": after, "records": recs,
        }, indent=2, sort_keys=True))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--snapshot-db", default=str(SNAPSHOT_DB))
    ap.add_argument("--kalshi-db", default=str(KALSHI_DB))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    return run(execute=a.execute, snapshot_db=Path(a.snapshot_db),
               kalshi_db=Path(a.kalshi_db), json_out=a.json)


if __name__ == "__main__":
    raise SystemExit(main())
