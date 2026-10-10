#!/usr/bin/env python3
"""
Pre-ingest gate composer (G0-G5, M6 core) tests.

Locks GR5 §2:
  - a clean candidate is ADMITTED;
  - a fee off by >$0.005 is REJECTED (G2) and the canonical fee is reused;
  - a day-over-day axis is REJECTED (G4);
  - a lookahead input is REJECTED (G5);
  - G5 branches by strategy class (2026-10-10 ruling): the intraday family is
    bounded at the pinned 13:30 station-local entry instant (provenance-guarded),
    the observational/prior-day family keeps the settlement-date boundary;
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
        "settlement_outcome": "correct", "settled_value": 72.0,
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


# ─── G5 methodology ruling 2026-10-10: branch by strategy class ──────────────
# Intraday family (B2 sweep, SBOX EV-strike bridge) decides at the pinned
# 13:30 station-local instant. KNYC 2026-10-04 is EDT -> 17:30Z; KSFO PDT -> 20:30Z.

def _intraday(**over):
    base = _good(
        trade_uuid="i1", instance="B2", station="KNYC", signal_family="intraday",
        settlement_date="2026-10-04",
        created_at_utc="2026-10-04T17:00:00Z",
        snapshot_time_utc="2026-10-04T17:30:00Z",
        entry_time_utc="2026-10-04T17:30:00Z",
        finalization_utc="2026-10-05T05:00:00Z",
    )
    base.update(over)
    return base


def test_g5_intraday_same_day_legit_input_admitted():
    # The reproduced failure: a same-day 17:30Z input previously REJECTED as
    # lookahead. After the ruling it is the decision instant -> ADMITTED.
    v = compose_gate(_intraday(), REPO, suite_green=True, frozen_version="v3.1_sbox")
    assert v.verdict == VERDICT_ADMITTED
    assert all(c.status == "PASS" for c in v.checks)


def test_g5_intraday_station_specific_pin():
    # KSFO 2026-10-04 PDT -> pinned entry 20:30Z, and the same-day obs at 20:30Z passes.
    v = compose_gate(_intraday(station="KSFO", entry_time_utc="2026-10-04T20:30:00Z",
                               snapshot_time_utc="2026-10-04T20:30:00Z"),
                     REPO, suite_green=True, frozen_version="v3.1_sbox")
    assert v.verdict == VERDICT_ADMITTED
    # but a KNYC-pinned 17:30Z entry on KSFO is a provenance mismatch.
    v2 = compose_gate(_intraday(station="KSFO", entry_time_utc="2026-10-04T17:30:00Z"),
                      REPO, suite_green=True, frozen_version="v3.1_sbox")
    assert v2.verdict == VERDICT_REJECTED
    assert any(c.gate == "G5" and c.status == "REJECT" for c in v2.checks)


def test_g5_prior_day_family_unchanged():
    v = compose_gate(_good(trade_uuid="p1",
                           signal_family="observational_prior_day",
                           created_at_utc="2026-10-09T12:00:00Z",
                           snapshot_time_utc="2026-10-09T12:00:00Z"),
                     REPO, suite_green=True, frozen_version="v3.1_sbox")
    assert v.verdict == VERDICT_ADMITTED
    # settlement-day observation still REJECTED for the observational family.
    v2 = compose_gate(_good(trade_uuid="p2",
                            signal_family="observational_prior_day",
                            snapshot_time_utc="2026-10-10T01:00:00Z"),
                      REPO, suite_green=True, frozen_version="v3.1_sbox")
    assert any(c.gate == "G5" and c.status == "REJECT" for c in v2.checks)
    # unknown/missing family falls through to the strict observational check.
    v3 = compose_gate(_good(trade_uuid="p3", settlement_date="2026-10-04",
                            created_at_utc="2026-10-04T17:00:00Z",
                            snapshot_time_utc="2026-10-04T17:30:00Z"),
                      REPO, suite_green=True, frozen_version="v3.1_sbox")
    assert any(c.gate == "G5" and c.status == "REJECT" for c in v3.checks)


def test_g5_intraday_post_decision_input_rejected():
    # an input strictly after the pinned entry instant is genuine lookahead.
    v = compose_gate(_intraday(snapshot_time_utc="2026-10-04T18:00:00Z"), REPO,
                     suite_green=True, frozen_version="v3.1_sbox")
    assert v.verdict == VERDICT_REJECTED
    assert any(c.gate == "G5" and c.status == "REJECT" and "post-decision" in c.detail
               for c in v.checks)
    # boundary: an input exactly AT entry is admitted (known at decision time).
    v2 = compose_gate(_intraday(obs_max_source_ts="2026-10-04T17:30:00Z"), REPO,
                      suite_green=True, frozen_version="v3.1_sbox")
    assert v2.verdict == VERDICT_ADMITTED
    v3 = compose_gate(_intraday(obs_max_source_ts="2026-10-04T17:30:01Z"), REPO,
                      suite_green=True, frozen_version="v3.1_sbox")
    assert any(c.gate == "G5" and c.status == "REJECT" for c in v3.checks)


def test_g5_intraday_provenance_mismatch_rejected():
    # H3 provenance guard: a bare run silently defaulted entry to 13:00Z. A
    # candidate whose decision instant != the pinned 13:30-local -> REJECT.
    v = compose_gate(_intraday(entry_time_utc="2026-10-04T13:00:00Z"), REPO,
                     suite_green=True, frozen_version="v3.1_sbox")
    assert v.verdict == VERDICT_REJECTED
    assert any(c.gate == "G5" and c.status == "REJECT" and "provenance" in c.detail
               for c in v.checks)


def test_g5_intraday_missing_entry_or_finalization_rejected():
    v = compose_gate(_intraday(entry_time_utc=None), REPO, suite_green=True,
                     frozen_version="v3.1_sbox")
    assert any(c.gate == "G5" and c.status == "REJECT" for c in v.checks)
    v2 = compose_gate(_intraday(finalization_utc=None), REPO, suite_green=True,
                      frozen_version="v3.1_sbox")
    assert any(c.gate == "G5" and c.status == "REJECT" for c in v2.checks)


def test_g5_intraday_decision_after_finalization_rejected():
    v = compose_gate(_intraday(entry_time_utc="2026-10-05T06:00:00Z"), REPO,
                     suite_green=True, frozen_version="v3.1_sbox")
    assert any(c.gate == "G5" and c.status == "REJECT" for c in v.checks)
    # non-pinned and after finalization are both rejections; the boundary is what matters.
    assert v.verdict == VERDICT_REJECTED


def test_g3_unconfirmed_rejected():
    v = compose_gate(_good(settlement_confirmed=0), REPO, suite_green=True,
                     frozen_version="v3.1_sbox")
    assert any(c.gate == "G3" and c.status == "REJECT" for c in v.checks)


# ─── G3 reconciliation (2026-10-10): the book stores a TEMPERATURE ──────────
# The book's canonical correction writes `settled_value` as the settlement
# temperature (°F) and the strike-crossing `canonical_outcome`. G3's intent is
# "settlement is final and binary-decidable", which canonical_outcome IS.
def test_g3_admits_temperature_settled_value():
    # a temperature-valued settled_value must NOT fail G3 by itself.
    v = compose_gate(_good(settled_value=91.0, canonical_outcome="correct"),
                     REPO, suite_green=True, frozen_version="v3.1_sbox")
    g3 = [c for c in v.checks if c.gate == "G3"][0]
    assert g3.status == "PASS"
    assert v.verdict in (VERDICT_ADMITTED, VERDICT_REJECTED)  # only other gates may reject
    assert not any(c.gate == "G3" and c.status == "REJECT" for c in v.checks)


def test_g3_derived_binary_matches_canonical_outcome():
    from gate_composer import settled_binary
    assert settled_binary({"canonical_outcome": "correct"}) == 1
    assert settled_binary({"canonical_outcome": "incorrect"}) == 0
    assert settled_binary({"settlement_outcome": "correct"}) == 1
    assert settled_binary({}) is None  # not decidable -> no binary


def test_g3_rejects_non_strike_crossing_axis():
    # outcome must be strike-crossing derived, not day-over-day.
    v = compose_gate(_good(label_axis="day_over_day"), REPO, suite_green=True,
                     frozen_version="v3.1_sbox")
    assert any(c.gate == "G3" and c.status == "REJECT" for c in v.checks)


def test_g3_rejects_missing_outcome_even_with_temperature():
    v = compose_gate(_good(settled_value=91.0, settlement_outcome=None),
                     REPO, suite_green=True, frozen_version="v3.1_sbox")
    assert any(c.gate == "G3" and c.status == "REJECT" for c in v.checks)


def test_g3_prefers_canonical_outcome_over_booked():
    # canonical (strike-crossing) outcome wins over a stale booked one.
    v = compose_gate(_good(settlement_outcome="incorrect",
                           canonical_outcome="correct"),
                     REPO, suite_green=True, frozen_version="v3.1_sbox")
    g3 = [c for c in v.checks if c.gate == "G3"][0]
    assert g3.status == "PASS" and "correct" in g3.detail


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
