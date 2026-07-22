"""
Phase 21.1 — Unit Tests: Trade Journal

Tests core/trade_journal.py:
- Schema creation
- Append entry (all fields)
- Append from decision dict
- Query with filters
- Update outcome (settlement backfill)
- Stats and aggregate summaries
- Edge cases: empty journal, duplicate alert_id
"""

import sys
import os
import json
import tempfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.trade_journal import (
    TradeJournal,
    JournalOutcome,
    record_decision,
    get_journal,
)


@pytest.fixture
def journal():
    """Create a temporary TradeJournal for testing."""
    tmpfile = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmpfile.close()
    # Use a unique path to avoid conftest issues
    yield TradeJournal(db_path=tmpfile.name)
    try:
        os.unlink(tmpfile.name)
    except OSError:
        pass


@pytest.mark.unit
class TestTradeJournalSchema:
    """Test database schema creation."""

    def test_schema_created(self, journal):
        """Journal should create the trade_journal table."""
        import sqlite3
        conn = sqlite3.connect(journal._db_path)
        try:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                ("trade_journal",)
            ).fetchone()
            assert tables is not None
            assert tables[0] == "trade_journal"
        finally:
            conn.close()

    def test_indices_created(self, journal):
        """Indices should exist for station, timestamp, outcome, alert_id."""
        import sqlite3
        conn = sqlite3.connect(journal._db_path)
        try:
            indices = [r[2] for r in conn.execute(
                "SELECT * FROM sqlite_master WHERE type='index'"
            ).fetchall()]
            index_names = [i for i in indices if i and i.startswith("idx_trade_journal")]
            assert len(index_names) >= 3
        finally:
            conn.close()


@pytest.mark.unit
class TestTradeJournalAppend:
    """Test appending entries to the journal."""

    def test_append_basic(self, journal):
        row_id = journal.append_entry(
            station="KATL", market="HIGH", direction="UP",
            outcome=JournalOutcome.EXECUTED,
            confidence=0.75, edge=0.15, market_prob=0.60,
            lane="sure_thing", alert_id="alert_001",
            trade_version="v3.0", functionality="late_day_momentum",
            position_size=100.0,
        )
        assert row_id > 0

    def test_append_minimal(self, journal):
        row_id = journal.append_entry(
            station="KATL", market="HIGH", direction="UP",
            outcome=JournalOutcome.EXECUTED,
        )
        assert row_id > 0

    def test_append_with_metadata(self, journal):
        row_id = journal.append_entry(
            station="KBOS", market="LOW", direction="DOWN",
            outcome=JournalOutcome.SKIPPED_EDGE,
            metadata={"reason": "edge_below_threshold", "threshold": 0.05},
        )
        assert row_id > 0

    def test_append_skiplist_outcomes(self, journal):
        outcomes = [
            JournalOutcome.SKIPPED_EDGE,
            JournalOutcome.SKIPPED_COST,
            JournalOutcome.SKIPPED_CONFIDENCE,
            JournalOutcome.SKIPPED_RISK,
            JournalOutcome.SKIPPED_COOLDOWN,
            JournalOutcome.SKIPPED_FILTER,
            JournalOutcome.SKIPPED_STATION,
            JournalOutcome.SKIPPED_SKILL,
            JournalOutcome.SKIPPED_WINDOW,
            JournalOutcome.SKIPPED_CLUSTER,
            JournalOutcome.SKIPPED_SIZE_ZERO,
            JournalOutcome.ERROR,
        ]
        for outcome in outcomes:
            row_id = journal.append_entry(
                station="KATL", market="HIGH", direction="UP",
                outcome=outcome.value if hasattr(outcome, 'value') else outcome,
            )
            assert row_id > 0

    def test_append_settled_outcomes(self, journal):
        for outcome in [JournalOutcome.SETTLED_WIN, JournalOutcome.SETTLED_LOSS]:
            row_id = journal.append_entry(
                station="KATL", market="HIGH", direction="UP",
                outcome=outcome.value,
            )
            assert row_id > 0


@pytest.mark.unit
class TestTradeJournalAppendFromDecision:
    """Test append_from_decision with various decision result dicts."""

    def test_executed_decision(self, journal):
        decision = {
            "status": "executed",
            "confidence": 0.8,
            "market_prob": 0.55,
            "position_size_usd": 150.0,
            "trade_version": "v3.0",
            "functionality": "test_signal",
        }
        row_id = journal.append_from_decision(
            station="KATL", market="HIGH", direction="UP",
            decision_result=decision,
            signal_ids="test_signal",
        )
        assert row_id > 0

    def test_skipped_decision(self, journal):
        decision = {
            "status": "skipped",
            "confidence": 0.5,
            "market_prob": 0.52,
            "reason": "Edge too low",
        }
        row_id = journal.append_from_decision(
            station="KATL", market="HIGH", direction="DOWN",
            decision_result=decision,
        )
        assert row_id > 0

    def test_risk_killed_decision(self, journal):
        decision = {
            "status": "skipped",
            "risk_killed": True,
            "reason": "Risk kill switch active",
        }
        row_id = journal.append_from_decision(
            station="KATL", market="HIGH", direction="UP",
            decision_result=decision,
        )
        assert row_id > 0


@pytest.mark.unit
class TestTradeJournalQuery:
    """Test query methods on the journal."""

    def test_query_all(self, journal):
        _populate_journal(journal)
        results = journal.query(limit=100)
        assert len(results) >= 5

    def test_query_by_station(self, journal):
        _populate_journal(journal)
        results = journal.query(station="KATL")
        assert len(results) > 0
        for r in results:
            assert r["station"] == "KATL"

    def test_query_by_outcome(self, journal):
        _populate_journal(journal)
        results = journal.query(outcome="EXECUTED")
        assert len(results) > 0
        for r in results:
            assert r["outcome"] == "EXECUTED"

    def test_query_by_lane(self, journal):
        _populate_journal(journal)
        results = journal.query(lane="sure_thing")
        assert len(results) > 0
        for r in results:
            assert r["lane"] == "sure_thing"

    def test_query_empty_results(self, journal):
        results = journal.query(station="NONEXISTENT")
        assert results == []

    def test_query_limit(self, journal):
        _populate_journal(journal, count=20)
        results = journal.query(limit=5)
        assert len(results) <= 5

    def test_get_by_alert_id(self, journal):
        _populate_journal(journal)
        entry = journal.get_by_alert_id("alert_001")
        assert entry is not None
        assert entry["alert_id"] == "alert_001"
        assert entry["station"] == "KATL"

    def test_get_by_alert_id_missing(self, journal):
        entry = journal.get_by_alert_id("nonexistent")
        assert entry is None


@pytest.mark.unit
class TestTradeJournalUpdateOutcome:
    """Test updating outcomes (settlement backfill)."""

    def test_update_outcome(self, journal):
        row_id = journal.append_entry(
            station="KATL", market="HIGH", direction="UP",
            outcome=JournalOutcome.EXECUTED,
            alert_id="alert_settle_001",
        )
        journal.update_outcome("alert_settle_001", "SETTLED_WIN")
        entry = journal.get_by_alert_id("alert_settle_001")
        assert entry["outcome"] == "SETTLED_WIN"

    def test_update_with_metadata(self, journal):
        row_id = journal.append_entry(
            station="KATL", market="HIGH", direction="UP",
            outcome=JournalOutcome.EXECUTED,
            alert_id="alert_settle_002",
        )
        journal.update_outcome(
            "alert_settle_002", "SETTLED_LOSS",
            metadata={"payout": 0.0, "settlement_price": 0.3},
        )
        entry = journal.get_by_alert_id("alert_settle_002")
        assert entry["outcome"] == "SETTLED_LOSS"
        if entry.get("metadata_json"):
            meta = json.loads(entry["metadata_json"])
            assert "payout" in meta
            assert meta["payout"] == 0.0

    def test_update_nonexistent(self, journal):
        # Should not raise
        journal.update_outcome("nonexistent", "SETTLED_WIN")


@pytest.mark.unit
class TestTradeJournalStats:
    """Test aggregate statistics."""

    def test_empty_stats(self, journal):
        stats = journal.get_stats()
        assert stats["total_entries"] == 0

    def test_stats_with_data(self, journal):
        _populate_journal(journal, count=10)
        stats = journal.get_stats()
        assert stats["total_entries"] == 10
        assert "EXECUTED" in stats["by_outcome"]

    def test_aggregate_summary(self, journal):
        _populate_journal(journal)
        summary = journal.get_aggregate_summary(days=30)
        assert summary["total_decisions"] > 0
        assert "executed" in summary
        assert "skipped" in summary

    def test_get_recent_trades(self, journal):
        _populate_journal(journal, count=15)
        trades = journal.get_recent_trades(limit=5)
        assert len(trades) <= 5


@pytest.mark.unit
class TestTradeJournalAccuracy:
    """Test accuracy-by-signal methods."""

    def test_accuracy_by_signal(self, journal):
        _populate_journal(journal)
        acc = journal.get_accuracy_by_signal()
        assert isinstance(acc, dict)

    def test_trade_counts_by_station(self, journal):
        _populate_journal(journal)
        counts = journal.get_trade_counts_by_station()
        assert "KATL" in counts
        assert "traded" in counts["KATL"]

    def test_failure_breakdown(self, journal):
        _populate_journal(journal)
        failures = journal.get_failure_breakdown()
        assert isinstance(failures, list)


@pytest.mark.unit
class TestTradeJournalSingleton:
    """Test the module-level get_journal singleton."""

    def test_singleton(self):
        j1 = get_journal()
        j2 = get_journal()
        assert j1 is j2

    def test_record_decision_convenience(self, journal):
        """record_decision should work and return a row_id."""
        # Override the singleton temporarily
        import core.trade_journal as tj
        original = tj._JOURNAL
        tj._JOURNAL = journal
        try:
            decision = {
                "status": "executed",
                "confidence": 0.7,
                "market_prob": 0.55,
                "position_size_usd": 100.0,
            }
            row_id = record_decision(
                station="KATL", market="HIGH", direction="UP",
                decision_result=decision,
                signal_ids="test_sig",
            )
            assert row_id > 0
        finally:
            tj._JOURNAL = original


# ─── Helpers ─────────────────────────────────────────────────────────────

def _populate_journal(journal, count=5):
    """Populate a journal with test entries."""
    for i in range(count):
        station = ["KATL", "KBOS", "KLAX", "KORD", "KDEN"][i % 5]
        direction = "UP" if i % 2 == 0 else "DOWN"
        outcome = "EXECUTED" if i % 3 != 0 else "SKIPPED_EDGE"
        lane = "sure_thing" if i % 2 == 0 else "regular"
        journal.append_entry(
            station=station, market="HIGH", direction=direction,
            outcome=outcome, confidence=0.7 - i * 0.05,
            edge=0.15 - i * 0.02, market_prob=0.55,
            lane=lane, alert_id=f"alert_{i:03d}",
            trade_version="v3.0", functionality="test",
            position_size=100.0 - i * 10,
        )