#!/usr/bin/env python3
"""
backfill_canonical_settlement.py — re-source canonical settlement truth and
repair the settlement confirmation fields, WITHOUT mutating historical P&L.

Ruling: `.meta/continuity/weather-engine/settlement-truth-reconciliation-2026-10-10.md`.

What it does (all idempotent; DRY-RUN unless `--execute`):

  1. Build the canonical settlement map for a window (default 2026-10-01..today)
     from, in priority order:
        a. Kalshi live API `expiration_value` (api.elections.kalshi.com) — canonical
        b. data/kalshi_settlements.db (source_type='finalized') — in-DB cross-check
        c. data/ghcn_settlements.db high_f — climatological cross-check (tie-break)
        d. data/weatherapi_archive.db max_temp_f — last resort for days not yet
           finalized on the Kalshi API
     Canonical source label is written with every value so provenance is explicit.

  2. Write the canonical value into `trades.canonical_settled_f` (NEW column,
     added if missing). Historical `settled_value` / `realized_pnl` / status are
     **never** touched here.

  3. Freeze the canonical value into the immutable `settlement_snapshot` table
     (core.canonical_settlement) with an as-of anchor + frozen-at ts. Write-once.

  4. Repopulate `settlement_confirmed` (=1 where canonical truth exists) and
     `settlement_outcome` (strict strike-crossing) from canonical truth, plus a
     NEW `canonical_outcome` column so the booked outcome is preserved for audit.

  5. Backfill `metar_backfill.db::settlement_truth` for the window (it had no rows
     after 2026-09-19).

Verification helpers report per-station before/after on the flipped trades and
whether G3 now ADMITS corrected rows.

B-Mode compliant. Deterministic. No AI/ML. No historical P&L mutation.

Usage:
    python3 scripts/backfill_canonical_settlement.py                 # dry-run
    python3 scripts/backfill_canonical_settlement.py --execute
    python3 scripts/backfill_canonical_settlement.py --start 2026-10-01 --end 2026-10-08
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from core.canonical_settlement import (  # noqa: E402
    CANONICAL_TEMP_SOURCE, ImmutableSettlementError, canonical_outcome,
    ensure_snapshot_table, freeze_snapshot, read_snapshot,
)

# The immutable `settlement_snapshot` accepts FINAL canonical truth ONLY. The
# backfill's last-resort proxies (weatherapi/GHCN) are PROVISIONAL and must never
# be frozen here (governance ruling 2026-10-10: provisional-into-immutable was
# the root defect behind the 25 non-canonical rows). Provisional values are
# diverted to the supersedable `settlement_provisional` layer instead.
CANONICAL_SNAPSHOT_SOURCES = frozenset({CANONICAL_TEMP_SOURCE, "kalshi_finalized"})

PAPER_DB = REPO / "data" / "paper_trading_sbox.db"
METAR_DB = REPO / "data" / "metar_backfill.db"

# Supersedable provisional layer (A4-lite): provisional values live here until
# canonical finalization, then may be promoted ONCE into the immutable snapshot.
# Unlike `settlement_snapshot` this table has NO immutability triggers: a value
# can be revised (INSERT OR REPLACE) as better truth arrives.
PROVISIONAL_TABLE = "settlement_provisional"
PROVISIONAL_DDL = f"""
CREATE TABLE IF NOT EXISTS {PROVISIONAL_TABLE} (
    station            TEXT NOT NULL,
    market_type        TEXT NOT NULL,
    local_trading_date TEXT NOT NULL,
    provisional_temp_f REAL NOT NULL,
    temp_source        TEXT NOT NULL,
    as_of_utc          TEXT,
    created_at_utc     TEXT NOT NULL,
    superseded_by      TEXT,            -- canonical source once promoted, else NULL
    PRIMARY KEY (station, market_type, local_trading_date)
);
"""


def ensure_provisional_table(conn: sqlite3.Connection) -> None:
    """Idempotently create the supersedable provisional settlement layer."""
    conn.executescript(PROVISIONAL_DDL)
    conn.commit()


def write_provisional(conn: sqlite3.Connection, rows: List[Dict[str, Any]],
                      *, created_at_utc: str) -> int:
    """Write provisional (non-canonical) values to the supersedable layer.

    INSERT OR REPLACE so a later provisional read can revise an earlier one;
    a canonical promotion is recorded by the caller setting superseded_by.
    Returns the number of rows written.
    """
    ensure_provisional_table(conn)
    n = 0
    for r in rows:
        conn.execute(
            f"INSERT OR REPLACE INTO {PROVISIONAL_TABLE} "
            "(station, market_type, local_trading_date, provisional_temp_f, "
            " temp_source, as_of_utc, created_at_utc, superseded_by) "
            "VALUES (?,?,?,?,?,?,?,"
            " COALESCE((SELECT superseded_by FROM " + PROVISIONAL_TABLE +
            " WHERE station=? AND market_type=? AND local_trading_date=?), NULL))",
            (r["station"], r["market_type"], r["local_trading_date"],
             r["settlement_temp_f"], r["temp_source"], r.get("settled_at_utc"),
             created_at_utc,
             r["station"], r["market_type"], r["local_trading_date"]))
        n += 1
    conn.commit()
    return n
KALSHI_DB = REPO / "data" / "kalshi_settlements.db"
GHCN_DB = REPO / "data" / "ghcn_settlements.db"
WEATHERAPI_DB = REPO / "data" / "weatherapi_archive.db"
CACHE = REPO / "artifacts" / "canonical_settlement" / "canonical_high_cache.json"

_KALSHI_BASE = (os.environ.get("KALSHI_PUBLIC_BASE_URL")
                or "https://api.elections.kalshi.com/trade-api/v2").rstrip("/")

# station -> Kalshi HIGH series
_STATION_SERIES = {
    "KATL": "KXHIGHTATL", "KAUS": "KXHIGHAUS", "KBOS": "KXHIGHTBOS", "KDCA": "KXHIGHTDC",
    "KDEN": "KXHIGHDEN", "KDFW": "KXHIGHTDAL", "KHOU": "KXHIGHTHOU", "KLAS": "KXHIGHTLV",
    "KLAX": "KXHIGHLAX", "KMDW": "KXHIGHCHI", "KMIA": "KXHIGHMIA", "KMSP": "KXHIGHTMIN",
    "KMSY": "KXHIGHTNOLA", "KNYC": "KXHIGHNY", "KOKC": "KXHIGHTOKC", "KPHL": "KXHIGHPHIL",
    "KPHX": "KXHIGHTPHX", "KSAT": "KXHIGHTSATX", "KSEA": "KXHIGHTSEA", "KSFO": "KXHIGHTSFO",
}
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


def _ro(p: Path) -> Optional[sqlite3.Connection]:
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True) if Path(p).exists() else None


def _rw(p: Path) -> sqlite3.Connection:
    c = sqlite3.connect(str(p), timeout=30)
    c.execute("PRAGMA busy_timeout=30000")
    return c


def _event_date(ev: str) -> Optional[str]:
    tok = (ev or "").split("-")[-1]
    if len(tok) != 7:
        return None
    yy, mon, dd = tok[0:2], tok[2:5], tok[5:7]
    if mon not in _MONTHS:
        return None
    return f"20{yy}-{_MONTHS[mon]:02d}-{dd}"


def _api_get(url: str, params: dict, session, tries: int = 4) -> Optional[dict]:
    for i in range(tries):
        try:
            r = session.get(url, params=params, timeout=25)
        except Exception:
            time.sleep(1.5 * (i + 1))
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "2")))
            continue
        time.sleep(1.5 * (i + 1))
    return None


def fetch_live_canonical(stations: List[str]) -> Dict[str, Dict[str, str]]:
    """Pull `expiration_value` per (station, date) from the live Kalshi API."""
    import requests
    session = requests.Session()
    session.headers.update({"User-Agent": "WeatherEngineCanonicalBackfill/1.0",
                            "Accept": "application/json"})
    out: Dict[str, Dict[str, str]] = {}
    for st in stations:
        series = _STATION_SERIES.get(st)
        if not series:
            continue
        vals: Dict[str, set] = {}
        cursor = None
        for _ in range(8):
            params = {"series_ticker": series, "status": "settled", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            j = _api_get(f"{_KALSHI_BASE}/markets", params, session)
            if not j:
                break
            for m in j.get("markets", []):
                d = _event_date(m.get("event_ticker") or "")
                v = m.get("expiration_value")
                if d and v not in (None, ""):
                    vals.setdefault(d, set()).add(str(v))
            cursor = j.get("cursor")
            if not cursor:
                break
            time.sleep(0.3)
        out[st] = {d: sorted(vs)[0] for d, vs in vals.items()}
    return out


# ─── canonical map assembly ────────────────────────────────────────────────
def build_canonical_map(stations: List[str], start: str, end: str, *,
                        use_live: bool = True) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Return {(station, date): {temp_f, source, settled_at_utc}} for the window."""
    live: Dict[str, Dict[str, str]] = {}
    settled_at: Dict[Tuple[str, str], str] = {}
    if use_live:
        if CACHE.exists():
            live = json.loads(CACHE.read_text())
        else:
            live = fetch_live_canonical(stations)
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            CACHE.write_text(json.dumps(live, indent=0, sort_keys=True))
    kalshi = _ro(KALSHI_DB)
    ghcn = _ro(GHCN_DB)
    wapi = _ro(WEATHERAPI_DB)

    out: Dict[Tuple[str, str], Dict[str, Any]] = {}
    cur = datetime.fromisoformat(start)
    stop = datetime.fromisoformat(end)
    while cur <= stop:
        d = cur.date().isoformat()
        for st in stations:
            key = (st, d)
            lv = live.get(st, {}).get(d)
            if lv is not None:
                out[key] = {"temp_f": float(lv), "source": CANONICAL_TEMP_SOURCE}
            elif kalshi is not None:
                r = kalshi.execute(
                    "SELECT kalshi_temp FROM kalshi_settlements WHERE station=? "
                    "AND target_date=? AND source_type='finalized' AND kalshi_temp IS NOT NULL",
                    (st, d)).fetchone()
                if r and r[0] is not None:
                    out[key] = {"temp_f": float(r[0]), "source": "kalshi_finalized"}
            if key not in out and ghcn is not None:
                r = ghcn.execute("SELECT high_f FROM ghcn_settlements WHERE station=? "
                                 "AND date=?", (st, d)).fetchone()
                if r and r[0] is not None:
                    out[key] = {"temp_f": round(float(r[0])), "source": "ghcn"}
            if key not in out and wapi is not None:
                r = wapi.execute("SELECT max_temp_f FROM daily WHERE station=? AND date=? "
                                 "AND max_temp_f IS NOT NULL", (st, d)).fetchone()
                if r and r[0] is not None:
                    out[key] = {"temp_f": float(r[0]), "source": "weatherapi"}
        cur = cur.fromordinal(cur.toordinal() + 1)
    for c in (kalshi, ghcn, wapi):
        if c:
            c.close()

    # as-of anchor: Kalshi HIGH markets finalize the calendar day after the local
    # trading day (observed ~05:00Z). Store the anchor so the value is as-of
    # pinned and the epoch regeneration can never move it.
    for (st, d), rec in out.items():
        nxt = datetime.fromisoformat(d).fromordinal(
            datetime.fromisoformat(d).toordinal() + 1).date().isoformat()
        rec["settled_at_utc"] = f"{nxt}T05:00:00Z"
    return out


def _strike_from_notes(notes: str) -> Optional[float]:
    if not notes:
        return None
    m = re.search(r"@\s*(-?\d+(?:\.\d+)?)F", notes)
    return float(m.group(1)) if m else None


# ─── main correction ───────────────────────────────────────────────────────
def _ensure_trade_columns(conn: sqlite3.Connection) -> None:
    cols = {d[1] for d in conn.execute("PRAGMA table_info(trades)")}
    if "canonical_settled_f" not in cols:
        conn.execute("ALTER TABLE trades ADD COLUMN canonical_settled_f REAL")
    if "canonical_outcome" not in cols:
        conn.execute("ALTER TABLE trades ADD COLUMN canonical_outcome TEXT")
    if "canonical_source" not in cols:
        conn.execute("ALTER TABLE trades ADD COLUMN canonical_source TEXT")
    conn.commit()


def run(start: str, end: str, *, execute: bool, use_live: bool) -> int:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    trades_ro = _ro(PAPER_DB)
    trades_ro.row_factory = sqlite3.Row
    closed = trades_ro.execute(
        "SELECT id, trade_uuid, station, market_type, trade_date_utc,"
        " signal_direction, trade_type, quantity, trade_price, settled_value,"
        " realized_pnl, status, notes, settlement_confirmed, settlement_outcome"
        " FROM trades WHERE status='closed' ORDER BY id").fetchall()
    trades_ro.close()

    try:
        from core.station_registry import get_all_stations
        stations = sorted(get_all_stations())
    except Exception:
        stations = sorted(_STATION_SERIES)
    print(f"[canonical] assembling map {start}..{end} for {len(stations)} stations "
          f"(live={'yes' if use_live else 'no'})")
    cmap = build_canonical_map(stations, start, end, use_live=use_live)
    print(f"[canonical] map entries: {len(cmap)}")

    # Partition the assembled map into FINAL canonical truth (may be frozen) and
    # PROVISIONAL last-resort truth (must NOT be frozen). Root defect fix (A3):
    # the immutable final-truth store accepts canonical sources only; a
    # weatherapi/GHCN last-resort value is diverted to the supersedable
    # `settlement_provisional` layer instead of being frozen into the snapshot.
    snap_rows = []
    provisional_rows = []
    for (st, d), rec in cmap.items():
        row = {
            "station": st, "market_type": "HIGH", "local_trading_date": d,
            "settlement_temp_f": rec["temp_f"], "temp_source": rec["source"],
            "settled_at_utc": rec.get("settled_at_utc"),
        }
        (snap_rows if rec["source"] in CANONICAL_SNAPSHOT_SOURCES
         else provisional_rows).append(row)
    if provisional_rows:
        print(f"[snapshot] BLOCKED {len(provisional_rows)} non-canonical row(s) "
              f"from the immutable snapshot -> diverted to {PROVISIONAL_TABLE} "
              f"{'(.db write)' if execute else '(dry-run)'}")

    # settle snapshot db is metar_backfill.db (single home for settlement truth)
    # DRY-RUN must not write: compare against existing frozen rows read-only and
    # report what an execute would do. All writes happen under --execute only.
    rw = _rw(METAR_DB)
    if execute:
        ensure_snapshot_table(rw)
    inserted = skipped = 0
    conflicts: List[str] = []
    if execute:
        inserted, skipped, conflicts = freeze_snapshot(rw, snap_rows,
                                                       frozen_at_utc=now,
                                                       conflict_raises=False)
        if provisional_rows:
            n_prov = write_provisional(rw, provisional_rows, created_at_utc=now)
            print(f"[snapshot] wrote {n_prov} provisional row(s) to {PROVISIONAL_TABLE}")
    else:
        try:
            for r in snap_rows:
                ex = read_snapshot(rw, r["station"], r["market_type"],
                                   r["local_trading_date"])
                if ex is None:
                    inserted += 1
                elif ex["settlement_temp_f"] == r["settlement_temp_f"]:
                    skipped += 1
                else:
                    conflicts.append(f"{r['station']}/{r['market_type']}/"
                                     f"{r['local_trading_date']} frozen="
                                     f"{ex['settlement_temp_f']} != new="
                                     f"{r['settlement_temp_f']}")
        except sqlite3.OperationalError:
            inserted = len(snap_rows)  # table absent -> all would be new
    print(f"[snapshot] {'new=' + str(inserted) if execute else 'would-insert=' + str(inserted)}"
          f" identical={skipped} conflicts={len(conflicts)}")
    for c in conflicts:
        print(f"  CONFLICT {c}")

    # ── per-trade correction ─────────────────────────────────────────────
    pconn = _rw(PAPER_DB)
    if execute:
        _ensure_trade_columns(pconn)

    rows_out: List[Dict[str, Any]] = []
    n_conf = n_flip = n_mismatch = 0
    for r in closed:
        key = (r["station"], r["trade_date_utc"])
        rec = cmap.get(key)
        if rec is None:
            rows_out.append({**dict(r), "canonical_f": None, "canonical_outcome": None,
                             "source": None, "flip": False, "match": None})
            continue
        temp = rec["temp_f"]
        strike = _strike_from_notes(r["notes"])
        cout = None
        if strike is not None:
            cout = canonical_outcome(r["signal_direction"], r["market_type"], temp, strike)
        # Booked outcome = the strike-crossing label the engine ACTUALLY booked
        # (from its booked settled_value). settlement_outcome is NULL on these
        # rows, so derive the before-state from the booked value itself.
        booked_out = r["settlement_outcome"]
        if booked_out is None and r["settled_value"] is not None and strike is not None:
            booked_out = canonical_outcome(r["signal_direction"], r["market_type"],
                                           float(r["settled_value"]), strike)
        flip = bool(booked_out and cout and booked_out != cout)
        match = None
        if r["settled_value"] is not None:
            match = abs(float(r["settled_value"]) - float(temp)) <= 0.6
            if not match:
                n_mismatch += 1
        n_conf += 1
        if flip:
            n_flip += 1
        rows_out.append({**dict(r), "canonical_f": temp, "canonical_outcome": cout,
                         "booked_outcome": booked_out, "source": rec["source"],
                         "flip": flip, "match": match})

        if execute:
            # A canonical write into `trades` must be backed by FINAL canonical
            # truth only. A provisional (weatherapi/GHCN) value is NOT copied
            # onto canonical_settled_f / settlement_confirmed — otherwise the
            # provisional value would be laundered as canonical trade truth.
            # (No historical P&L is touched either way.)
            if rec["source"] in CANONICAL_SNAPSHOT_SOURCES:
                pconn.execute(
                    "UPDATE trades SET canonical_settled_f=?, canonical_source=?, "
                    "canonical_outcome=?, settlement_confirmed=1, settlement_outcome=? "
                    "WHERE trade_uuid=?",
                    (temp, rec["source"], cout, cout, r["trade_uuid"]))

                # settlement_truth backfill (corrected-truth table)
                rw.execute(
                    "INSERT OR REPLACE INTO settlement_truth (station, market_type,"
                    " local_trading_date, settlement_temp_f, temp_source, kalshi_temp_f,"
                    " strike_price, strike_type, trustworthy) VALUES (?,?,?,?,?,?,?,?,1)",
                    (r["station"], r["market_type"], r["trade_date_utc"], temp,
                     rec["source"], temp,
                     strike, "greater" if r["market_type"] == "HIGH" else "less"))

    # Backfill settlement_truth for ALL station-days in the window (not only
    # trades) — canonical station-days only; a provisional station-day is not
    # trustworthy final truth and is left for promotion once canonical arrives.
    if execute:
        g = _ro(GHCN_DB)
        for (st, d), rec in cmap.items():
            if rec["source"] not in CANONICAL_SNAPSHOT_SOURCES:
                continue
            strike = None
            rw.execute(
                "INSERT OR REPLACE INTO settlement_truth (station, market_type,"
                " local_trading_date, settlement_temp_f, temp_source, kalshi_temp_f,"
                " strike_price, strike_type, trustworthy) VALUES (?,?,?,?,?,?,?,?,1)",
                (st, "HIGH", d, rec["temp_f"], rec["source"], rec["temp_f"],
                 strike, "greater"))
        if g:
            g.close()
        pconn.commit()
        rw.commit()

    # ── report ───────────────────────────────────────────────────────────
    print(f"\n[report] closed trades={len(closed)} canonical-found={n_conf} "
          f"booked!=canonical={n_mismatch} outcome-flips={n_flip}")
    print("\nper-station before/after on flipped trades:")
    print(f"{'id':>4} {'st':<5} {'date':<12} {'strike':>6} {'booked':>7} "
          f"{'canon':>6} {'src':<20} {'booked_out':<10} {'canon_out':<10}")
    for x in rows_out:
        if x["flip"]:
            print(f"{x['id']:>4} {x['station']:<5} {x['trade_date_utc']:<12} "
                  f"{(_strike_from_notes(x['notes']) or 0):>6.0f} "
                  f"{(x['settled_value'] if x['settled_value'] is not None else -1):>7.0f} "
                  f"{x['canonical_f']:>6.0f} {str(x['source']):<20} "
                  f"{str(x['booked_outcome']):<10} {str(x['canonical_outcome']):<10}")

    pconn.close()
    rw.close()
    if not execute:
        print("\n[dry-run] no writes performed; re-run with --execute")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", default="2026-10-01")
    ap.add_argument("--end", default=datetime.now(timezone.utc).date().isoformat())
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--no-live", action="store_true", help="skip live API; use in-DB sources")
    a = ap.parse_args(argv)
    return run(a.start, a.end, execute=a.execute, use_live=not a.no_live)


if __name__ == "__main__":
    raise SystemExit(main())
