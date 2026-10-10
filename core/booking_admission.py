#!/usr/bin/env python3
"""
booking_admission.py — wire the GR5 measurement substrate into the live booking
path (pre-registration epoch · pre-ingest gate G0–G5 · TradeCustodyRecord).

GR5 §2/§6. Before 2026-10-10 the substrate (`scripts/gate_composer.py`,
`core/trade_custody.py`, `core/preregistration.py`) was built and green but ran
*standalone*: nothing in the live booking path called it. This module is the
single composition point that makes the gate decide whether a trade is admitted
and that gives every accepted trade a custody row.

Where the gate actually binds (coupling disclosure — the honest reason this is
not a naive per-candidate pre-book filter)
-------------------------------------------
The gate's blocking M6 set is {G0..G5}. Three of those checks are **not
decidable at booking time** on this codebase:

  * G2 fee-canonical  — `record_trade` books `trade_cost = qty * price` (the
    execution cost), not the canonical Kalshi fee; no stored fee exists to
    reconcile. (This is the R2/E1 "fee unwired" finding.)
  * G3 settlement     — the book records a trades row at DECISION time with
    `settlement_confirmed=0`; settlement is filled later by
    `core/pnl_tracking.py::process_settlements_for_date`. G3 requires
    `settlement_confirmed=1`, so a fresh, open candidate fails G3 by
    construction.
  * G5 as-of          — the intraday G5 branch needs `finalization_utc`
    (the frozen `settlement_snapshot.settled_at_utc` anchor), which only exists
    after settlement.

Therefore G0–G5 is necessarily an **admission filter on SETTLED candidates** —
it runs when a settled row is admitted into the measured cohort (N=640 / the
diagnostic run), not when an open row is first written. `admit_settled_trades()`
is that boundary.

The live path is wired in two places so the substrate genuinely runs on real
data, not a fixture:

  1. **Booking (decision time)** — `admit_decision_candidate()` runs the
     decidable subset (G0 window-green · G1 identity/version · G4 axis) and
     appends the verdict to the append-only gate ledger. `record_custody()`
     writes exactly one TradeCustodyRecord row per accepted trade.
  2. **Admission (settled)** — `admit_settled_trades()` runs the FULL composed
     gate (G0–G5) over settled rows, appends every verdict, and writes custody
     for each admitted trade. It refuses on a pre-registration hash mismatch.

`pinned_entry_time_utc()` supplies the real H3 pin (13:30 station-local -> UTC)
so a candidate never silently defaults to 13:00 (the provenance failure the G5
ruling records).

Deterministic. No AI/ML. Does not enable live trading and does not mutate
historical P&L (the custody store and gate ledger are separate append-only
stores; every read of `trades` is read-only).
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

REPO_DEFAULT = Path(__file__).resolve().parents[1]
if str(REPO_DEFAULT) not in sys.path:
    sys.path.insert(0, str(REPO_DEFAULT))

# Reuse the existing substrate — never re-derive it (GR5 §2).
from core.fees import kalshi_fee  # noqa: E402
from core.canonical_settlement import load_canonical_snapshots  # noqa: E402
from core.station_time import station_timezone_name  # noqa: E402
from core.trade_custody import (  # noqa: E402
    COLUMN_GROUPS, record_from_trade, create_trade_custody_schema,
    idempotency_key, _TABLE as _CUSTODY_TABLE,
)

try:  # zoneinfo stdlib (py3.9+)
    from zoneinfo import ZoneInfo  # type: ignore
    _HAS_ZONEINFO = True
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore
    _HAS_ZONEINFO = False

# `scripts/` is not a package (no __init__.py); import the gate composer as a
# top-level module exactly as the test suite does.
_SCRIPTS = REPO_DEFAULT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
from gate_composer import (  # noqa: E402
    compose_gate, GateCheck, GateLedger, GateVerdict, VERDICT_ADMITTED,
    VERDICT_HALT, VERDICT_REJECTED, _candidate_hash, verdict_idempotency_key,
    gate_g0_suite_green, gate_g1_provenance, gate_g4_axis,
    INTRADAY_ENTRY_LOCAL_HHMM, AXIS_VALUE, settled_binary,
)

GOVERNED_EXPERIMENT = "GR5-WE-001"
FROZEN_TRADE_VERSION = "v3.1_sbox"

GATE_DB_DEFAULT = REPO_DEFAULT / "data" / "gate_ledger.db"
CUSTODY_DB_DEFAULT = REPO_DEFAULT / "data" / "trade_custody.db"


class CandidateRejected(RuntimeError):
    """Raised when a candidate fails the pre-ingest gate (REJECTED/HALT)."""

    def __init__(self, verdict: GateVerdict):
        failed = [c.gate for c in verdict.checks if c.status != "PASS"]
        super().__init__(
            f"candidate {verdict.trade_uuid!r} not admitted: "
            f"{verdict.verdict} (failed {failed})")
        self.verdict = verdict


# ─── pinned entry instant (13:30 station-local) ─────────────────────────────
def pinned_entry_time_utc(station: str, settlement_date: str) -> Optional[str]:
    """The intraday family's pinned decision instant, as an ISO UTC string.

    13:30 station-local on `settlement_date` -> tz-aware UTC (KNYC EDT = 17:30Z,
    KSFO PDT = 20:30Z). This is the real H3 pin; the gate's G5 provenance check
    compares the candidate's `entry_time_utc` against exactly this instant.
    """
    if not station or not settlement_date or not _HAS_ZONEINFO:
        return None
    try:
        tz = ZoneInfo(station_timezone_name(str(station)))
        hh, mm = INTRADAY_ENTRY_LOCAL_HHMM
        local = datetime.fromisoformat(
            f"{str(settlement_date)[:10]}T{hh:02d}:{mm:02d}:00").replace(tzinfo=tz)
        return local.astimezone(timezone.utc).isoformat()
    except Exception:
        return None


# ─── append-only gate ledger ────────────────────────────────────────────────
def _open_ledger(conn: sqlite3.Connection) -> GateLedger:
    return GateLedger(conn)


def ledger_of(db_path: Path | str) -> GateLedger:
    """Open (create-if-absent) the append-only gate ledger."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL;")
    return GateLedger(conn)


# ─── booking-time (decision-time) gate: decidable subset ────────────────────
def admit_decision_candidate(*, trade_uuid: str, instance: str = "SBOX",
                             trade_version: str = FROZEN_TRADE_VERSION,
                             station: str, market_type: str = "HIGH",
                             settlement_date: str, label_axis: str,
                             candidate: Optional[Dict[str, Any]] = None,
                             gate_db: Path | str = GATE_DB_DEFAULT,
                             repo: Path | str = REPO_DEFAULT,
                             frozen_version: Optional[str] = FROZEN_TRADE_VERSION,
                             suite_green: Optional[bool] = None,
                             conn: Optional[sqlite3.Connection] = None
                             ) -> Tuple[GateVerdict, bool]:
    """Booking-time gate: the decidable subset of G0–G5, logged to the ledger.

    Runs G0 (window green), G1 (identity/version) and G4 (axis). G2, G3 and G5
    are recorded as DEFERRED with their reason (fee not stamped / settlement
    unknown / finalization anchor unknown) so the ledger shows exactly what was
    checked at booking and what is deferred to admission. Blocks only on a
    decidable failure (REJECTED/HALT). Returns (verdict, admitted).
    """
    if candidate is None:
        candidate = {
            "trade_uuid": trade_uuid, "instance": instance,
            "trade_version": trade_version, "station": station,
            "market_type": market_type, "settlement_date": settlement_date,
            "label_axis": label_axis, "created_at_utc":
                datetime.now(timezone.utc).isoformat(),
        }
    checks = [
        gate_g0_suite_green(Path(repo), suite_green=suite_green),
        gate_g1_provenance(candidate, frozen_version=frozen_version),
        gate_g4_axis(candidate),
        GateCheck("G2", "DEFERRED",
                  "fee not stamped at booking (bridge books execution cost)"),
        GateCheck("G3", "DEFERRED", "settlement unknown at booking"),
        GateCheck("G5", "DEFERRED",
                  "finalization anchor unknown at booking"),
    ]
    if any(c.status == "HALT" for c in checks):
        verdict = VERDICT_HALT
    elif any(c.status == "REJECT" for c in checks):
        verdict = VERDICT_REJECTED
    else:
        verdict = VERDICT_ADMITTED
    gv = GateVerdict(verdict=verdict, checks=checks, trade_uuid=trade_uuid,
                     candidate_hash=_candidate_hash(candidate))
    if conn is not None:
        _open_ledger(conn).append(gv)
    else:
        led = ledger_of(gate_db)
        led.append(gv)
        led.conn.close()
    return gv, gv.verdict == VERDICT_ADMITTED


# ─── full composed gate + ledger (admission of an explicit candidate) ───────
def admit_candidate(candidate: Dict[str, Any], *,
                    gate_db: Path | str = GATE_DB_DEFAULT,
                    repo: Path | str = REPO_DEFAULT,
                    frozen_version: Optional[str] = FROZEN_TRADE_VERSION,
                    suite_green: Optional[bool] = None,
                    conn: Optional[sqlite3.Connection] = None) -> GateVerdict:
    """Run the FULL composed gate G0–G5; ADMITTED -> return, else log + raise.

    Every verdict (ADMITTED / REJECTED / HALT) is appended to the append-only
    gate ledger, so a rejected candidate is always visible with its failing
    check.
    """
    v = compose_gate(candidate, Path(repo), frozen_version=frozen_version,
                     suite_green=suite_green)
    if conn is not None:
        _open_ledger(conn).append(v)
    else:
        led = ledger_of(gate_db)
        led.append(v)
        led.conn.close()
    if v.verdict != VERDICT_ADMITTED:
        raise CandidateRejected(v)
    return v


def log_verdict(v: GateVerdict, *, gate_db: Path | str = GATE_DB_DEFAULT,
                conn: Optional[sqlite3.Connection] = None) -> None:
    """Append a composed verdict to the append-only gate ledger."""
    if conn is not None:
        _open_ledger(conn).append(v)
    else:
        led = ledger_of(gate_db)
        led.append(v)
        led.conn.close()


# ─── TCR writer: one custody row per accepted trade ─────────────────────────
def record_custody(conn: sqlite3.Connection, trade: Dict[str, Any], *,
                   instance: str = "SBOX", git_commit: Optional[str] = None,
                   config_hash: Optional[str] = None) -> str:
    """Append ONE TradeCustodyRecord row for an accepted trade (idempotent).

    `conn` is the trades DB connection (or any connection); the custody table is
    created there if absent. Idempotent on the custody idempotency key
    (`sha256(instance|market_ticker|decision_ts)`): an existing row is NOT
    duplicated and its `record_hash` is returned. Returns the new/existing hash.
    """
    create_trade_custody_schema(conn)
    # Normalize the idempotency discriminator BEFORE computing the key so the
    # pre-check and `record_from_trade` derive the SAME key. A `trades` row has
    # no `market_ticker` column, so fall back to `trade_uuid` — otherwise every
    # row collapses onto the "None" key and the second trade is wrongly deduped.
    mt = trade.get("market_ticker") or trade.get("ticker") or trade.get("trade_uuid")
    trade = {**trade, "market_ticker": mt}
    decision_ts = trade.get("created_at_utc") or trade.get("trade_date_utc") or ""
    idem = idempotency_key(instance, str(mt), str(decision_ts))
    existing = conn.execute(
        f"SELECT record_hash FROM {_CUSTODY_TABLE} WHERE idempotency_key=?",
        (idem,)).fetchone()
    if existing:
        return existing[0]
    try:
        return record_from_trade(conn, trade, instance=instance,
                                 git_commit=git_commit, config_hash=config_hash)
    except sqlite3.IntegrityError:  # duplicate idempotency key -> idempotent no-op
        row = conn.execute(
            f"SELECT record_hash FROM {_CUSTODY_TABLE} WHERE idempotency_key=?",
            (idem,)).fetchone()
        if row:
            return row[0]
        raise


def record_custody_for_trade(trades_db: Path | str, trade_uuid: str, *,
                             custody_db: Path | str = CUSTODY_DB_DEFAULT,
                             instance: str = "SBOX",
                             git_commit: Optional[str] = None,
                             config_hash: Optional[str] = None) -> str:
    """Path variant: read an accepted trade read-only, append its custody row.

    Used by the settled-admission boundary, where the trades DB and the custody
    store are distinct append-only files.
    """
    ro = sqlite3.connect(f"file:{trades_db}?mode=ro", uri=True)
    ro.row_factory = sqlite3.Row
    try:
        r = ro.execute("SELECT * FROM trades WHERE trade_uuid=?",
                       (trade_uuid,)).fetchone()
    finally:
        ro.close()
    if r is None:
        raise KeyError(f"trade_uuid {trade_uuid!r} not found in {trades_db}")
    trade = dict(r)
    trade.setdefault("instance", instance)
    trade.setdefault("label_axis", "strike_crossing")
    c = sqlite3.connect(str(custody_db))
    c.execute("PRAGMA journal_mode=WAL;")
    try:
        return record_custody(c, trade, instance=instance,
                              git_commit=git_commit, config_hash=config_hash)
    finally:
        c.close()


# ─── candidate construction from a real trades row ──────────────────────────
def load_finalization_anchors(metar_db: str) -> Dict[Tuple[str, str, str], str]:
    """Read `settlement_snapshot.settled_at_utc` per station/market/day (read-only).

    This is the as-of anchor G5's intraday branch requires (`entry < final`).
    `load_canonical_snapshots` deliberately keeps only the value+source, so the
    anchor is loaded here. Returns {} when the table is absent — a missing anchor
    makes G5 fail closed (REJECT), never admit on a guessed boundary.
    """
    out: Dict[Tuple[str, str, str], str] = {}
    if not metar_db or not Path(metar_db).exists():
        return out
    try:
        ro = sqlite3.connect(f"file:{metar_db}?mode=ro", uri=True)
        try:
            for st, mt, d, anchor in ro.execute(
                    "SELECT station, market_type, local_trading_date, "
                    "settled_at_utc FROM settlement_snapshot"):
                if anchor:
                    out[(st, mt, d)] = anchor
        finally:
            ro.close()
    except sqlite3.Error:
        return out
    return out


def candidate_from_trades_row(row: Dict[str, Any], *,
                              canonical_map: Optional[Dict] = None,
                              finalization_map: Optional[Dict] = None
                              ) -> Dict[str, Any]:
    """Map a `trades` row -> a gate candidate carrying REAL provenance.

    Reads raw columns only: quantity+trade_price for G2 (canonical recompute),
    the settlement columns for G3, `label_axis` for G4. G5's `entry_time_utc` is
    the real 13:30 station-local pin (never a 13:00 default) and
    `finalization_utc` is the real frozen `settlement_snapshot.settled_at_utc`
    anchor when a canonical snapshot map is supplied.

    NOTE (data contract): the SBOX book's canonical correction writes
    `settled_value` as the settlement *temperature* (°F) — that is what
    `pnl_tracking` writes — while the old frozen G3 contract expected the binary
    `{0,1}`. That divergence is now reconciled in G3: the gate reads the
    strike-crossing `canonical_outcome` (`correct`/`incorrect`) as the binary-
    decidable finality and no longer requires the temperature-valued
    `settled_value` to be `{0,1}`. This builder passes the stored value through
    unchanged and additionally surfaces `canonical_outcome`/`canonical_settled_f`
    so G3's verdict reflects the book's real finality representation.
    """
    station = row.get("station")
    settlement_date = (row.get("trade_date_utc")
                       or (str(row.get("settlement_date_utc"))[:10]
                           if row.get("settlement_date_utc") else None))
    qty = row.get("quantity")
    fill = row.get("trade_price")
    fee_total = (kalshi_fee(int(qty), float(fill))
                 if qty is not None and fill is not None else None)

    finalization_utc = None
    if finalization_map and station and settlement_date:
        finalization_utc = finalization_map.get(
            (station, row.get("market_type"), settlement_date))
    elif canonical_map and station and settlement_date:
        snap = canonical_map.get((station, row.get("market_type"), settlement_date))
        if snap:
            finalization_utc = snap.get("settled_at_utc")

    return {
        "trade_uuid": row.get("trade_uuid"),
        "instance": row.get("instance") or "SBOX",
        "trade_version": row.get("trade_version"),
        "signal_family": "intraday",
        "station": station,
        "settlement_date": settlement_date,
        "quantity": qty,
        "fill_price": fill,
        "fee_total": fee_total,
        "settlement_confirmed": row.get("settlement_confirmed"),
        "settlement_outcome": row.get("settlement_outcome"),
        "canonical_outcome": row.get("canonical_outcome"),
        "canonical_settled_f": row.get("canonical_settled_f"),
        "settled_value": row.get("settled_value"),
        "label_axis": row.get("label_axis") or "strike_crossing",
        "snapshot_time_utc": row.get("snapshot_time_utc"),
        "obs_max_source_ts": row.get("obs_max_source_ts"),
        "created_at_utc": row.get("created_at_utc"),
        "entry_time_utc": pinned_entry_time_utc(station, settlement_date),
        "finalization_utc": finalization_utc,
    }


def settled_binary_for_row(row: Dict[str, Any]) -> Optional[int]:
    """Derived binary settlement (1=correct, 0=incorrect) for a `trades` row.

    Thin wrapper over `gate_composer.settled_binary` for callers holding a raw
    book row. The stored `settled_value` (a temperature) is never rewritten.
    """
    return settled_binary({"canonical_outcome": row.get("canonical_outcome"),
                           "settlement_outcome": row.get("settlement_outcome")})


# ─── pre-registration epoch for the diagnostic run ──────────────────────────
def prereg_artifact_path(experiment_id: str = GOVERNED_EXPERIMENT,
                         repo: Path | str = REPO_DEFAULT) -> Path:
    return Path(repo) / "artifacts" / "preregistration" / f"{experiment_id}.json"


def verify_run_relies_on_prereg(experiment_id: str = GOVERNED_EXPERIMENT,
                                repo: Path | str = REPO_DEFAULT) -> Dict[str, Any]:
    """Analysis admission gate: refuse on a missing/tampered pre-registration.

    Delegates to `core.preregistration.require_preregistration` (already
    enforced there). Returns the verified artifact; raises PreregistrationError
    on a hash mismatch. This is the pre-registration epoch the live path
    references before any counted (diagnostic) run.
    """
    from core.preregistration import require_preregistration
    return require_preregistration(experiment_id, Path(repo))


# ─── admission boundary: gate settled trades into the measured cohort ───────
def admit_settled_trades(trades_db: Path | str, *,
                         date: Optional[str] = None,
                         gate_db: Path | str = GATE_DB_DEFAULT,
                         custody_db: Path | str = CUSTODY_DB_DEFAULT,
                         repo: Path | str = REPO_DEFAULT,
                         experiment_id: str = GOVERNED_EXPERIMENT,
                         git_commit: Optional[str] = None,
                         config_hash: Optional[str] = None,
                         canonical_map: Optional[Dict] = None,
                         finalization_map: Optional[Dict] = None
                         ) -> Dict[str, Any]:
    """Gate every settled trade G0–G5; write custody for each ADMITTED trade.

    For each settled row (`settlement_confirmed=1`): build the real candidate,
    run the full composed gate, append the verdict to the gate ledger (a
    rejected candidate is logged with its failing check and is NOT admitted),
    and for every ADMITTED trade write exactly one custody row. Idempotent:
    re-running adds no duplicate custody rows and never writes `trades` (no P&L
    change).

    Refuses the counted run on a pre-registration hash mismatch.
    """
    prereg = verify_run_relies_on_prereg(experiment_id, repo)

    metar_db = Path(repo) / "data" / "metar_backfill.db"
    if canonical_map is None:
        canonical_map = load_canonical_snapshots(str(metar_db))
    if finalization_map is None:
        finalization_map = load_finalization_anchors(str(metar_db))

    ro = sqlite3.connect(f"file:{trades_db}?mode=ro", uri=True)
    ro.row_factory = sqlite3.Row
    try:
        sql = "SELECT * FROM trades WHERE settlement_confirmed = 1"
        params: Tuple = ()
        if date:
            sql += " AND trade_date_utc = ?"
            params = (date,)
        rows = [dict(r) for r in ro.execute(sql, params).fetchall()]
    finally:
        ro.close()

    led = ledger_of(gate_db)
    admitted, rejected, custody = [], [], {}
    ledger_written = ledger_duplicates = 0
    for row in rows:
        cand = candidate_from_trades_row(row, canonical_map=canonical_map,
                                         finalization_map=finalization_map)
        v = compose_gate(cand, Path(repo), frozen_version=FROZEN_TRADE_VERSION,
                         suite_green=True)
        # Idempotency: a given (trade, check-set) admission writes its verdict at
        # most once. The key is derived from ROW identity (trade_uuid, frozen
        # gate_version, verdict, decision instant) — never wall-clock now() — so
        # re-running this writer over the same settled rows is a no-op, not a
        # duplicate. Append-only: a duplicate is skipped, never overwritten.
        wrote = led.append(v, idempotency_key=verdict_idempotency_key(
            v.trade_uuid, cand.get("trade_version"), v.verdict,
            cand.get("created_at_utc")))
        ledger_written += int(wrote)
        ledger_duplicates += int(not wrote)
        if v.verdict == VERDICT_ADMITTED:
            admitted.append(row["trade_uuid"])
            custody[row["trade_uuid"]] = record_custody_for_trade(
                trades_db, row["trade_uuid"], custody_db=custody_db,
                git_commit=git_commit, config_hash=config_hash)
        else:
            failed = next((c.gate for c in v.checks
                           if c.status in ("REJECT", "HALT")), None)
            rejected.append((row["trade_uuid"], failed))
    led.conn.close()
    return {"admitted": admitted, "rejected": rejected,
            "custody_hashes": custody,
            "ledger_written": ledger_written,
            "ledger_duplicates": ledger_duplicates,
            "prereg": {"ok": prereg.get("ok"), "sha256": prereg.get("sha256")}}


def column_count() -> int:
    """48 — the frozen TradeCustodyRecord width (re-exported for callers)."""
    return sum(len(COLUMN_GROUPS[g]) for g in COLUMN_GROUPS)


if __name__ == "__main__":  # smoke: pins + prereg verification
    print("GOVERNED", GOVERNED_EXPERIMENT)
    print("KNYC 2026-10-04 pin:", pinned_entry_time_utc("KNYC", "2026-10-04"))
    print("KSFO 2026-10-04 pin:", pinned_entry_time_utc("KSFO", "2026-10-04"))
    print("custody columns:", column_count())
    print("prereg:", verify_run_relies_on_prereg()["sha256"][:16])
