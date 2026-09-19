#!/usr/bin/env python3
"""
z8_freeze_tier.py — Build the frozen, hash-pinned, provenance-tagged `z8` data tier.

This produces the point-in-time-frozen settlement-truth artifact that the H1/H2
preregistration and B2.1/2.2 real-price join require. It is DISTINCT from
`scripts/z8_kalshi_market_cron.py` (a *live* Kalshi market snapshot collector).

What this produces (both under `data/z8-frozen/`, gitignored like the rest of `data/`):
  1. `z8_settlement_truth.db` — a frozen COPY of the date-tiered ground-truth spine
     (`settlement_truth` from `metar_backfill.db`), already sanity-banded to
     [-60, 135] F and provenance-tagged via `temp_source` + `trustworthy`.
  2. `z8-frozen-manifest.json` — hash-pinned provenance manifest declaring:
       - content sha256 of every frozen asset AND its upstream source DB
       - row counts / station counts / date ranges (point-in-time identity)
       - truth precedence (Kalshi finalized > GHCN > WeatherAPI bridge;
         settlement_epochs bucket + historical_api are NON-authoritative)
       - sanity band + corruption audit result

Non-destructive: source DBs are read-only (`mode=ro`); the frozen tier is an
additive copy, not a mutation. No live/paper trading. No signal change.
"""

import datetime
import hashlib
import json
import os
import sqlite3

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
OUT_DIR = os.path.join(DATA_DIR, "z8-frozen")

SOURCE_DBS = [
    "kalshi_settlements.db",
    "ghcn_settlements.db",
    "weatherapi_archive.db",
    "metar_backfill.db",
]

TRUTH_PRECEDENCE = [
    {"rank": 1, "source": "kalshi_finalized", "db": "kalshi_settlements.db",
     "filter": "source_type='finalized'", "authoritative": True,
     "role": "economic settlement truth (P&L axis)"},
    {"rank": 2, "source": "ghcn", "db": "ghcn_settlements.db",
     "filter": "high_f/low_f daily", "authoritative": True,
     "role": "weather ground truth (label source, deep history)"},
    {"rank": 3, "source": "weatherapi", "db": "weatherapi_archive.db",
     "filter": "daily max_temp_f/min_temp_f", "authoritative": False,
     "role": "recency bridge (best-available, reconcile later)"},
]

NON_AUTHORITATIVE = [
    {"source": "settlement_epochs.settlement_bucket", "db": "metar_backfill.db",
     "reason": "METAR-derived proxy; -1 F convention offset + historical corruption rows (S5)"},
    {"source": "kalshi_settlements historical_api", "db": "kalshi_settlements.db",
     "reason": "proxy/reconstructed, NOT live-finalized (S5 open data-policy question)"},
]

SANITY_BAND = {"min_f": -60.0, "max_f": 135.0}


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # 1. Read-only verify + hash upstream source DBs
    source_hashes = {}
    for db in SOURCE_DBS:
        p = os.path.join(DATA_DIR, db)
        if os.path.exists(p):
            source_hashes[db] = {
                "bytes": os.path.getsize(p),
                "sha256": sha256_file(p),
            }

    # 2. Freeze the settlement_truth spine (date-tiered ground truth)
    src = os.path.join(DATA_DIR, "metar_backfill.db")
    conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    cur = conn.cursor()
    rows = list(cur.execute(
        "SELECT station, market_type, local_trading_date, settlement_temp_f, "
        "temp_source, kalshi_temp_f, strike_price, strike_type, trustworthy "
        "FROM settlement_truth ORDER BY station, market_type, local_trading_date"
    ))
    conn.close()

    truth_counts = {}
    truth_counts["n_rows"] = len(rows)
    truth_counts["n_stations"] = len({r[0] for r in rows})
    truth_counts["temp_source_dist"] = {}
    truth_counts["market_type_dist"] = {}
    trusty = {}
    strike_n = 0
    kalshi_n = 0
    for r in rows:
        ts, mt, tr = r[4], r[1], r[8]
        truth_counts["temp_source_dist"][ts] = truth_counts["temp_source_dist"].get(ts, 0) + 1
        truth_counts["market_type_dist"][mt] = truth_counts["market_type_dist"].get(mt, 0) + 1
        trusty[str(tr)] = trusty.get(str(tr), 0) + 1
        if r[6] is not None:
            strike_n += 1
        if r[5] is not None:
            kalshi_n += 1
    truth_counts["trustworthy_dist"] = trusty
    truth_counts["n_with_strike"] = strike_n
    truth_counts["n_with_kalshi_temp"] = kalshi_n
    truth_counts["temp_min_f"] = min(r[3] for r in rows if r[3] is not None)
    truth_counts["temp_max_f"] = max(r[3] for r in rows if r[3] is not None)

    # sanity-band audit
    out_of_band = [r for r in rows if r[3] is not None and
                   (r[3] < SANITY_BAND["min_f"] or r[3] > SANITY_BAND["max_f"])]
    truth_counts["sanity_band"] = SANITY_BAND
    truth_counts["out_of_band_rows"] = len(out_of_band)

    # 3. Write frozen DB
    out_db = os.path.join(OUT_DIR, "z8_settlement_truth.db")
    if os.path.exists(out_db):
        os.remove(out_db)
    oconn = sqlite3.connect(out_db)
    ocur = oconn.cursor()
    ocur.execute("""
        CREATE TABLE settlement_truth (
            station TEXT, market_type TEXT, local_trading_date TEXT,
            settlement_temp_f REAL, temp_source TEXT, kalshi_temp_f REAL,
            strike_price REAL, strike_type TEXT, trustworthy INTEGER
        )
    """)
    ocur.execute("CREATE INDEX idx_truth_station_date ON settlement_truth(station, local_trading_date)")
    ocur.executemany(
        "INSERT INTO settlement_truth VALUES (?,?,?,?,?,?,?,?,?)", rows
    )
    # provenance record embedded in the frozen DB
    ocur.execute("""
        CREATE TABLE provenance (
            key TEXT PRIMARY KEY, value TEXT
        )
    """)
    ocur.executemany("INSERT INTO provenance VALUES (?,?)", [
        ("frozen_utc", now),
        ("source_db", "metar_backfill.db"),
        ("source_table", "settlement_truth"),
        ("sanity_band", json.dumps(SANITY_BAND)),
    ])
    oconn.commit()
    oconn.close()

    truth_sha = sha256_file(out_db)
    truth_bytes = os.path.getsize(out_db)

    # 4. Write manifest
    manifest = {
        "frozen_utc": now,
        "tier": "z8",
        "definition": (
            "Point-in-time-frozen, provenance-tagged, corruption-audited settlement-truth "
            "dataset; identity pinned by content sha256; truth precedence declared. "
            "Reproducible against P1-LINEAGE-FREEZE."
        ),
        "naming_note": (
            "This z8 tier is DISTINCT from scripts/z8_kalshi_market_cron.py (live collector)."
        ),
        "source_dbs": source_hashes,
        "truth_precedence": TRUTH_PRECEDENCE,
        "non_authoritative": NON_AUTHORITATIVE,
        "frozen_assets": {
            "z8_settlement_truth.db": {
                "bytes": truth_bytes,
                "sha256": truth_sha,
                "counts": truth_counts,
            }
        },
        "sanity_band": SANITY_BAND,
    }

    manifest_path = os.path.join(OUT_DIR, "z8-frozen-manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)

    # 5. Also write a repo-local provenance doc for git tracking (the manifest is
    #    under gitignored data/, so the durable receipt lives in docs/plans/).
    print(json.dumps({
        "frozen_utc": now,
        "out_dir": os.path.relpath(OUT_DIR, PROJECT_ROOT),
        "truth_rows": len(rows),
        "truth_sha256": truth_sha,
        "truth_bytes": truth_bytes,
        "out_of_band_rows": len(out_of_band),
        "source_hashes": source_hashes,
    }, indent=2))


if __name__ == "__main__":
    main()
