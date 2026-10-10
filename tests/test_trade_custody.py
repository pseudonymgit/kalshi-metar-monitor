#!/usr/bin/env python3
"""
D1 — TradeCustodyRecord (TCR) tests.

Locks the GR5 §1 invariants:
  - 48 columns in 7 groups.
  - a row can be inserted;
  - a row cannot be UPDATEd or DELETEd (append-only triggers);
  - the hash chain links (prev_hash == prior record_hash, record_hash recomputes);
  - idempotency_key = sha256(instance|market_ticker|decision_ts) is UNIQUE;
  - the Merkle manifest is deterministic and its root covers every leaf.
"""
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from core.trade_custody import (  # noqa: E402
    COLUMNS, COLUMN_GROUPS, GROUP_ORDER, DATA_COLUMNS, GENESIS_HASH,
    append_custody_record, build_merkle_manifest, column_count,
    compute_record_hash, create_trade_custody_schema, idempotency_key,
    verify_chain,
)


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    create_trade_custody_schema(c)
    yield c
    c.close()


def _rec(**over):
    base = {
        "trade_uuid": "uuid-1", "instance": "SBOX",
        "trade_version": "v3.1_sbox", "git_commit": "deadbeef",
        "market_ticker": "KXHIGHKATL-26OCT10-T80",
        "snapshot_time_utc": "2026-10-09T12:00:00Z",
        "mid": 0.42, "price_source": "kalshi_market_snapshots.db",
        "p_gefs": 0.55, "p_analog": 0.50, "p_trajectory": 0.53, "p_fused": 0.54,
        "decision_ts_utc": "2026-10-09T12:00:00Z",
        "entry_price": 0.42, "fill_price": 0.42, "net_ev": 0.12,
    }
    base.update(over)
    return base


def test_schema_is_48_columns_in_7_groups():
    assert column_count() == 48
    assert len(GROUP_ORDER) == 7
    assert sum(len(COLUMN_GROUPS[g]) for g in GROUP_ORDER) == 48
    assert len(COLUMNS) == len(set(COLUMNS)) == 48


def test_insert_row(conn):
    h = append_custody_record(conn, _rec())
    assert len(h) == 64
    row = conn.execute("SELECT custody_seq, prev_hash, record_hash, "
                       "idempotency_key FROM trade_custody").fetchone()
    assert row[0] == 1
    assert row[1] == GENESIS_HASH  # first row links to genesis
    assert row[2] == h
    assert row[3] == idempotency_key("SBOX", "KXHIGHKATL-26OCT10-T80",
                                     "2026-10-09T12:00:00Z")


def test_update_is_refused(conn):
    append_custody_record(conn, _rec())
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE trade_custody SET mid=0.99 WHERE custody_id=1")
    # value unchanged
    assert conn.execute("SELECT mid FROM trade_custody WHERE custody_id=1"
                        ).fetchone()[0] == 0.42


def test_delete_is_refused(conn):
    append_custody_record(conn, _rec())
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM trade_custody WHERE custody_id=1")
    assert conn.execute("SELECT COUNT(*) FROM trade_custody").fetchone()[0] == 1


def test_idempotency_key_unique(conn):
    append_custody_record(conn, _rec())
    with pytest.raises(sqlite3.IntegrityError):
        append_custody_record(conn, _rec())  # same instance|ticker|decision_ts


def test_hash_chain_links(conn):
    h1 = append_custody_record(conn, _rec(trade_uuid="u1"))
    h2 = append_custody_record(conn, _rec(trade_uuid="u2",
                                          market_ticker="KXHIGHKATL-26OCT10-T82",
                                          decision_ts_utc="2026-10-09T12:05:00Z"))
    rows = conn.execute("SELECT custody_seq, prev_hash, record_hash "
                        "FROM trade_custody ORDER BY custody_id").fetchall()
    assert rows[0][1] == GENESIS_HASH and rows[0][2] == h1
    assert rows[1][1] == h1 and rows[1][2] == h2  # links
    ok, problems = verify_chain(conn)
    assert ok, problems


def test_verify_chain_detects_tamper(conn):
    append_custody_record(conn, _rec())
    append_custody_record(conn, _rec(trade_uuid="u2",
                                     market_ticker="KXHIGHKATL-26OCT10-T82",
                                     decision_ts_utc="2026-10-09T12:05:00Z"))
    # Forge a row directly (bypassing the API) with a wrong stored hash.
    conn.execute("DROP TRIGGER trg_trade_custody_no_update")
    conn.execute("UPDATE trade_custody SET record_hash=? WHERE custody_id=2",
                 ("f" * 64,))
    ok, problems = verify_chain(conn)
    assert not ok
    assert any("record_hash mismatch" in p for p in problems)


def test_merkle_manifest_deterministic(conn, tmp_path):
    for i in range(3):
        append_custody_record(conn, _rec(
            trade_uuid=f"u{i}", market_ticker=f"KXHIGHKATL-26OCT10-T8{i}",
            decision_ts_utc=f"2026-10-09T12:0{i}:00Z"))
    m1 = build_merkle_manifest(conn, str(tmp_path / "a"), instance="SBOX")
    m2 = build_merkle_manifest(conn, str(tmp_path / "b"), instance="SBOX")
    assert m1["row_count"] == 3
    assert m1["merkle_root"] == m2["merkle_root"]
    assert m1["chain_ok"] is True
    # manifest sidecar + ndjson + sha256 all written
    for ext in (".custody.ndjson", ".custody.manifest.json", ".custody.sha256"):
        assert (tmp_path / "a" / f"SBOX{ext}").exists()


def test_compute_record_hash_reproducible():
    r = _rec()
    a = compute_record_hash(GENESIS_HASH, r)
    b = compute_record_hash(GENESIS_HASH, dict(r))
    assert a == b and len(a) == 64
