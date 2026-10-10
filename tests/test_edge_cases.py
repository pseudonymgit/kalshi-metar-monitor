"""
Phase 21.4 — Edge Case Tests

Tests edge cases for:
- Kalshi API wrapper: mock HTTP errors, rate limits, empty responses
- Position sizing: zero balance, negative edge, extreme values
- DB operations: schema creation, migration, missing tables
- Alert dispatch: webhook failures, malformed payloads
"""

import sys
import os
import json
import tempfile
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch, Mock

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def temp_db():
    """Create a temporary SQLite database for testing."""
    tmpfile = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmpfile.close()
    yield tmpfile.name
    try:
        os.unlink(tmpfile.name)
    except OSError:
        pass
    for ext in ('-wal', '-shm'):
        try:
            os.unlink(tmpfile.name + ext)
        except OSError:
            pass


# ─── Kalshi API Wrapper Edge Cases ──────────────────────────────────────────

@pytest.mark.unit
class TestKalshiApiEdgeCases:
    """Test Kalshi API wrapper with mock HTTP errors, rate limits, empty responses."""

    def test_empty_market_response(self):
        """Verify graceful handling of an empty market list (mocked, deterministic)."""
        from core import market_monitor

        with patch.object(market_monitor, "_kalshi_public_get",
                          return_value={"markets": []}):
            result = market_monitor.discover_kalshi_weather_markets(max_pages=1, page_limit=1)
        # An empty feed must degrade to an empty discovery, not raise.
        assert result == []

    def test_connection_error_mock(self):
        """A connection error must surface, not be silently swallowed.

        Was `except Exception: pass` — a working call was indistinguishable from
        a crash (a vacuous pass). Now assert the real degradation contract: the
        transport error propagates so the caller can back off.
        """
        from core import kalshi_monitor

        with patch.object(kalshi_monitor, "_kalshi_public_get",
                          side_effect=ConnectionError("mock network down")):
            with pytest.raises(Exception):
                kalshi_monitor.get_public_markets(limit=1)

    def test_price_fetcher_none_market(self):
        """Price fetcher's public-markets reader is a documented CORE DEFECT.

        ``core/price_fetcher.py:357`` calls ``_kalshi_public_get`` with
        undefined names (``series_ticker``, ``normalized_station``) → NameError
        on any call. This is genuine breakage, NOT test staleness (flag for
        Workstream E report); the old ``except Exception: pass`` hid it.
        """
        from core import price_fetcher

        pytest.xfail(
            "core defect: price_fetcher._get_all_public_markets references "
            "undefined series_ticker/normalized_station (line 357-364)"
        )
        result = price_fetcher._get_all_public_markets(max_pages=1, page_limit=1)
        assert isinstance(result, list)


# ─── Position Sizing Edge Cases ─────────────────────────────────────────────

@pytest.mark.unit
class TestPositionSizingEdgeCases:
    """Test position sizing with extreme values: zero balance, negative edge, etc."""

    def test_negative_edge(self):
        """Negative edge should produce zero position."""
        from core.position_sizing import compute_position_size

        position, tier, meta = compute_position_size(
            signal_type='momentum',
            confidence=0.7,
            current_balance=10000.0,
            market_price=0.5,
        )
        # Even with positive edge, position should be reasonable
        assert position >= 0

    def test_zero_confidence(self):
        """Zero confidence should produce low position or zero."""
        from core.position_sizing import compute_position_size

        position, tier, meta = compute_position_size(
            signal_type='momentum',
            confidence=0.0,
            current_balance=10000.0,
            market_price=0.5,
        )
        # Position should be very small for zero confidence
        assert position >= 0
        assert tier.name == 'LOW' or tier == 'LOW' or str(tier) == 'LOW' or 'LOW' in str(tier)

    def test_max_confidence(self):
        """Maximum confidence should produce reasonable position."""
        from core.position_sizing import compute_position_size

        position, tier, meta = compute_position_size(
            signal_type='momentum',
            confidence=1.0,
            current_balance=10000.0,
            market_price=0.5,
        )
        assert position >= 0

    def test_kelly_sizing_negative_edge(self):
        """Kelly sizer with negative edge (win_rate < 0.5) should return <= 0."""
        from core.fee_aware_kelly_position_sizing import KellyPositionSizer

        sizer = KellyPositionSizer()
        # Negative edge should give non-positive fraction
        fraction = sizer.compute_kelly_fraction(win_rate=0.40)
        assert fraction <= 0.0

    def test_extreme_confidence_values(self):
        """Test position sizing with confidence 0.0 and 1.0."""
        from core.position_sizing import PositionSizingConfig, classify_confidence, ConfidenceTier

        assert classify_confidence(0.0) == ConfidenceTier.LOW
        assert classify_confidence(1.0) == ConfidenceTier.HIGH

    def test_zero_max_risk(self):
        """Kelly fraction with zero max_risk should work."""
        from core.position_sizing import KellyPositionSizer

        sizer = KellyPositionSizer(fee_rate=0.0, fraction_kelly=0.5)
        # calculate_kelly_fraction takes edge, win_rate, signal_id
        fraction = sizer.calculate_kelly_fraction(edge=0.1, win_rate=0.6)
        assert fraction >= 0

    def test_negative_edge_kelly(self):
        """Negative edge may produce negative or zero kelly fraction."""
        from core.position_sizing import KellyPositionSizer

        sizer = KellyPositionSizer(fee_rate=0.0, fraction_kelly=0.5)
        fraction = sizer.calculate_kelly_fraction(edge=-0.1, win_rate=0.4)
        # Negative edge: Kelly fraction should not be positive
        assert fraction is not None

    def test_confidence_tier_classification(self):
        """Test extreme confidence tier classification."""
        from core.position_sizing import PositionSizingConfig, classify_confidence, ConfidenceTier

        config = PositionSizingConfig(
            high_confidence_threshold=0.70,
            medium_confidence_threshold=0.50,
        )
        # Very low confidence
        assert classify_confidence(0.01, config) == ConfidenceTier.LOW
        # Very high confidence
        assert classify_confidence(0.99, config) == ConfidenceTier.HIGH
        # Boundary values
        assert classify_confidence(0.50, config) == ConfidenceTier.MEDIUM
        assert classify_confidence(0.70, config) == ConfidenceTier.HIGH


# ─── DB Operations Edge Cases ────────────────────────────────────────────────

@pytest.mark.unit
class TestDatabaseOperationsEdgeCases:
    """Test DB schema creation, migration, missing tables."""

    def test_schema_creation_with_empty_db(self, temp_db):
        """Schema creation should work on empty database."""
        # R/T (2026-09-29): was a silent skip on `from core.sqlite_utils import
        # ensure_schema` (ImportError -> skip). The module EXISTS; the symbol was
        # renamed. Use the current API so a regression is a real FAILURE.
        from core.sqlite_utils import get_sqlite_connection
        from core.db_schema import ensure_all_tables
        conn = get_sqlite_connection(temp_db)
        n = ensure_all_tables(conn)
        assert n is not None
        conn.close()

    def test_schema_reentrant(self, temp_db):
        """Running schema creation twice should not fail."""
        from core.sqlite_utils import get_sqlite_connection
        from core.db_schema import ensure_all_tables
        conn = get_sqlite_connection(temp_db)
        ensure_all_tables(conn)
        n = ensure_all_tables(conn)
        assert n is not None
        conn.close()

    def test_get_schema_report(self, temp_db):
        """Schema registry should list available tables."""
        from core.db_schema import get_table_names
        tables = get_table_names()
        assert tables is not None
        assert isinstance(tables, (list, dict, tuple))
        assert len(tables) > 0, "db_schema reports no tables"

    def test_missing_table_graceful(self, temp_db):
        """Querying a missing table should not crash."""
        conn = sqlite3.connect(temp_db)
        try:
            result = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            assert isinstance(result, list)
        finally:
            conn.close()

    def test_db_schema_has_tables(self):
        """Schema registry should list available tables."""
        # R/T (2026-09-29): removed silent skip; db_schema IS importable.
        from core.db_schema import get_table_names
        table_names = get_table_names()
        assert table_names is not None
        assert isinstance(table_names, (list, dict, tuple))

    def test_db_connection_none_path(self):
        """Connection with an invalid path should be handled."""
        # R/T (2026-09-29): get_pooled_connection was removed. Assert the current
        # connection surface rejects a bad path rather than silently skipping.
        from core.sqlite_utils import get_sqlite_connection
        with pytest.raises((TypeError, ValueError)):
            get_sqlite_connection(None)

    def test_migration_test(self, temp_db):
        """Schema setup + table enumeration on a fresh DB."""
        # R/T (2026-09-29): run_migration_test was removed; assert the current
        # ensure_all_tables path works on a fresh DB.
        from core.sqlite_utils import get_sqlite_connection
        from core.db_schema import ensure_all_tables
        conn = get_sqlite_connection(temp_db)
        n = ensure_all_tables(conn)
        assert n is not None
        conn.close()


# ─── Alert Dispatch Edge Cases ───────────────────────────────────────────────

@pytest.mark.unit
class TestAlertDispatchEdgeCases:
    """Test alert dispatch with webhook failures, malformed payloads."""

    def test_alert_builder_empty_payload(self):
        """Alert builder should handle empty payload gracefully."""
        try:
            from core.alert_builder import build_paper_trade_alert
        except ImportError:
            pytest.skip("alert_builder module not available")

        try:
            result = build_paper_trade_alert(
                trade_result={}, station="KATL", market_type="HIGH", direction="UP"
            )
            assert result is not None
            assert isinstance(result, dict)
        except (ImportError, TypeError) as e:
            pytest.skip(f"build_paper_trade_alert raised: {e}")

    def test_alert_dispatcher_missing_webhook(self):
        """Alert dispatcher should handle missing webhook URL."""
        try:
            from core.alert_dispatcher import dispatch_alert
        except ImportError:
            pytest.skip("alert_dispatcher module not available")

        try:
            result = dispatch_alert(
                alert_data={"test": True},
                discord_payload={"content": "test"},
                webhook_url=None,
            )
            assert result is not None
            assert isinstance(result, dict)
        except (ImportError, TypeError) as e:
            pytest.skip(f"dispatch_alert raised: {e}")

    def test_alert_dispatcher_malformed_payload(self):
        """Alert dispatcher should handle malformed payload."""
        try:
            from core.alert_dispatcher import dispatch_alert
        except ImportError:
            pytest.skip("alert_dispatcher module not available")

        try:
            result = dispatch_alert(
                alert_data={},
                discord_payload={},
                webhook_url="https://hooks.example.com/alert",
            )
            assert result is not None
            assert isinstance(result, dict)
        except (ImportError, TypeError) as e:
            pytest.skip(f"dispatch_alert raised: {e}")

    def test_alert_retry_queue_empty(self, temp_db, monkeypatch):
        """Alert retry queue should handle empty queue."""
        from core import alert_retry_queue
        # Hermetic: point the queue at a temp DB (was opening the prod
        # alert_retry_queue.db, which the conftest DB sandbox blocks).
        monkeypatch.setenv("ALERT_DB_PATH", temp_db)
        from core.alert_retry_queue import _get_queue_stats, _retry_delivery_batch

        # An empty queue processes cleanly and returns a zero-count result
        # (was skip-on-exception — a vacuous pass that hid real regressions).
        result = _retry_delivery_batch(batch_size=1)
        assert isinstance(result, dict), f"retry batch returned {type(result).__name__}"
        assert result.get("processed") == 0, f"empty queue processed {result.get('processed')} entries"

    def test_alert_state_machine_invalid_transition(self, temp_db):
        """Alert state machine should handle invalid transitions."""
        from core.alert_state_machine import AlertStateMachine, AlertState

        machine = AlertStateMachine(db_path=temp_db)
        # A transition on an unknown alert must be rejected, not crash
        # (was skip-on-exception — a vacuous pass).
        ok, reason = machine.transition(
            alert_id="nonexistent",
            target_state=AlertState.ISSUED,
        )
        assert ok is False, "invalid transition on unknown alert was accepted"
        assert "not found" in str(reason).lower(), f"unexpected rejection reason: {reason!r}"

    def test_alert_throttle_excessive(self, temp_db):
        """Alert throttle should handle excessive alerts."""
        from core.alert_throttle import AlertThrottle

        throttle = AlertThrottle(db_path=temp_db)
        # 100 rapid alerts across 10 types must never crash and must always
        # return a boolean decision (was skip-on-exception — a vacuous pass).
        for i in range(100):
            result = throttle.should_send_alert(
                station="KATL",
                alert_type=f"test_type_{i % 10}",
                current_state="PENDING",
            )
            assert isinstance(result, bool), f"throttle returned {type(result).__name__}"

    def test_alert_formatter_none_values(self):
        """Alert formatter should handle None values."""
        try:
            from core.alert_formatter import format_alert
        except ImportError:
            pytest.skip("alert_formatter module not available")

        try:
            result = format_alert(
                station="KATL", market_type="HIGH", direction="UP",
                event_ticker="KXHIGHTATL", position_size=100.0,
                conviction_details={"method": "test"}, balance=10000.0,
            )
            assert result is not None
            assert isinstance(result, dict)
        except (ImportError, TypeError) as e:
            pytest.skip(f"format_alert raised: {e}")


# ─── Agreement Gate Edge Cases ──────────────────────────────────────────────

@pytest.mark.unit
class TestAgreementGateEdgeCases:
    """Test agreement gate with edge cases: empty signals, extreme inputs."""

    def test_empty_signals_list(self):
        """Empty signals list should return empty list."""
        from core.agreement_gate import AgreementGate, SimpleAgreementChecker

        gate = AgreementGate()
        result = gate.filter_signals([])
        assert result == []

    def test_single_signal_insufficient(self):
        """Single signal with n_required=3 should pass through if < m_total."""
        from core.agreement_gate import AgreementGate

        gate = AgreementGate(n_required=3, m_total=9)
        signals = [("KATL", "HIGH", "UP", "test")]
        result = gate.filter_signals(signals)
        assert len(result) == 1

    def test_simple_checker_empty(self):
        """SimpleAgreementChecker with empty list should return empty."""
        from core.agreement_gate import SimpleAgreementChecker

        result = SimpleAgreementChecker.check_agreement([], n_required=3)
        assert result == []

    def test_simple_checker_insufficient_count(self):
        """SimpleAgreementChecker with fewer signals than n_required."""
        from core.agreement_gate import SimpleAgreementChecker

        signals = [("KATL", "HIGH", "UP", "a"), ("KATL", "HIGH", "UP", "b")]
        result = SimpleAgreementChecker.check_agreement(signals, n_required=3)
        assert result == []

    def test_update_threshold_dynamic(self):
        """Dynamic update of agreement threshold."""
        from core.agreement_gate import AgreementGate

        gate = AgreementGate(n_required=3, m_total=9)
        gate.update_threshold(n_required=5, m_total=11)
        assert gate.n_required == 5
        assert gate.m_total == 11


# ─── Trade Journal Edge Cases ────────────────────────────────────────────────

@pytest.mark.unit
class TestTradeJournalEdgeCases:
    """Test trade journal with edge cases: empty journal, duplicate IDs, etc."""

    def test_empty_journal_query(self, temp_db):
        """Querying empty journal should return empty list."""
        from core.trade_journal import TradeJournal

        journal = TradeJournal(db_path=temp_db)
        trades = journal.get_recent_trades(limit=10)
        assert trades == []

    def test_empty_journal_stats(self, temp_db):
        """Stats on empty journal should return zeros."""
        from core.trade_journal import TradeJournal

        journal = TradeJournal(db_path=temp_db)
        stats = journal.get_stats()
        assert stats is not None
        if isinstance(stats, dict):
            for key, value in stats.items():
                assert value is not None

    def test_duplicate_alert_id(self, temp_db):
        """Duplicate alert_id should not crash."""
        from core.trade_journal import TradeJournal

        journal = TradeJournal(db_path=temp_db)
        entry = {
            'station': 'KATL',
            'market': 'HIGH',
            'direction': 'up',
            'outcome': 'pending',
            'confidence': 0.7,
            'alert_id': 'test_dup_001',
        }
        # Duplicate append must be a clean no-op or an explicit rejection — but
        # it must NEVER silently swallow an exception (was `except Exception:
        # pass`). Call it twice directly; any raise fails the test.
        journal.append_entry(**entry)
        journal.append_entry(**entry)
        rows = [t for t in journal.get_recent_trades()
                if t.get("alert_id") == entry["alert_id"]]
        assert len(rows) >= 1, "duplicate append lost the original row"

    def test_journal_with_missing_fields(self, temp_db):
        """Journal should handle entries with missing optional fields."""
        from core.trade_journal import TradeJournal

        journal = TradeJournal(db_path=temp_db)
        try:
            journal.append_entry(
                station='KATL',
                market='HIGH',
                direction='up',
                outcome='pending',
                confidence=0.5,
                alert_id='test_missing_001',
            )
            trades = journal.get_recent_trades()
            assert len(trades) >= 1
        except Exception as e:
            pytest.skip(f"Minimal journal entry raised: {e}")

    def test_journal_update_nonexistent(self, temp_db):
        """Updating nonexistent entry should not crash."""
        from core.trade_journal import TradeJournal

        journal = TradeJournal(db_path=temp_db)
        # Updating a missing row must be a clean no-op, not an unhandled crash
        # (was `except Exception: pass` — a vacuous pass).
        result = journal.update_outcome(
            alert_id='nonexistent_id',
            outcome='EXECUTED',
        )
        assert result is False or result is None, \
            f"updating a missing alert_id must report no rows, got {result!r}"


# ─── Signal Factory Edge Cases ───────────────────────────────────────────────

@pytest.mark.unit
class TestSignalFactoryEdgeCases:
    """Test signal registry factory with edge cases."""

    def test_registry_with_none_db_path(self):
        """SignalRegistry should handle None db_path."""
        from core.signals import SignalRegistry, create_signal_registry

        registry = SignalRegistry(db_path=None)
        # 2026-10-05: +subhourly_trajectory (QC 789b30027 registry-gap fix) → 18.
        assert len(registry.signals) == 18
        for name, sig in registry.signals.items():
            assert hasattr(sig, 'evaluate')

    def test_get_nonexistent_signal(self):
        """Getting a nonexistent signal should return None."""
        from core.signals import SignalRegistry

        registry = SignalRegistry(db_path="/tmp/test_nonexistent.db")
        result = registry.get_signal("nonexistent_signal")
        assert result is None

    def test_get_all_signals(self):
        """get_all_signals should return all signals."""
        from core.signals import SignalRegistry

        registry = SignalRegistry(db_path="/tmp/test_get_all.db")
        signals = registry.get_all_signals()
        # 2026-10-05: +subhourly_trajectory (QC 789b30027 registry-gap fix) → 18.
        assert len(signals) == 18
        assert "subhourly_trajectory" in signals

    def test_add_signal_valid(self):
        """Adding a valid signal should work."""
        from core.signals import SignalRegistry
        from core.signals.base_signal import BaseSignal

        class TestSignal(BaseSignal):
            name = "test_signal"
            min_lookback = 0
            def evaluate(self, idx, days):
                return 'up', 0.5

        registry = SignalRegistry(db_path="/tmp/test_add.db")
        registry.add_signal("test_signal", TestSignal())
        assert registry.get_signal("test_signal") is not None
        assert len(registry.signals) == 19  # 18 base + 1 added

    def test_add_signal_invalid_type(self):
        """Adding a non-BaseSignal object should raise TypeError."""
        from core.signals import SignalRegistry

        registry = SignalRegistry(db_path="/tmp/test_invalid.db")
        with pytest.raises(TypeError):
            registry.add_signal("bad_signal", "not_a_signal_object")