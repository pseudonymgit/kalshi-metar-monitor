#!/usr/bin/env python3
"""
reconcile_custody_gate_ledger.py — backfill the SETTLEMENT-admission audit
surfaces (`data/gate_ledger.db`, `data/trade_custody.db`) for settled trades
that were admitted at settlement but never routed through the admission-boundary
writers.

Context
-------
`core/booking_admission.admit_settled_trades()` is the single admission boundary
that (a) runs the full composed gate G0–G5 over every settled row and appends the
verdict to the append-only gate ledger, and (b) writes exactly one
TradeCustodyRecord per ADMITTED trade to the custody store. The historical replay
(`scripts/sbox_historical_replay.py`) settles rows in place and writes the
BOOKING-time substrate (the SBOX book's own `gate_ledger`/`trade_custody`
tables) but does not invoke the admission boundary, so the standalone audit
surfaces were left behind.

This driver is the remedy: it runs the same composed gate / candidate mapping /
custody writer as `admit_settled_trades`, but is IDEMPOTENT per row so a re-run is
safe:

  * a verdict is appended to the gate ledger only if that `trade_uuid` has no
    ADMITTED row yet (the raw ledger has no idempotency key; appending a verdict
    is an event, so an unguarded re-run would double-count);
  * a custody row is written via `record_custody_for_trade`, whose UNIQUE
    idempotency key makes a re-append a no-op.

Safety
------
* Deterministic. No AI/ML. No live trading.
* `trades` is opened read-only; no row of `trades` and no P&L is ever mutated.
* Refuses any trades DB whose path does not contain "sbox".
* Append-only: existing ledger/custody rows are never updated or deleted; the
  custody hash chain is extended, never rewritten.

Usage
-----
    python3 scripts/reconcile_custody_gate_ledger.py --dry-run
    python3 scripts/reconcile_custody_gate_ledger.py
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

from core.booking_admission import (  # noqa: E402
    GATE_DB_DEFAULT, CUSTODY_DB_DEFAULT, FROZEN_TRADE_VERSION,
    candidate_from_trades_row, ledger_of, record_custody_for_trade,
    load_finalization_anchors,
)
from core.canonical_settlement import load_canonical_snapshots  # noqa: E402
from core.trade_custody import verify_chain  # noqa: E402
from scripts.gate_composer import (  # noqa: E402
    compose_gate, VERDICT_ADMITTED,
)


def _count(db: Path, table: str) -> Optional[int]:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.Error:
        return None
    finally:
        c.close()


def _ledgered_uuids(gate_db: Path) -> set:
    c = sqlite3.connect(str(gate_db))
    try:
        return {r[0] for r in c.execute(
            "SELECT DISTINCT trade_uuid FROM gate_ledger WHERE verdict='ADMITTED'")}
    finally:
        c.close()


def reconcile(trades_db: Path, *, gate_db: Path, custody_db: Path,
              repo: Path = REPO, dry_run: bool = False) -> Dict[str, Any]:
    trades_db = Path(trades_db)
    if "sbox" not in str(trades_db).lower():
        raise SystemExit(f"refusing non-SBOX trades DB {trades_db}")

    metar_db = Path(repo) / "data" / "metar_backfill.db"
    canonical_map = load_canonical_snapshots(str(metar_db))
    finalization_map = load_finalization_anchors(str(metar_db))

    ro = sqlite3.connect(f"file:{trades_db}?mode=ro", uri=True)
    ro.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in ro.execute(
            "SELECT * FROM trades WHERE settlement_confirmed = 1 AND status = 'closed'")]
    finally:
        ro.close()

    already = _ledgered_uuids(gate_db)
    rec: Dict[str, Any] = {
        "settled_rows": len(rows), "admitted": 0, "custody_written": 0,
        "ledger_appended": 0, "skipped_already_ledgered": [], "excluded": [],
        "dry_run": dry_run,
    }

    led = None if dry_run else ledger_of(gate_db)
    for row in rows:
        tuuid = row["trade_uuid"]
        cand = candidate_from_trades_row(row, canonical_map=canonical_map,
                                         finalization_map=finalization_map)
        v = compose_gate(cand, Path(repo),
                         frozen_version=row.get("trade_version") or FROZEN_TRADE_VERSION,
                         suite_green=True)
        if v.verdict != VERDICT_ADMITTED:
            failed = next((c.gate for c in v.checks
                           if c.status in ("REJECT", "HALT")), None)
            rec["excluded"].append({"trade_uuid": tuuid, "verdict": v.verdict,
                                    "failed_gate": failed})
            continue
        rec["admitted"] += 1
        if tuuid in already:
            rec["skipped_already_ledgered"].append(tuuid)
            continue
        if dry_run:
            rec["ledger_appended"] += 1
            rec["custody_written"] += 1
            continue
        led.append(v)
        rec["ledger_appended"] += 1
        record_custody_for_trade(trades_db, tuuid, custody_db=custody_db)
        rec["custody_written"] += 1
    if led is not None:
        led.conn.close()

    # custody chain integrity (post-write)
    c = sqlite3.connect(f"file:{custody_db}?mode=ro", uri=True)
    ok, problems = verify_chain(c)
    c.close()
    rec["custody_chain_ok"] = ok
    rec["custody_chain_problems"] = problems[:5]
    return rec


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Backfill settlement-admission audit surfaces.")
    ap.add_argument("--trades-db", default=str(REPO / "data" / "paper_trading_sbox.db"))
    ap.add_argument("--gate-db", default=str(GATE_DB_DEFAULT))
    ap.add_argument("--custody-db", default=str(CUSTODY_DB_DEFAULT))
    ap.add_argument("--repo", default=str(REPO))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    gate_db, custody_db = Path(a.gate_db), Path(a.custody_db)
    before = {"gate_ledger": _count(gate_db, "gate_ledger"),
              "trade_custody": _count(custody_db, "trade_custody")}
    out = reconcile(Path(a.trades_db), gate_db=gate_db, custody_db=custody_db,
                    repo=Path(a.repo), dry_run=a.dry_run)
    after = {"gate_ledger": _count(gate_db, "gate_ledger"),
             "trade_custody": _count(custody_db, "trade_custody")}
    print(json.dumps({"before": before, "after": after, **out}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
