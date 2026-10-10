#!/usr/bin/env python3
"""
annotate_snapshot_provenance.py — freeze-and-go-forward annotation of the
non-canonical `settlement_snapshot` rows (governance actions A1 + A2).

Ruling: `.meta/continuity/weather-engine/governance-provenance-drift-disposition-2026-10-10.md`.

The immutable `settlement_snapshot` (data/metar_backfill.db) holds 25 rows that
were frozen from `weatherapi` by the backfill's last-resort write path — a
provisional source written into an immutable final-truth store. The store is
write-once (UPDATE/DELETE triggers), so the rows are NOT restated. Instead this
tool records the governance disposition in an additive, supersedable annotation
side table, keyed by the snapshot's primary key:

  * provenance_drift   — canonical truth EXISTS and disagrees (Oct-8, 5 rows).
                         Confirmed wrong; frozen; no restatement; no P&L impact.
                         The canonical value + drift are recorded as evidence.
  * provisional_source — no canonical truth yet (Oct-9, 20 rows). Pending, not
                         yet wrong. Marked so it is *expected* to be reconciled
                         (A4 promotion) rather than silently trusted.

The snapshot rows are never modified (no UPDATE/DELETE) — the tool asserts the
frozen count + value checksum are byte-identical before/after. Historical P&L is
not touched. Deterministic; DRY-RUN unless `--execute`.

Usage:
    python3 scripts/annotate_snapshot_provenance.py                 # dry-run
    python3 scripts/annotate_snapshot_provenance.py --execute
    python3 scripts/annotate_snapshot_provenance.py --no-live       # offline
    python3 scripts/annotate_snapshot_provenance.py --json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

# Reuse the backfill's Kalshi series map / event-date parser / HTTP helper so the
# canonical classification here can never drift from the canonical writer.
import backfill_canonical_settlement as bfs  # noqa: E402

SNAPSHOT_DB = REPO / "data" / "metar_backfill.db"

# Canonical temp sources allowed in the immutable final-truth store. A row whose
# temp_source is outside this set is non-canonical by construction.
CANONICAL_SNAPSHOT_SOURCES = frozenset({"kalshi_live_api", "kalshi_finalized"})

ANNOTATION_TABLE = "settlement_snapshot_annotation"
ANNOTATION_DDL = f"""
CREATE TABLE IF NOT EXISTS {ANNOTATION_TABLE} (
    station            TEXT NOT NULL,
    market_type        TEXT NOT NULL,
    local_trading_date TEXT NOT NULL,
    annotation         TEXT NOT NULL,   -- provenance_drift | provisional_source
    frozen_temp_f      REAL,            -- the frozen (immutable) value
    frozen_temp_source TEXT,            -- the non-canonical source on the row
    canonical_temp_f   REAL,            -- canonical value, when it exists
    canonical_source   TEXT,            -- kalshi_live_api | kalshi_finalized
    drift_f            REAL,            -- canonical - frozen (provenance_drift only)
    detail             TEXT,
    noted_at_utc       TEXT NOT NULL,
    PRIMARY KEY (station, market_type, local_trading_date)
);
"""
ANNOTATION_DDL_EXTRA = (
    f"CREATE INDEX IF NOT EXISTS idx_{ANNOTATION_TABLE}_annotation "
    f"ON {ANNOTATION_TABLE}(annotation);"
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
    """All snapshot rows (read-only). {} when the table is absent."""
    if not Path(path).exists():
        return []
    c = _ro(path)
    try:
        rows = c.execute(
            "SELECT station, market_type, local_trading_date, settlement_temp_f, "
            "temp_source FROM settlement_snapshot "
            "ORDER BY local_trading_date, station").fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        c.close()


def frozen_checksum(rows: List[Dict[str, Any]]) -> str:
    """Order-independent checksum of the frozen (station,date,value,source) set.
    Used to prove the annotation pass never moved a frozen row."""
    parts = sorted(
        f"{r['station']}|{r['market_type']}|{r['local_trading_date']}|"
        f"{r['settlement_temp_f']}|{r['temp_source']}" for r in rows)
    return f"n={len(parts)};sha=" + str(hash(tuple(parts)))


def fetch_canonical(stations: List[str], dates: List[str], *,
                    use_live: bool) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """{(station, date): {temp_f, source}} for the requested station-days.

    Kalshi live API `expiration_value` only. A station-day the live API has not
    finalized is simply absent -> the caller treats it as provisional.
    """
    out: Dict[Tuple[str, str], Dict[str, Any]] = {}
    if not use_live:
        return out
    import requests
    session = requests.Session()
    session.headers.update({"User-Agent": "WeatherEngineProvenanceAnnotate/1.0",
                            "Accept": "application/json"})
    want = set(dates)
    for st in stations:
        series = bfs._STATION_SERIES.get(st)
        if not series:
            continue
        cursor = None
        for _ in range(8):
            params = {"series_ticker": series, "status": "settled", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            j = bfs._api_get(f"{bfs._KALSHI_BASE}/markets", params, session)
            if not j:
                break
            for m in j.get("markets", []):
                d = bfs._event_date(m.get("event_ticker") or "")
                v = m.get("expiration_value")
                if d in want and v not in (None, ""):
                    out.setdefault((st, d), {"temp_f": float(v),
                                             "source": "kalshi_live_api"})
            cursor = j.get("cursor")
            if not cursor:
                break
            time.sleep(0.3)
    return out


def classify(rows: List[Dict[str, Any]], canonical: Dict[Tuple[str, str], Dict[str, Any]]
             ) -> List[Dict[str, Any]]:
    """Annotate every non-canonical snapshot row. Canonical rows are untouched."""
    out: List[Dict[str, Any]] = []
    for r in rows:
        src = r["temp_source"]
        if src in CANONICAL_SNAPSHOT_SOURCES:
            continue
        key = (r["station"], r["local_trading_date"])
        canon = canonical.get(key)
        rec = {
            "station": r["station"], "market_type": r["market_type"],
            "local_trading_date": r["local_trading_date"],
            "frozen_temp_f": r["settlement_temp_f"], "frozen_temp_source": src,
            "canonical_temp_f": None, "canonical_source": None, "drift_f": None,
            "annotation": "provisional_source",
            "detail": ("no canonical truth yet (Kalshi not finalized) -> pending, "
                       "not yet wrong; reconcile/promote on finalization (A4)"),
        }
        if canon is not None:
            drift = round(canon["temp_f"] - float(r["settlement_temp_f"]), 4)
            rec.update({
                "canonical_temp_f": canon["temp_f"],
                "canonical_source": canon["source"],
                "drift_f": drift,
                "annotation": "provenance_drift",
                "detail": (f"canonical {canon['temp_f']} != frozen "
                           f"{r['settlement_temp_f']} ({src}); confirmed drift, "
                           f"frozen, no restatement"),
            })
        out.append(rec)
    return out


def run(*, execute: bool, use_live: bool, snapshot_db: Path = SNAPSHOT_DB,
        json_out: bool = False) -> int:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = load_snapshot(snapshot_db)
    if not rows:
        print(f"[annotate] no settlement_snapshot rows found in {snapshot_db}")
        return 0
    before = frozen_checksum(rows)

    noncanon = [r for r in rows if r["temp_source"] not in CANONICAL_SNAPSHOT_SOURCES]
    stations = sorted({r["station"] for r in noncanon})
    dates = sorted({r["local_trading_date"] for r in noncanon})
    canonical = fetch_canonical(stations, dates, use_live=use_live)
    recs = classify(rows, canonical)

    drift = [r for r in recs if r["annotation"] == "provenance_drift"]
    prov = [r for r in recs if r["annotation"] == "provisional_source"]

    print(f"[annotate] snapshot rows={len(rows)} non-canonical={len(noncanon)} "
          f"-> provenance_drift={len(drift)} provisional_source={len(prov)}")
    print(f"[annotate] canonical sources allowed: {sorted(CANONICAL_SNAPSHOT_SOURCES)}")
    print(f"[annotate] frozen checksum before: {before}")
    for r in recs:
        extra = ""
        if r["annotation"] == "provenance_drift":
            extra = f" canon={r['canonical_temp_f']} drift={r['drift_f']}"
        print(f"  {r['annotation']:<17} {r['station']:<5} {r['local_trading_date']} "
              f"frozen={r['frozen_temp_f']}({r['frozen_temp_source']}){extra}")

    if execute:
        conn = _rw(snapshot_db)
        try:
            conn.executescript(ANNOTATION_DDL)
            conn.executescript(ANNOTATION_DDL_EXTRA)
            for r in recs:
                conn.execute(
                    f"INSERT OR REPLACE INTO {ANNOTATION_TABLE} "
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
        print("[annotate] FATAL: frozen snapshot checksum changed — aborting", file=sys.stderr)
        return 2
    print(f"[annotate] frozen checksum after:  {after} (unchanged: "
          f"{'yes' if before == after else 'NO'})")
    if not execute:
        print("[annotate] dry-run: no writes performed; re-run with --execute")
    if json_out:
        print(json.dumps({
            "snapshot_rows": len(rows), "non_canonical": len(noncanon),
            "provenance_drift": len(drift), "provisional_source": len(prov),
            "frozen_checksum": after, "records": recs,
        }, indent=2, sort_keys=True))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--no-live", action="store_true",
                    help="skip the Kalshi live API; all rows -> provisional_source")
    ap.add_argument("--snapshot-db", default=str(SNAPSHOT_DB))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    return run(execute=a.execute, use_live=not a.no_live,
               snapshot_db=Path(a.snapshot_db), json_out=a.json)


if __name__ == "__main__":
    raise SystemExit(main())
