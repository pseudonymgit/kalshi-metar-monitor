#!/usr/bin/env python3
"""
Canonical settlement truth + epoch-regeneration immutability tests.

Locks the 2026-10-10 settlement-truth ruling:
  - canonical strike-crossing outcome is strict (equality == not-ITM);
  - the frozen snapshot is write-once and immutable (UPDATE/DELETE abort);
  - a settlement value cannot change when the epoch table is regenerated:
    `verify_regeneration_invariants` reports the move and the epoch generator
    FAILS CLOSED instead of writing it;
  - re-freezing an identical value is idempotent;
  - corrected rows now pass G3 and the gate composer ADMITS them.
"""
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from core.canonical_settlement import (  # noqa: E402
    CANONICAL_TEMP_SOURCE, ImmutableSettlementError, canonical_outcome,
    canonical_settle_yes, ensure_snapshot_table, freeze_snapshot, read_snapshot,
    verify_regeneration_invariants,
)
from core.fees import kalshi_fee  # noqa: E402
from gate_composer import VERDICT_ADMITTED, compose_gate  # noqa: E402


def _row(station="KBOS", market="HIGH", date="2026-10-04", temp=63.0, **over):
    r = {"station": station, "market_type": market, "local_trading_date": date,
         "settlement_temp_f": temp, "temp_source": CANONICAL_TEMP_SOURCE,
         "settled_at_utc": "2026-10-05T05:00:00Z"}
    r.update(over)
    return r


# ─── canonical label ─────────────────────────────────────────────────────
def test_strike_crossing_is_strict_high_and_low():
    assert canonical_settle_yes(71, 70, "HIGH") == 1
    assert canonical_settle_yes(70, 70, "HIGH") == 0        # equality = loss
    assert canonical_settle_yes(69, 70, "LOW") == 1
    assert canonical_settle_yes(70, 70, "LOW") == 0


def test_canonical_outcome_high_low():
    # KBOS 10-04 strike 61, canonical 63 -> HIGH ITM -> buy UP is correct
    assert canonical_outcome("UP", "HIGH", 63.0, 61.0) == "correct"
    assert canonical_outcome("DOWN", "HIGH", 63.0, 61.0) == "incorrect"
    # LOW: temp < strike is ITM -> DOWN correct
    assert canonical_outcome("DOWN", "LOW", 58.0, 60.0) == "correct"
    assert canonical_outcome("UP", "LOW", 58.0, 60.0) == "incorrect"


def test_canonical_outcome_equality_is_incorrect_for_up():
    assert canonical_outcome("UP", "HIGH", 70.0, 70.0) == "incorrect"


# ─── frozen snapshot write-once + immutable ───────────────────────────────
def test_freeze_then_identical_refreeze_is_idempotent():
    conn = sqlite3.connect(":memory:")
    ins, skip, conf = freeze_snapshot(conn, [_row()], frozen_at_utc="t1")
    assert (ins, skip, conf) == (1, 0, [])
    ins, skip, conf = freeze_snapshot(conn, [_row()], frozen_at_utc="t2")
    assert (ins, skip, conf) == (0, 1, [])
    assert read_snapshot(conn, "KBOS", "HIGH", "2026-10-04")["settlement_temp_f"] == 63.0


def test_refreeze_different_value_fails_closed():
    conn = sqlite3.connect(":memory:")
    freeze_snapshot(conn, [_row(temp=63.0)], frozen_at_utc="t1")
    with pytest.raises(ImmutableSettlementError):
        freeze_snapshot(conn, [_row(temp=57.0)], frozen_at_utc="t2")
    # value untouched
    assert read_snapshot(conn, "KBOS", "HIGH", "2026-10-04")["settlement_temp_f"] == 63.0


def test_snapshot_rows_are_immutable():
    conn = sqlite3.connect(":memory:")
    freeze_snapshot(conn, [_row()], frozen_at_utc="t1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE settlement_snapshot SET settlement_temp_f=99 WHERE station='KBOS'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM settlement_snapshot WHERE station='KBOS'")


def test_ensure_snapshot_table_idempotent():
    conn = sqlite3.connect(":memory:")
    ensure_snapshot_table(conn)
    ensure_snapshot_table(conn)  # must not raise


# ─── the invariant ────────────────────────────────────────────────────────
def test_regeneration_matching_frozen_is_clean():
    conn = sqlite3.connect(":memory:")
    freeze_snapshot(conn, [_row(temp=63.0)], frozen_at_utc="t1")
    v = verify_regeneration_invariants(conn, [
        {"station": "KBOS", "market_type": "HIGH",
         "local_trading_date": "2026-10-04", "settlement_bucket": 63}])
    assert v == []


def test_regeneration_moving_frozen_value_is_violation():
    conn = sqlite3.connect(":memory:")
    freeze_snapshot(conn, [_row(temp=63.0)], frozen_at_utc="t1")
    v = verify_regeneration_invariants(conn, [
        {"station": "KBOS", "market_type": "HIGH",
         "local_trading_date": "2026-10-04", "settlement_bucket": 57}])
    assert len(v) == 1 and "move frozen 63.0 -> 57" in v[0]


def test_regeneration_ignores_unfrozen_rows():
    conn = sqlite3.connect(":memory:")
    ensure_snapshot_table(conn)
    v = verify_regeneration_invariants(conn, [
        {"station": "KSEA", "market_type": "HIGH",
         "local_trading_date": "2026-10-03", "settlement_bucket": 66}])
    assert v == []


def test_epoch_generator_fails_closed_on_frozen_move():
    """generate_settlement_epochs.insert_epochs must refuse to move a frozen value."""
    gen = pytest.importorskip("generate_settlement_epochs")
    conn = sqlite3.connect(":memory:")
    freeze_snapshot(conn, [_row(temp=63.0)], frozen_at_utc="t1")
    gen.create_settlement_epochs_table(conn)
    epochs = [{
        "station": "KBOS", "market_type": "HIGH", "local_trading_date": "2026-10-04",
        "date_utc": "2026-10-04", "settlement_bucket": 57,
        "prior_settlement_bucket": 60, "settlement_timestamp_utc": "2026-10-04T05:00:00Z",
        "epoch_status": "closed", "epoch_close_reason": "daily_completion",
        "epoch_close_timestamp_utc": "2026-10-04T05:00:00Z", "reversion_occurred": 0,
        "first_reversion_timestamp_utc": None, "max_excursion_above_settlement": 0.0,
        "duration_at_or_above_settlement_seconds": 86400,
        "duration_strictly_above_settlement_seconds": 0, "terminal_state_reached": 1,
        "settlement_transition_event_id": None, "last_transition_event_id": None,
        "last_transition_timestamp_utc": None, "last_transition_temp_f": None,
        "epoch_sequence": 1, "created_at_utc": "2026-10-10T00:00:00Z",
    }]
    with pytest.raises(RuntimeError, match="immutable across epoch regeneration"):
        gen.insert_epochs(conn, epochs)
    # nothing written
    assert conn.execute("SELECT COUNT(*) FROM settlement_epochs").fetchone()[0] == 0


def test_epoch_generator_writes_when_matching_frozen():
    gen = pytest.importorskip("generate_settlement_epochs")
    conn = sqlite3.connect(":memory:")
    freeze_snapshot(conn, [_row(temp=63.0)], frozen_at_utc="t1")
    gen.create_settlement_epochs_table(conn)
    epochs = [{
        "station": "KBOS", "market_type": "HIGH", "local_trading_date": "2026-10-04",
        "date_utc": "2026-10-04", "settlement_bucket": 63,
        "prior_settlement_bucket": 60, "settlement_timestamp_utc": "2026-10-04T05:00:00Z",
        "epoch_status": "closed", "epoch_close_reason": "daily_completion",
        "epoch_close_timestamp_utc": "2026-10-04T05:00:00Z", "reversion_occurred": 0,
        "first_reversion_timestamp_utc": None, "max_excursion_above_settlement": 0.0,
        "duration_at_or_above_settlement_seconds": 86400,
        "duration_strictly_above_settlement_seconds": 0, "terminal_state_reached": 1,
        "settlement_transition_event_id": None, "last_transition_event_id": None,
        "last_transition_timestamp_utc": None, "last_transition_temp_f": None,
        "epoch_sequence": 1, "created_at_utc": "2026-10-10T00:00:00Z",
    }]
    gen.insert_epochs(conn, epochs)
    row = conn.execute("SELECT settlement_bucket FROM settlement_epochs").fetchone()
    assert row[0] == 63


# ─── gate composer integration: corrected rows ADMIT ──────────────────────
def _corrected_candidate(**over):
    """A corrected SBOX row: canonical truth KBOS 10-04 = 63 vs strike 61 -> ITM.

    settlement_date is the T+1 finalization day (2026-10-05), matching how the
    engine records settlement_date_utc, so G5 (as-of) does not fire on the same-
    day entry timestamp.  G5 itself is unchanged.
    """
    base = {
        "trade_uuid": "KBOS-2026-10-04", "instance": "SBOX",
        "trade_version": "v3.1_sbox", "created_at_utc": "2026-10-04T14:00:00Z",
        "settlement_date": "2026-10-05", "quantity": 2000, "fill_price": 0.025,
        "fee_total": kalshi_fee(2000, 0.025),
        "settlement_confirmed": 1, "settlement_outcome": "correct", "settled_value": 1,
        "label_axis": "strike_crossing", "snapshot_time_utc": "2026-10-04T14:00:00Z",
    }
    base.update(over)
    return base


def test_g3_passes_for_corrected_row():
    v = compose_gate(_corrected_candidate(), REPO, suite_green=True,
                     frozen_version="v3.1_sbox")
    g3 = [c for c in v.checks if c.gate == "G3"][0]
    assert g3.status == "PASS"


def test_gate_composer_admits_corrected_row():
    v = compose_gate(_corrected_candidate(), REPO, suite_green=True,
                     frozen_version="v3.1_sbox")
    assert v.verdict == VERDICT_ADMITTED
    assert all(c.status == "PASS" for c in v.checks)


def test_g3_still_rejects_uncorrected_row():
    """G3 is NOT weakened: settlement_confirmed=0 still rejects."""
    v = compose_gate(_corrected_candidate(settlement_confirmed=0), REPO,
                     suite_green=True, frozen_version="v3.1_sbox")
    g3 = [c for c in v.checks if c.gate == "G3"][0]
    assert g3.status == "REJECT"
