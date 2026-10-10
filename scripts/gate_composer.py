#!/usr/bin/env python3
"""
gate_composer.py — pre-ingest gate composer (GR5 §2, M6 core G0-G5).

One ordered filter for a trade candidate, cheapest-first:

  G0 suite-green     window may not open unless verify_gate is GREEN on the
                     frozen commit (structural + substrate gates). Window-level.
  G1 provenance      required identity fields non-null; trade_uuid unique;
                     trade_version == the frozen version hash.
  G2 fee-canonical   abs(stored_fee - kalshi_fee(qty, price)) <= $0.005.
  G3 settlement      settlement is FINAL and BINARY-DECIDABLE: settlement_confirmed=1
                     and a strike-crossing outcome in {correct, incorrect}, dates
                     sane. The book's settled_value is the settlement TEMPERATURE
                     (°F); G3 does NOT require it to be {0,1}. Derive the binary
                     with settled_binary() when a downstream consumer needs it.
  G4 axis-correct    label basis == strike-crossing; reject day-over-day.
  G5 as-of           branches by strategy class (2026-10-10 methodology ruling):
                     the INTRADAY family (B2 sweep, SBOX EV-strike bridge) bounds
                     every input at the trade's pinned entry instant (13:30
                     station-local -> tz-aware UTC) and REJECTS anything after the
                     decision; the OBSERVATIONAL/prior-day family keeps its date
                     boundary (obs date_utc < settlement_date). `settlement_date`
                     stays a DATE, never an as-of timestamp.

Verdict per candidate: ADMITTED | REJECTED | HALT, appended to an append-only
ledger. HALT is reserved for a window-level/structural failure (G0 or a
provenance version mismatch) — it stops the window; REJECTED quarantines the one
row. This REUSES core.fees (canonical fee), core.observational_asof_gate
(as-of predicate) and the frozen axis constant — it does not re-derive them.

Deterministic. No AI/ML.

Usage:
    from scripts.gate_composer import compose_gate, GateLedger
    python3 scripts/gate_composer.py --repo . [--json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_DEFAULT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_DEFAULT))

from core.fees import kalshi_fee, fee_per_contract  # noqa: E402
from core.observational_asof_gate import is_asof_safe, settlement_window_open  # noqa: E402
from core.station_time import parse_iso_utc, station_timezone_name  # noqa: E402

try:  # py3.9+
    from zoneinfo import ZoneInfo  # type: ignore
    _HAS_ZONEINFO = True
except Exception:  # pragma: no cover - stdlib fallback
    ZoneInfo = None  # type: ignore
    _HAS_ZONEINFO = False

# ─── frozen constants (aligned with A4 / verify_gate) ───────────────────────
AXIS_VALUE = "strike_crossing"
FEE_TOLERANCE = 0.005
TEMP_MIN_F = -60.0
TEMP_MAX_F = 150.0

VERDICT_ADMITTED = "ADMITTED"
VERDICT_REJECTED = "REJECTED"
VERDICT_HALT = "HALT"

# G0 depends on the two structural gates existing and passing.
GATE_SCRIPTS = ("scripts/verify_gate.py", "scripts/substrate_gate.py")

REQUIRED_IDENTITY = ("trade_uuid", "instance", "trade_version", "created_at_utc")
VALID_OUTCOMES = ("correct", "incorrect")

# ─── G5 as-of methodology (2026-10-10 ruling) ───────────────────────────────
# The intraday family decides at the observed high-so-far pinned to 13:30
# STATION-LOCAL (H3 pin; the B2 sweep and the SBOX EV-strike bridge both use it).
# That instant is load-bearing provenance: a bare run once silently defaulted to
# 13:00, so the gate verifies the candidate's own entry_time_utc against the pin.
INTRADAY_ENTRY_LOCAL_HHMM = (13, 30)
INTRADAY_FAMILIES = frozenset({"intraday", "intraday_ev_strike"})

# Input timestamps consulted for the intraday boundary when the candidate does not
# supply an explicit `input_timestamps` list.
INTRADAY_INPUT_FIELDS = ("snapshot_time_utc", "obs_max_source_ts",
                         "created_at_utc", "nwp_fetch_date")


@dataclass
class GateCheck:
    gate: str
    status: str              # PASS | REJECT | HALT
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {"gate": self.gate, "status": self.status, "detail": self.detail}


@dataclass
class GateVerdict:
    verdict: str
    checks: List[GateCheck] = field(default_factory=list)
    trade_uuid: Optional[str] = None
    candidate_hash: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "trade_uuid": self.trade_uuid,
            "candidate_hash": self.candidate_hash,
            "checks": [c.to_dict() for c in self.checks],
        }


def _candidate_hash(candidate: Dict[str, Any]) -> str:
    blob = json.dumps({k: candidate.get(k) for k in sorted(candidate)},
                      sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ─── Individual gates ───────────────────────────────────────────────────────
def gate_g0_suite_green(repo: Path, *, suite_green: Optional[bool] = None) -> GateCheck:
    """G0: window may not open unless the structural gates certify green.

    `suite_green` lets a caller pass a pre-computed verdict; when omitted the
    composer runs verify_gate + substrate_gate and treats exit 0 as green
    (exit 4 PROVISIONAL does not block the window on its own — it is surfaced).
    """
    if suite_green is None:
        import subprocess
        codes = {}
        for rel in GATE_SCRIPTS:
            script = repo / rel
            if not script.exists():
                return GateCheck("G0", "HALT", f"{rel} missing; cannot certify window")
            try:
                p = subprocess.run([sys.executable, str(script)], cwd=str(repo),
                                   capture_output=True, text=True, timeout=300)
            except (OSError, subprocess.SubprocessError) as exc:
                return GateCheck("G0", "HALT", f"{rel} failed to run: {exc}")
            codes[rel] = p.returncode
        if any(c == 3 or c == 2 for c in codes.values()):
            return GateCheck("G0", "HALT",
                             f"structural gate not green: {codes}")
        suite_green = all(c in (0, 4) for c in codes.values())
        detail = f"verify_gate/substrate_gate exit codes {codes}"
    else:
        detail = f"suite_green={suite_green} (caller-supplied)"
    return GateCheck("G0", "PASS" if suite_green else "HALT", detail)


def gate_g1_provenance(candidate: Dict[str, Any],
                       *, frozen_version: Optional[str] = None) -> GateCheck:
    """G1: required fields non-null; version == frozen hash (else HALT)."""
    missing = [k for k in REQUIRED_IDENTITY if candidate.get(k) in (None, "")]
    if missing:
        return GateCheck("G1", "REJECT", f"missing identity fields: {missing}")
    if frozen_version is not None and str(candidate.get("trade_version")) != str(frozen_version):
        return GateCheck("G1", "HALT",
                         f"trade_version {candidate.get('trade_version')!r} != "
                         f"frozen {frozen_version!r} (mid-window version change)")
    return GateCheck("G1", "PASS", "identity fields present; version pinned")


def gate_g2_fee_canonical(candidate: Dict[str, Any]) -> GateCheck:
    """G2: abs(stored_fee - kalshi_fee(qty, price)) <= $0.005."""
    stored = candidate.get("fee_total", candidate.get("trade_cost_fee"))
    qty = candidate.get("quantity")
    price = candidate.get("fill_price", candidate.get("trade_price"))
    if qty is None or price is None:
        return GateCheck("G2", "REJECT", "missing quantity/fill_price for fee check")
    canonical = kalshi_fee(int(qty), float(price))
    if stored is None:
        return GateCheck("G2", "REJECT", "no stored fee to reconcile")
    delta = abs(float(stored) - canonical)
    if delta > FEE_TOLERANCE:
        return GateCheck("G2", "REJECT",
                         f"fee mismatch: stored={stored} canonical={canonical:.4f} "
                         f"delta={delta:.4f} > {FEE_TOLERANCE}")
    return GateCheck("G2", "PASS", f"fee within ${FEE_TOLERANCE} (delta={delta:.4f})")


def settled_binary(candidate: Dict[str, Any]) -> Optional[int]:
    """Derive the binary settlement (1=correct, 0=incorrect) from the canonical
    strike-crossing outcome.

    The book stores `settled_value` as the settlement TEMPERATURE, never a binary,
    so the binary is DERIVED here for any downstream consumer that needs one. The
    stored temperature is never rewritten. Returns None when no valid outcome is
    present (callers must treat None as not-decided).
    """
    outcome = candidate.get("canonical_outcome") or candidate.get("settlement_outcome")
    if outcome not in VALID_OUTCOMES:
        return None
    return 1 if outcome == "correct" else 0


def gate_g3_settlement(candidate: Dict[str, Any]) -> GateCheck:
    """G3: settlement is FINAL and BINARY-DECIDABLE (temperature-aware).

    PASS iff `settlement_confirmed=1` AND a strike-crossing outcome in
    {correct, incorrect} is present AND the declared axis (when given) is
    strike-crossing (a day-over-day basis is not a strike-crossing settlement) AND
    the settlement date is sane.

    Reconciles the book's actual finality representation: the canonical correction
    writes `settled_value` as the settlement TEMPERATURE (°F) plus
    `canonical_outcome` (the strike-crossing win/loss), so the frozen `{0,1}`
    requirement on `settled_value` is dropped — it is the temperature, not a
    binary. Use `settled_binary()` to derive the binary when needed.
    """
    if candidate.get("settlement_axis") is not None:
        # canary/analytics rows may not be settlement rows; only gate trades
        pass
    confirmed = candidate.get("settlement_confirmed")
    # Canonical strike-crossing outcome takes precedence over the booked one: the
    # canonical correction is the strike-crossing label (core.canonical_settlement
    # .canonical_outcome), which is exactly the binary-decidable finality G3 wants.
    outcome = candidate.get("canonical_outcome") or candidate.get("settlement_outcome")
    axis = candidate.get("label_axis")
    sdate = candidate.get("settlement_date")
    if confirmed != 1:
        return GateCheck("G3", "REJECT", f"settlement_confirmed={confirmed!r} != 1")
    if outcome not in VALID_OUTCOMES:
        return GateCheck("G3", "REJECT",
                         f"outcome={outcome!r} not in {VALID_OUTCOMES} "
                         f"(settlement not binary-decidable)")
    if axis is not None and axis != AXIS_VALUE:
        return GateCheck("G3", "REJECT",
                         f"outcome axis={axis!r} != {AXIS_VALUE!r} (day-over-day basis)")
    if not sdate or len(str(sdate)) < 10:
        return GateCheck("G3", "REJECT", f"settlement_date={sdate!r} not sane")
    return GateCheck("G3", "PASS",
                     f"settlement confirmed, outcome={outcome!r} (binary={settled_binary(candidate)})")


def gate_g4_axis(candidate: Dict[str, Any]) -> GateCheck:
    """G4: label basis == strike-crossing; reject day-over-day."""
    axis = candidate.get("label_axis")
    if axis != AXIS_VALUE:
        return GateCheck("G4", "REJECT",
                         f"label_axis={axis!r} != {AXIS_VALUE!r} (day-over-day rejected)")
    return GateCheck("G4", "PASS", f"label_axis={AXIS_VALUE!r}")


def _pinned_entry_time_utc(station: Any, settlement_date: Any) -> Optional[datetime]:
    """13:30 station-local on `settlement_date` -> tz-aware UTC instant (or None).

    Station-specific: KNYC in EDT is 17:30Z; KSFO in PDT is 20:30Z. This is the
    intraday family's pinned decision instant.
    """
    if not station or not settlement_date or not _HAS_ZONEINFO:
        return None
    try:
        tz = ZoneInfo(station_timezone_name(str(station)))
        hh, mm = INTRADAY_ENTRY_LOCAL_HHMM
        local = datetime.fromisoformat(f"{str(settlement_date)[:10]}T{hh:02d}:{mm:02d}:00")
        return local.replace(tzinfo=tz).astimezone(timezone.utc)
    except Exception:
        return None


def _g5_observational(candidate: Dict[str, Any]) -> GateCheck:
    """Observational / prior-day family: every input ts < settlement_date T00:00:00Z.

    Unchanged by the 2026-10-10 ruling — this boundary is correct for a signal that
    reads the settlement day's own observation stream. Any candidate without an
    explicit intraday family falls here, so no unknown family opens a lookahead hole.
    """
    sdate = candidate.get("settlement_date")
    if not sdate:
        return GateCheck("G5", "REJECT", "no settlement_date for as-of check")
    sdate = str(sdate)[:10]
    inputs = candidate.get("input_timestamps")
    if inputs is None:
        inputs = [candidate.get("created_at_utc"), candidate.get("snapshot_time_utc"),
                  candidate.get("obs_max_source_ts"), candidate.get("nwp_fetch_date")]
    checked = 0
    for ts in inputs:
        if not ts:
            continue
        ts = str(ts)
        # nwp_fetch_date is a bare date; treat as that day's 00:00:00Z
        if len(ts) == 10:
            ts = f"{ts}T00:00:00"
        if not is_asof_safe(ts, sdate):
            return GateCheck("G5", "REJECT",
                             f"as-of violation: input {ts} >= "
                             f"{settlement_window_open(sdate)}")
        checked += 1
    return GateCheck("G5", "PASS", f"{checked} input timestamp(s) all pre-window")


def _g5_intraday(candidate: Dict[str, Any]) -> GateCheck:
    """Intraday family: bound every input at the pinned entry instant.

    PASS iff every input ts <= entry_time_utc AND entry_time_utc < finalization_utc.
    The pinned entry time is 13:30 station-local -> UTC; a candidate whose
    entry_time_utc does not equal that pin is a provenance mismatch -> REJECT.
    """
    station = candidate.get("station")
    sdate = candidate.get("settlement_date")
    if not station or not sdate:
        return GateCheck("G5", "REJECT",
                         "intraday: no station/settlement_date for entry-instant check")
    sdate = str(sdate)[:10]
    pinned = _pinned_entry_time_utc(station, sdate)
    if pinned is None:
        return GateCheck("G5", "REJECT",
                         f"intraday: cannot resolve 13:30 local for station={station!r} "
                         f"on {sdate}")

    entry = parse_iso_utc(candidate.get("entry_time_utc"))
    if entry is None:
        return GateCheck("G5", "REJECT",
                         "intraday: missing/unparseable entry_time_utc (decision instant)")
    if entry != pinned:
        return GateCheck("G5", "REJECT",
                         f"provenance: entry_time_utc {entry.isoformat()} != pinned "
                         f"13:30-local {pinned.isoformat()} for {station} {sdate}")

    final = parse_iso_utc(candidate.get("finalization_utc")
                          or candidate.get("settled_at_utc"))
    if final is None:
        return GateCheck("G5", "REJECT",
                         "intraday: missing finalization_utc "
                         "(settlement_snapshot.settled_at_utc)")
    if not (entry < final):
        return GateCheck("G5", "REJECT",
                         f"decision after finalization: entry {entry.isoformat()} "
                         f">= final {final.isoformat()}")

    inputs = candidate.get("input_timestamps")
    if inputs is None:
        inputs = [candidate.get(k) for k in INTRADAY_INPUT_FIELDS]
    checked = 0
    for raw in inputs:
        if not raw:
            continue
        ts = parse_iso_utc(raw)
        if ts is None:
            return GateCheck("G5", "REJECT", f"intraday: unparseable input ts {raw!r}")
        if ts > entry:
            return GateCheck("G5", "REJECT",
                             f"post-decision input {ts.isoformat()} > "
                             f"entry {entry.isoformat()}")
        checked += 1
    return GateCheck("G5", "PASS",
                     f"intraday: {checked} input(s) <= entry {entry.isoformat()} "
                     f"< finalization {final.isoformat()}")


def gate_g5_asof(candidate: Dict[str, Any]) -> GateCheck:
    """G5: as-of boundary branches by strategy class (2026-10-10 ruling)."""
    family = candidate.get("signal_family")
    if family in INTRADAY_FAMILIES:
        return _g5_intraday(candidate)
    return _g5_observational(candidate)


# ─── Composer ───────────────────────────────────────────────────────────────
def compose_gate(candidate: Dict[str, Any], repo: Path = REPO_DEFAULT,
                 *, frozen_version: Optional[str] = None,
                 suite_green: Optional[bool] = None) -> GateVerdict:
    """Run G0-G5 in order and return the composed verdict.

    Precedence: any HALT wins; else any REJECT -> REJECTED; else ADMITTED.
    """
    repo = Path(repo)
    checks: List[GateCheck] = [
        gate_g0_suite_green(repo, suite_green=suite_green),
        gate_g1_provenance(candidate, frozen_version=frozen_version),
        gate_g2_fee_canonical(candidate),
        gate_g3_settlement(candidate),
        gate_g4_axis(candidate),
        gate_g5_asof(candidate),
    ]
    if any(c.status == "HALT" for c in checks):
        verdict = VERDICT_HALT
    elif any(c.status == "REJECT" for c in checks):
        verdict = VERDICT_REJECTED
    else:
        verdict = VERDICT_ADMITTED
    return GateVerdict(verdict=verdict, checks=checks,
                       trade_uuid=candidate.get("trade_uuid"),
                       candidate_hash=_candidate_hash(candidate))


# ─── Append-only ledger ─────────────────────────────────────────────────────
class GateLedger:
    """Append-only verdict ledger. Rows are immutable (UPDATE/DELETE abort)."""

    TABLE = "gate_ledger"

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.ensure_schema()

    def ensure_schema(self) -> None:
        self.conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {self.TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                logged_at_utc TEXT NOT NULL,
                trade_uuid TEXT,
                candidate_hash TEXT,
                verdict TEXT NOT NULL,
                failed_gate TEXT,
                detail TEXT
            )""")
        self.conn.execute(
            f"""CREATE TRIGGER IF NOT EXISTS trg_{self.TABLE}_no_update
                BEFORE UPDATE ON {self.TABLE}
                BEGIN SELECT RAISE(ABORT, '{self.TABLE} is append-only'); END""")
        self.conn.execute(
            f"""CREATE TRIGGER IF NOT EXISTS trg_{self.TABLE}_no_delete
                BEFORE DELETE ON {self.TABLE}
                BEGIN SELECT RAISE(ABORT, '{self.TABLE} is append-only'); END""")
        self.conn.commit()

    def append(self, v: GateVerdict, *, logged_at_utc: Optional[str] = None) -> None:
        failed = next((c.gate for c in v.checks
                       if c.status in ("REJECT", "HALT")), None)
        detail = "; ".join(f"{c.gate}:{c.status}:{c.detail}"
                           for c in v.checks if c.status != "PASS")
        self.conn.execute(
            f"INSERT INTO {self.TABLE} (logged_at_utc, trade_uuid, candidate_hash, "
            f"verdict, failed_gate, detail) VALUES (?,?,?,?,?,?)",
            (logged_at_utc or datetime.now(timezone.utc).isoformat(),
             v.trade_uuid, v.candidate_hash, v.verdict, failed, detail))
        self.conn.commit()


# ─── Self-check ─────────────────────────────────────────────────────────────
def _self_check(repo: Path) -> int:
    """Run the composer against a synthetic candidate set + report the verdicts."""
    good = {
        "trade_uuid": "u1", "instance": "SBOX", "trade_version": "v3.1_sbox",
        "created_at_utc": "2026-10-09T12:00:00Z",
        "settlement_date": "2026-10-10", "quantity": 100, "fill_price": 0.50,
        "fee_total": kalshi_fee(100, 0.50), "settlement_confirmed": 1,
        "settlement_outcome": "correct", "canonical_outcome": "correct",
        "settled_value": 72.0,  # settlement TEMPERATURE (book), not a binary
        "label_axis": AXIS_VALUE, "snapshot_time_utc": "2026-10-09T12:00:00Z",
    }
    bad_fee = dict(good, trade_uuid="u2", fee_total=20.0)
    bad_axis = dict(good, trade_uuid="u3", label_axis="day_over_day")
    bad_asof = dict(good, trade_uuid="u4", snapshot_time_utc="2026-10-10T01:00:00Z")
    loose = dict(good, trade_uuid="u5", trade_version="v9_mismatch")
    # Intraday family (KNYC 2026-10-04 EDT): pinned entry 17:30Z, final 05:00Z next day.
    intraday = dict(good, trade_uuid="u6", instance="B2", station="KNYC",
                    signal_family="intraday", settlement_date="2026-10-04",
                    snapshot_time_utc="2026-10-04T17:30:00Z",
                    created_at_utc="2026-10-04T17:00:00Z",
                    entry_time_utc="2026-10-04T17:30:00Z",
                    finalization_utc="2026-10-05T05:00:00Z")
    intraday_leak = dict(intraday, trade_uuid="u7",
                         snapshot_time_utc="2026-10-04T18:00:00Z")

    out = []
    for c in (good, bad_fee, bad_axis, bad_asof, loose, intraday, intraday_leak):
        v = compose_gate(c, repo, frozen_version="v3.1_sbox", suite_green=True)
        out.append(v)
    led = GateLedger(sqlite3.connect(":memory:"))
    for v in out:
        led.append(v)
    n = led.conn.execute("SELECT COUNT(*) FROM gate_ledger").fetchone()[0]
    for v in out:
        print(f"  {v.trade_uuid}: {v.verdict} "
              f"({[c.gate for c in v.checks if c.status != 'PASS'] or 'all pass'})")
    print(f"ledger rows: {n}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Pre-ingest gate composer G0-G5.")
    ap.add_argument("--repo", default=str(REPO_DEFAULT))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    return _self_check(Path(args.repo))


if __name__ == "__main__":
    sys.exit(main())
