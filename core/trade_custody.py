#!/usr/bin/env python3
"""
trade_custody.py — TradeCustodyRecord (TCR): append-only chain of custody.

GR5 §1 (docs/plans/GRAY-ROOM-5-SYNTHESIS-2026-10-09.md). There is no chain of
custody from snapshot -> bridge -> trade -> settlement, so a settled P&L cannot
be traced back to the inputs that produced it. This module defines the
`trade_custody` table, its immutability triggers, a deterministic hash chain,
and a git-committable Merkle manifest.

Table: `trade_custody`, exactly **48 columns in 7 groups**:

  A Identity/ordering (9)  custody_id, trade_uuid, custody_seq, instance,
                           trade_version, git_commit, config_hash,
                           created_at_utc, writer_host
  B Snapshot          (7)  snapshot_id, market_ticker, snapshot_time_utc,
                           yes_bid, yes_ask, mid, price_source
  C Observation       (5)  obs_max_source_ts, obs_ingestion_ts, asof_boundary,
                           asof_pass, nwp_fetch_date
  D Bridge            (8)  p_gefs, p_analog, p_trajectory, p_fused, fusion_rule,
                           n_analog, agreement, traj_mult
  E Decision/fee      (7)  decision_ts_utc, fee_model, fee_per_contract,
                           entry_price, fill_price, edge_net, net_ev
  F Settlement        (7)  settlement_truth_key, kalshi_temp_f, strike_price,
                           strike_type, settlement_confirmed, settlement_outcome,
                           label_axis
  G Integrity         (5)  record_hash, prev_hash, idempotency_key, disposition,
                           quarantine_reason

Integrity rules:
- BEFORE UPDATE / BEFORE DELETE triggers RAISE(ABORT): rows are immutable.
- `idempotency_key = sha256(instance|market_ticker|decision_ts)` is UNIQUE.
- `record_hash = sha256(prev_hash || canonical_json(payload))`; the first row's
  prev_hash is GENESIS_HASH (64 zeros). `verify_chain()` walks rows in
  custody_id order and recomputes every link.
- `build_merkle_manifest()` writes newline-delimited canonical JSON + a manifest
  sidecar with the Merkle root, for git commit *outside* the DB.

Deterministic. No AI/ML.

Usage:
    from core.trade_custody import (
        create_trade_custody_schema, append_custody_record, verify_chain,
        build_merkle_manifest, idempotency_key, COLUMN_GROUPS,
    )
    create_trade_custody_schema(conn)
    append_custody_record(conn, {...})
    assert verify_chain(conn)[0]
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ─── The frozen 48-column / 7-group schema ──────────────────────────────────
# (name, sqlite type). Order is the group order and the canonical column order.
COLUMN_GROUPS: "Dict[str, List[Tuple[str, str]]]" = {
    # A Identity/ordering
    "identity": [
        ("custody_id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("trade_uuid", "TEXT NOT NULL"),
        ("custody_seq", "INTEGER NOT NULL"),
        ("instance", "TEXT NOT NULL"),
        ("trade_version", "TEXT"),
        ("git_commit", "TEXT"),
        ("config_hash", "TEXT"),
        ("created_at_utc", "TEXT NOT NULL"),
        ("writer_host", "TEXT"),
    ],
    # B Snapshot
    "snapshot": [
        ("snapshot_id", "TEXT"),
        ("market_ticker", "TEXT"),
        ("snapshot_time_utc", "TEXT"),
        ("yes_bid", "REAL"),
        ("yes_ask", "REAL"),
        ("mid", "REAL"),
        ("price_source", "TEXT"),
    ],
    # C Observation
    "observation": [
        ("obs_max_source_ts", "TEXT"),
        ("obs_ingestion_ts", "TEXT"),
        ("asof_boundary", "TEXT"),
        ("asof_pass", "INTEGER"),
        ("nwp_fetch_date", "TEXT"),
    ],
    # D Bridge probabilities
    "bridge": [
        ("p_gefs", "REAL"),
        ("p_analog", "REAL"),
        ("p_trajectory", "REAL"),
        ("p_fused", "REAL"),
        ("fusion_rule", "TEXT"),
        ("n_analog", "INTEGER"),
        ("agreement", "REAL"),
        ("traj_mult", "REAL"),
    ],
    # E Decision / fee
    "decision": [
        ("decision_ts_utc", "TEXT NOT NULL"),
        ("fee_model", "TEXT"),
        ("fee_per_contract", "REAL"),
        ("entry_price", "REAL"),
        ("fill_price", "REAL"),
        ("edge_net", "REAL"),
        ("net_ev", "REAL"),
    ],
    # F Settlement
    "settlement": [
        ("settlement_truth_key", "TEXT"),
        ("kalshi_temp_f", "REAL"),
        ("strike_price", "REAL"),
        ("strike_type", "TEXT"),
        ("settlement_confirmed", "INTEGER"),
        ("settlement_outcome", "TEXT"),
        ("label_axis", "TEXT"),
    ],
    # G Integrity
    "integrity": [
        ("record_hash", "TEXT"),
        ("prev_hash", "TEXT"),
        ("idempotency_key", "TEXT"),
        ("disposition", "TEXT"),
        ("quarantine_reason", "TEXT"),
    ],
}

# Canonical column order = identity, snapshot, observation, bridge, decision,
# settlement, integrity. custody_id is DB-assigned; record_hash is derived.
GROUP_ORDER = ("identity", "snapshot", "observation", "bridge", "decision",
               "settlement", "integrity")
COLUMNS: List[str] = [name for g in GROUP_ORDER for name, _ in COLUMN_GROUPS[g]]
DATA_COLUMNS: List[str] = [c for c in COLUMNS
                           if c not in ("custody_id", "record_hash")]
HASHED_COLUMNS: List[str] = [c for c in DATA_COLUMNS]

GENESIS_HASH = "0" * 64
MANIFEST_VERSION = 1

_TABLE = "trade_custody"


# ─── Deterministic canonicalization ─────────────────────────────────────────
def _canon_number(x: float) -> str:
    """Fixed-precision decimal string for a float (chain must be reproducible)."""
    return format(float(x), ".10f")


def canonical_payload(record: Dict[str, Any]) -> str:
    """Canonical JSON of a record's hashed columns (sorted keys, fixed floats).

    None is emitted as JSON null. Floats are emitted as fixed-precision decimal
    strings so the hash cannot drift on repr differences across platforms.
    """
    def norm(v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, bool):
            return bool(v)
        if isinstance(v, float):
            return _canon_number(v)
        if isinstance(v, int):
            return v
        return str(v)

    payload = {c: norm(record.get(c)) for c in HASHED_COLUMNS}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def compute_record_hash(prev_hash: str, record: Dict[str, Any]) -> str:
    """sha256(prev_hash || canonical_json(payload))."""
    h = hashlib.sha256()
    h.update((prev_hash or GENESIS_HASH).encode("utf-8"))
    h.update(canonical_payload(record).encode("utf-8"))
    return h.hexdigest()


def idempotency_key(instance: str, market_ticker: str, decision_ts: str) -> str:
    """sha256(instance|market_ticker|decision_ts) — the UNIQUE idempotency key."""
    raw = f"{instance}|{market_ticker}|{decision_ts}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ─── Schema + immutability triggers ─────────────────────────────────────────
def create_trade_custody_schema(conn) -> None:
    """Create `trade_custody`, its UNIQUE idempotency index, and immutability
    triggers. Idempotent."""
    cols_sql = ",\n    ".join(f"{n} {t}" for n, t in
                              [(n, t) for g in GROUP_ORDER
                               for n, t in COLUMN_GROUPS[g]])
    conn.execute(f"CREATE TABLE IF NOT EXISTS {_TABLE} (\n    {cols_sql}\n)")
    conn.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS ux_{_TABLE}_idem "
        f"ON {_TABLE}(idempotency_key)"
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_uuid ON {_TABLE}(trade_uuid)"
    )
    # Immutability: any UPDATE or DELETE aborts the statement.
    conn.execute(
        f"""CREATE TRIGGER IF NOT EXISTS trg_{_TABLE}_no_update
            BEFORE UPDATE ON {_TABLE}
            BEGIN SELECT RAISE(ABORT, '{_TABLE} is append-only (UPDATE refused)'); END"""
    )
    conn.execute(
        f"""CREATE TRIGGER IF NOT EXISTS trg_{_TABLE}_no_delete
            BEFORE DELETE ON {_TABLE}
            BEGIN SELECT RAISE(ABORT, '{_TABLE} is append-only (DELETE refused)'); END"""
    )
    conn.commit()


def _table_exists(conn) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_TABLE,)
    ).fetchone()
    return row is not None


# ─── Append ─────────────────────────────────────────────────────────────────
def _last_hash(conn) -> str:
    row = conn.execute(
        f"SELECT record_hash FROM {_TABLE} ORDER BY custody_id DESC LIMIT 1"
    ).fetchone()
    if row is None or row[0] is None:
        return GENESIS_HASH
    return row[0]


def _next_seq(conn, trade_uuid: str) -> int:
    row = conn.execute(
        f"SELECT COALESCE(MAX(custody_seq), 0) FROM {_TABLE} WHERE trade_uuid=?",
        (trade_uuid,),
    ).fetchone()
    return int(row[0]) + 1


def append_custody_record(conn, record: Dict[str, Any],
                          *, ensure_schema: bool = True) -> str:
    """Append one custody row, computing seq/idempotency/hash chain.

    `record` supplies any of the DATA_COLUMNS; missing ones stay NULL.
    Required: trade_uuid, instance, decision_ts_utc (used for idempotency), and
    market_ticker for a non-empty idempotency key. Returns the new record_hash.

    Raises sqlite3.IntegrityError on a duplicate idempotency key (idempotent
    re-append is the caller's responsibility to pre-check).
    """
    if ensure_schema and not _table_exists(conn):
        create_trade_custody_schema(conn)

    rec: Dict[str, Any] = {c: record.get(c) for c in DATA_COLUMNS}
    rec["trade_uuid"] = record.get("trade_uuid")
    rec["instance"] = record.get("instance")
    rec["custody_seq"] = record.get("custody_seq") or _next_seq(
        conn, str(record.get("trade_uuid")))
    if not rec.get("created_at_utc"):
        rec["created_at_utc"] = datetime.now(timezone.utc).isoformat()
    if not rec.get("writer_host"):
        rec["writer_host"] = socket.gethostname()
    if not rec.get("decision_ts_utc"):
        rec["decision_ts_utc"] = rec["created_at_utc"]

    rec["idempotency_key"] = idempotency_key(
        str(rec.get("instance")), str(rec.get("market_ticker")),
        str(rec.get("decision_ts_utc")),
    )
    prev = _last_hash(conn)
    rec["prev_hash"] = prev
    rec["record_hash"] = compute_record_hash(prev, rec)

    insert_cols = [c for c in COLUMNS if c != "custody_id"]
    placeholders = ",".join("?" for _ in insert_cols)
    conn.execute(
        f"INSERT INTO {_TABLE} ({','.join(insert_cols)}) VALUES ({placeholders})",
        [rec.get(c) for c in insert_cols],
    )
    conn.commit()
    return rec["record_hash"]


# ─── Verify ─────────────────────────────────────────────────────────────────
def verify_chain(conn) -> Tuple[bool, List[str]]:
    """Recompute the hash chain over all rows in custody_id order.

    Returns (ok, problems). A problem is a broken link or a record_hash that
    does not recompute from the stored payload. Does not raise on a malformed
    row; it reports it.
    """
    problems: List[str] = []
    if not _table_exists(conn):
        return False, ["trade_custody table missing"]
    cur = conn.execute(
        f"SELECT {','.join(DATA_COLUMNS)}, record_hash FROM {_TABLE} "
        f"ORDER BY custody_id ASC"
    )
    expected_prev = GENESIS_HASH
    seen = 0
    for row in cur.fetchall():
        vals = dict(zip(DATA_COLUMNS, row[:len(DATA_COLUMNS)]))
        stored_hash = row[len(DATA_COLUMNS)]
        cid = vals.get("idempotency_key")
        if vals.get("prev_hash") != expected_prev:
            problems.append(
                f"{cid}: prev_hash {str(vals.get('prev_hash'))[:12]} != "
                f"expected {expected_prev[:12]}"
            )
        recomputed = compute_record_hash(vals.get("prev_hash") or GENESIS_HASH,
                                         vals)
        if recomputed != stored_hash:
            problems.append(
                f"{cid}: record_hash mismatch (stored {str(stored_hash)[:12]}, "
                f"recomputed {recomputed[:12]})"
            )
        expected_prev = stored_hash
        seen += 1
    if seen == 0:
        problems.append("no custody rows")
    return (len(problems) == 0), problems


# ─── Merkle manifest (git-committed outside the DB) ─────────────────────────
def _merkle_root(leaves: List[str]) -> str:
    if not leaves:
        return GENESIS_HASH
    layer = list(leaves)
    while len(layer) > 1:
        if len(layer) % 2 == 1:
            layer.append(layer[-1])  # duplicate odd tail (deterministic)
        layer = [hashlib.sha256((layer[i] + layer[i + 1]).encode()).hexdigest()
                 for i in range(0, len(layer), 2)]
    return layer[0]


def build_merkle_manifest(conn, out_dir: str, *, instance: str = "SBOX",
                          generated_at: Optional[str] = None) -> Dict[str, Any]:
    """Write ndjson custody export + manifest sidecar to `out_dir`.

    Returns the manifest dict. Writes:
      <instance>.custody.ndjson       canonical JSON, one row per line
      <instance>.custody.manifest.json  {row_count, merkle_root, chain_ok, ...}
      <instance>.custody.sha256       sha256 of each of the two files
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rows = conn.execute(
        f"SELECT {','.join(DATA_COLUMNS)} FROM {_TABLE} ORDER BY custody_id ASC"
    ).fetchall()
    leaves: List[str] = []
    lines: List[str] = []
    for row in rows:
        rec = dict(zip(DATA_COLUMNS, row))
        lines.append(canonical_payload(rec))
        leaves.append(str(rec.get("record_hash")))

    ndjson_path = out / f"{instance}.custody.ndjson"
    ndjson_path.write_text("\n".join(lines) + ("\n" if lines else ""),
                           encoding="utf-8")
    ndjson_sha = hashlib.sha256(ndjson_path.read_bytes()).hexdigest()

    ok, problems = verify_chain(conn)
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "instance": instance,
        "generated_at_utc": generated_at or datetime.now(timezone.utc).isoformat(),
        "row_count": len(rows),
        "first_custody_id": (conn.execute(
            f"SELECT MIN(custody_id) FROM {_TABLE}").fetchone()[0]),
        "last_custody_id": (conn.execute(
            f"SELECT MAX(custody_id) FROM {_TABLE}").fetchone()[0]),
        "leaf_count": len(leaves),
        "merkle_root": _merkle_root(leaves),
        "chain_ok": ok,
        "chain_problems": problems,
        "ndjson_sha256": ndjson_sha,
    }
    manifest_path = out / f"{instance}.custody.manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()

    (out / f"{instance}.custody.sha256").write_text(
        f"{ndjson_sha}  {ndjson_path.name}\n"
        f"{manifest_sha}  {manifest_path.name}\n",
        encoding="utf-8",
    )
    return manifest


# ─── Adapter: map a `trades` row into the TCR groups ────────────────────────
def record_from_trade(conn, trade: Dict[str, Any], *, instance: str = "SBOX",
                      git_commit: Optional[str] = None,
                      config_hash: Optional[str] = None,
                      git_commit_resolver=None) -> str:
    """Boundary-evidence map from a recorded trade dict to a custody row.

    Group A is filled from the trade + git/env context. Groups B-F are filled
    where the trade already carries the value (snapshot, observation, bridge,
    decision/fee, settlement); absent fields stay NULL until a later settle
    stage appends the settlement boundary. This is the writer the bridge calls
    at trade-record time (wiring tracked separately).
    """
    if git_commit is None and git_commit_resolver is not None:
        try:
            git_commit = git_commit_resolver()
        except Exception:
            git_commit = None

    rec = {
        # A Identity/ordering
        "trade_uuid": trade.get("trade_uuid"),
        "instance": instance or trade.get("instance"),
        "trade_version": trade.get("trade_version"),
        "git_commit": git_commit,
        "config_hash": config_hash,
        # B Snapshot
        "market_ticker": trade.get("market_ticker") or trade.get("ticker"),
        "snapshot_time_utc": trade.get("snapshot_time_utc"),
        "mid": trade.get("market_price") or trade.get("mid_price"),
        "price_source": trade.get("price_source"),
        # C Observation
        "asof_pass": trade.get("asof_pass"),
        "nwp_fetch_date": trade.get("nwp_fetch_date"),
        # D Bridge
        "p_gefs": trade.get("p_gefs"),
        "p_analog": trade.get("p_analog"),
        "p_trajectory": trade.get("p_trajectory"),
        "p_fused": trade.get("p_fused"),
        "agreement": trade.get("agreement"),
        "traj_mult": trade.get("traj_mult"),
        # E Decision/fee
        "decision_ts_utc": trade.get("created_at_utc") or trade.get("trade_date_utc"),
        "entry_price": trade.get("trade_price") or trade.get("entry_price"),
        "fill_price": trade.get("trade_price") or trade.get("fill_price"),
        "net_ev": trade.get("realized_pnl"),
        # F Settlement
        "strike_price": trade.get("strike_price"),
        "settlement_confirmed": trade.get("settlement_confirmed"),
        "settlement_outcome": trade.get("settlement_outcome"),
        "label_axis": trade.get("label_axis"),
    }
    return append_custody_record(conn, rec)


def column_count() -> int:
    """48 — asserted by the test suite."""
    return len(COLUMNS)


if __name__ == "__main__":  # smoke: build schema against a temp DB
    import sqlite3
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        c = sqlite3.connect(os.path.join(d, "t.db"))
        create_trade_custody_schema(c)
        print("columns:", column_count())
        print("groups:", {g: len(COLUMN_GROUPS[g]) for g in GROUP_ORDER})
        assert column_count() == 48, column_count()
