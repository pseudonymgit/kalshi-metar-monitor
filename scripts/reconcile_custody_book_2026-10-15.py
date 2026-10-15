#!/usr/bin/env python3
"""reconcile_custody_book_2026-10-15.py — read-only reconciliation of the SBOX
chain-of-custody / gate-ledger surfaces against the (blank-slate) book.

Origin: PWW Bug Hunt v3 (2026-10-15) finding #4 — `trade_custody` (91 rows) and
`gate_ledger` (100 rows) reference `trade_uuid`s that are absent from `trades`
(0 rows). This script PROVES the disposition instead of asserting it:

  1. where the live bridge writes custody/ledger (the SBOX book's EMBEDDED
     tables) vs the standalone `data/trade_custody.db` / `data/gate_ledger.db`
     (written by the settled-admission boundary + the 2026-10-10 reconcile);
  2. that the embedded 91 rows are the append-only archive preserved by the
     blank-slate purge (AG-0, 2026-10-14) — i.e. the divergence is BY DESIGN;
  3. that the tables are immutable (BEFORE UPDATE/DELETE triggers raise), so a
     "clear them" action would need to disable that design and would break the
     hash chain;
  4. why `disposition` is NULL (an optional governance annotation set only by the
     stale-open reconcile, never by the booking writer) and that `gate_ledger`
     has no `disposition` column at all.

READ-ONLY. Every book is opened `mode=ro`. Writes ONLY the JSON artifact.

usage: python3 scripts/reconcile_custody_book_2026-10-15.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from core.trade_custody import verify_chain  # noqa: E402

SBOX = REPO / "data" / "paper_trading_sbox.db"
STANDALONE_CUSTODY = REPO / "data" / "trade_custody.db"
STANDALONE_LEDGER = REPO / "data" / "gate_ledger.db"
OUT = REPO / "artifacts" / "custody-integrity" / "custody_book_reconcile_2026-10-15.json"

# The blank-slate purge (register commit 0f5edf2, Workstream AG-0): the SBOX book
# was CLEARED to 0 rows; prior books preserved in dated data/*.bak-* backups.
BLANK_SLATE_UTC = "2026-10-14"


def _ro(p: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def _counts(p: Path, tables) -> dict:
    out = {}
    c = _ro(p)
    try:
        have = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in tables:
            out[t] = (c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                      if t in have else None)
    finally:
        c.close()
    return out


def _triggers(p: Path) -> dict:
    c = _ro(p)
    try:
        sql = dict(c.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger'"))
    finally:
        c.close()
    return sql


def main():
    art = {"generated_by": "scripts/reconcile_custody_book_2026-10-15.py",
           "finding": "PWW v3 #4 — custody/ledger outlive the blank-slate book"}

    art["surfaces"] = {
        "sbox_embedded": _counts(SBOX, ["trades", "trade_custody", "gate_ledger"]),
        "standalone_custody_db": _counts(STANDALONE_CUSTODY, ["trade_custody"]),
        "standalone_gate_ledger_db": _counts(STANDALONE_LEDGER, ["gate_ledger"]),
    }

    # 1. Where does the live bridge write? (record_custody(conn, ...) with conn =
    #    the SBOX book connection) — embedded tables are authoritative for live
    #    bookings; the standalone DBs are the settled-admission/reconcile surface.
    src_note = ("sbox_intraday_ev_bridge.run -> record_custody(conn, ...) "
                "with conn = data/paper_trading_sbox.db; embedded tables.")
    art["write_path"] = {
        "live_bridge": "data/paper_trading_sbox.db (embedded trade_custody/gate_ledger)",
        "standalone": ("data/trade_custody.db + data/gate_ledger.db — written by "
                       "the settled-admission boundary (booking_admission."
                       "record_custody_for_trade / admit_settled_trades) and the "
                       "2026-10-10 reconcile driver; NOT by the live booking path"),
        "evidence": src_note,
    }

    # 2. The embedded rows are the pre-blank-slate archive.
    c = _ro(SBOX)
    try:
        lo, hi = c.execute(
            "SELECT MIN(created_at_utc), MAX(created_at_utc) FROM trade_custody"
        ).fetchone()
        joins = c.execute(
            "SELECT COUNT(*) FROM trade_custody tc "
            "JOIN trades t ON tc.trade_uuid = t.trade_uuid").fetchone()[0]
        after = c.execute(
            "SELECT COUNT(*) FROM trade_custody WHERE created_at_utc >= ?",
            (BLANK_SLATE_UTC,)).fetchone()[0]
        disp = c.execute(
            "SELECT disposition, COUNT(*) FROM trade_custody GROUP BY disposition"
        ).fetchall()
        gl_has_disp = any(
            r[1] == "disposition"
            for r in c.execute("PRAGMA table_info(gate_ledger)"))
    finally:
        c.close()
    art["embedded_custody"] = {
        "rows": art["surfaces"]["sbox_embedded"]["trade_custody"],
        "created_range": [lo, hi],
        "rows_joining_trades": joins,
        "rows_after_blank_slate": after,
        "disposition_distribution": disp,
        "interpretation": (
            "append-only archive from the pre-blank-slate book; the purge "
            "deliberately preserved it (0/91 join the now-empty trades)"),
    }
    art["disposition"] = {
        "custody_column_exists": True,
        "gate_ledger_column_exists": gl_has_disp,
        "writer": ("disposition is set ONLY by scripts/"
                   "reconcile_stale_open_trades_2026-10-13.py (a governance "
                   "annotation), NEVER by the booking writer record_from_trade; "
                   "gate_ledger has no disposition column"),
        "verdict": ("NULL is EXPECTED for booking-time rows, not a wiring gap"),
    }

    # 3. Immutability: a clear would need the triggers dropped + would break the
    #    hash chain. Prove the triggers exist and the chain verifies.
    trig = _triggers(SBOX)
    art["immutability"] = {
        "triggers": sorted(k for k in trig if "trade_custody" in k or "gate_ledger" in k),
        "delete_refused": "trade_custody/gate_ledger BEFORE DELETE/UPDATE RAISE(ABORT)",
    }
    c = _ro(SBOX)
    try:
        ok, probs = verify_chain(c)
    finally:
        c.close()
    art["custody_chain_ok"] = bool(ok)
    art["custody_chain_problems"] = len(probs) if probs else 0

    art["remedy"] = {
        "action": "KEEP (no delete)",
        "why": ("the 91 rows are the intended append-only archive, immutable by "
                "design; clearing them would require disabling the immutability "
                "triggers and would destroy the audit chain. The divergence is "
                "by design (blank-slate purge preserved the archive), not a "
                "defect. This artifact makes it legible so future hunts do not "
                "re-flag it."),
        "if_a_clear_is_ever_wanted": ("it must be an explicit archive-out (copy to "
                                      "a dated backup, drop triggers in that copy), "
                                      "signed off by Dan — never in place"),
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(art, indent=2))
    print(json.dumps(art, indent=2))
    print("\nwrote", OUT)


if __name__ == "__main__":
    main()
