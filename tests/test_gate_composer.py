#!/usr/bin/env python3
"""
Pre-ingest gate composer (G0-G5, M6 core) tests.

Locks GR5 §2:
  - a clean candidate is ADMITTED;
  - a fee off by >$0.005 is REJECTED (G2) and the canonical fee is reused;
  - a day-over-day axis is REJECTED (G4);
  - a lookahead input is REJECTED (G5);
  - a version mismatch is HALT (G1) and G0 failure is HALT;
  - verdicts are appended to an append-only ledger (UPDATE/DELETE refused).
"""
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from core.fees import kalshi_fee  # noqa: E402
from gate_composer import (  # noqa: E402
    AXIS_VALUE, VERDICT_ADMITTED, VERDICT_HALT, VERDICT_REJECTED,
    GateLedger, compose_gate,
)


def _good(**over):
    base = {
        "trade_uuid": "u1", "instance": "SBOX", "trade_version": "v3.1_sbox",
        "created_at_utc": "2026-10-09T12:00:00Z",
        "settlement_date": "2026-10-10", "quantity": 100, "fill_price": 0.50,
        "fee_total": kalshi_fee(100, 0.50), "settlement_confirmed": 1,
        "settlement_outcome": "correct", "settled_value": 1,
        "label_axis": AXIS_VALUE, "snapshot_time_utc": "2026-10-09T12:00:00Z",
    }
    base.update(over)
    return base


def test_admitted():
    v = compose_gate(_good(), REPO, frozen_version="v3.1_sbox", suite_green=True)
    assert v.verdict == VERDICT_ADMITTED
    assert all(c.status == "PASS" for c in v.checks)


def test_g2_fee_mismatch_rejected():
    v = compose_gate(_good(fee_total=25.0), REPO, suite_green=True,
                     frozen_version="v3.1_sbox")
    assert v.verdict == VERDICT_REJECTED
    assert any(c.gate == "G2" and c.status == "REJECT" for c in v.checks)


def test_g2_fee_within_tolerance_admitted():
    # canonical at 0.50 x100 = $2.00; +$0.004 is inside the $0.005 tolerance
    v = compose_gate(_good(fee_total=kalshi_fee(100, 0.50) + 0.004), REPO,
                     suite_green=True, frozen_version="v3.1_sbox")
    assert v.verdict == VERDICT_ADMITTED


def test_g4_day_over_day_rejected():
    v = compose_gate(_good(label_axis="day_over_day"), REPO, suite_green=True,
                     frozen_version="v3.1_sbox")
    assert v.verdict == VERDICT_REJECTED
    assert any(c.gate == "G4" and c.status == "REJECT" for c in v.checks)


def test_g5_lookahead_rejected():
    v = compose_gate(_good(snapshot_time_utc="2026-10-10T01:00:00Z"), REPO,
                     suite_green=True, frozen_version="v3.1_sbox")
    assert v.verdict == VERDICT_REJECTED
    assert any(c.gate == "G5" and c.status == "REJECT" for c in v.checks)


def test_g3_unconfirmed_rejected():
    v = compose_gate(_good(settlement_confirmed=0), REPO, suite_green=True,
                     frozen_version="v3.1_sbox")
    assert any(c.gate == "G3" and c.status == "REJECT" for c in v.checks)


def test_g1_version_mismatch_halt():
    v = compose_gate(_good(trade_version="v9_other"), REPO, suite_green=True,
                     frozen_version="v3.1_sbox")
    assert v.verdict == VERDICT_HALT
    assert any(c.gate == "G1" and c.status == "HALT" for c in v.checks)


def test_halt_beats_reject():
    # both a HALT (version) and a REJECT (bad axis): HALT wins.
    v = compose_gate(_good(trade_version="v9_other", label_axis="day_over_day"),
                     REPO, suite_green=True, frozen_version="v3.1_sbox")
    assert v.verdict == VERDICT_HALT


def test_g0_failure_halts_window():
    v = compose_gate(_good(), REPO, suite_green=False,
                     frozen_version="v3.1_sbox")
    assert v.verdict == VERDICT_HALT
    assert v.checks[0].gate == "G0"


def test_ledger_append_only():
    conn = sqlite3.connect(":memory:")
    led = GateLedger(conn)
    v = compose_gate(_good(), REPO, suite_green=True, frozen_version="v3.1_sbox")
    led.append(v)
    assert conn.execute("SELECT COUNT(*) FROM gate_ledger").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE gate_ledger SET verdict='ADMITTED' WHERE id=1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM gate_ledger WHERE id=1")


def test_ledger_records_verdict_and_failed_gate():
    conn = sqlite3.connect(":memory:")
    led = GateLedger(conn)
    led.append(compose_gate(_good(fee_total=99.0), REPO, suite_green=True,
                            frozen_version="v3.1_sbox"))
    row = conn.execute("SELECT verdict, failed_gate FROM gate_ledger").fetchone()
    assert row[0] == VERDICT_REJECTED and row[1] == "G2"
