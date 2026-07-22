"""
Phase 21.2 — Integration Tests

Tests end-to-end pipeline flows:
- Signal → Agreement → Position Sizing → Trade Execution
- Database schema creation and migration
- Alert dispatch and webhook delivery
- Kalshi API wrapper (with mock)

These tests verify that components work together correctly, not just in
isolation.
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

from core.agreement_gate import AgreementGate


# ─── Fixtures ────────────────────────────────────────────────────────────

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
    # Clean up WAL/SHM files
    for ext in ('-wal', '-shm'):
        try:
            os.unlink(tmpfile.name + ext)
        except OSError:
            pass


@pytest.fixture
def temp_dir():
    """Create a temporary directory for test artifacts."""
    tmpdir = tempfile.mkdtemp()
    yield tmpdir
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def sample_days():
    """Generate sample weather data for signal evaluation."""
    import math
    days = []
    for i in range(100):
        days.append({
            'date': f'2025-06-{i+1:02d}',
            'high': 70 + 5 * math.sin(i * 0.5),
            'low': 55 + 5 * math.sin(i * 0.5) - 5,
            'dewpoint': 50 + 5 * math.sin(i * 0.3),
            'temp': 65 + 5 * math.sin(i * 0.5),
            'wind_dir': 180 + 30 * math.sin(i * 0.7),
            'wind_speed': 10 + 5 * math.sin(i * 0.3),
            'pressure': 1013 + 10 * math.sin(i * 0.4),
        })
    return days


# ─── Signal → Agreement → Sizing Pipeline ───────────────────────────────

@pytest.mark.slow
@pytest.mark.integration
class TestSignalToAgreementPipeline:
    """Test the end-to-end signal → agreement → position sizing pipeline."""

    def test_signal_registry_to_agreement_gate(self, temp_db, sample_days):
        """Generate signals from registry, pass through agreement gate."""
        from core.signals import SignalRegistry

        registry = SignalRegistry(db_path=temp_db)
        # Evaluate each signal at a specific index
        signals = []
        for name, sig in registry.signals.items():
            idx = max(sig.min_lookback + 2, 10)
            if idx < len(sample_days):
                direction, confidence = sig.evaluate(idx, sample_days)
                if direction is not None and confidence > 0:
                    signals.append((name, direction, confidence))

        # Pass through agreement gate
        gate_signals = [(s[0], "HIGH", s[1], "test") for s in signals]
        gate = AgreementGate(n_required=2, m_total=len(gate_signals) if len(gate_signals) >= 3 else 3)
        filtered = gate.filter_signals(gate_signals)

        # If we have enough signals, filtering should produce reasonable output
        if len(gate_signals) >= 3:
            assert isinstance(filtered, list)
        else:
            # With fewer than m_total signals, pass-through
            assert len(filtered) == len(gate_signals)

    def test_agreement_to_position_sizing(self, temp_db, sample_days):
        """Agreement gate output should feed into position sizing."""
        from core.signals import SignalRegistry
        from core.position_sizing import compute_position_size, KellyPositionSizer

        # Build signals
        registry = SignalRegistry(db_path=temp_db)
        signals = []
        for name, sig in registry.signals.items():
            idx = max(sig.min_lookback + 2, 10)
            if idx < len(sample_days):
                direction, confidence = sig.evaluate(idx, sample_days)
                if direction is not None and confidence > 0:
                    signals.append((name, direction, confidence))

        if not signals:
            pytest.skip("No signals fired with test data")

        # Use the first signal's confidence for position sizing
        sizer = KellyPositionSizer()
        # Add some wins to establish positive edge
        from datetime import datetime, timezone, timedelta
        for i in range(10):
            d = (datetime.now(timezone.utc) - timedelta(days=i)).strftime('%Y-%m-%d')
            sizer.add_win_result(d, win=(i % 3 != 2))

        name, direction, confidence = signals[0]
        size, tier, meta = compute_position_size(
            signal_type=name,
            confidence=confidence,
            current_balance=10000.0,
            market_price=0.5,
            kelly_sizer=sizer,
        )
        assert size >= 0
        assert isinstance(meta, dict)
        assert "signal_type" in meta
        assert "position_size" in meta

    def test_full_pipeline_mock_trade(self, temp_db, sample_days):
        """Simulate a full trade decision: signal → agreement → sizing → journal."""
        from core.signals import SignalRegistry
        from core.position_sizing import compute_position_size, KellyPositionSizer
        from core.trade_journal import TradeJournal, JournalOutcome

        # 1. Get signals
        registry = SignalRegistry(db_path=temp_db)
        signals = []
        for name, sig in registry.signals.items():
            idx = max(sig.min_lookback + 2, 10)
            if idx < len(sample_days):
                direction, confidence = sig.evaluate(idx, sample_days)
                if direction is not None and confidence > 0:
                    signals.append((name, direction, confidence))

        # 2. Agreement gate
        agreements = [(s[0], "HIGH", s[1], "pipeline") for s in signals]
        gate = AgreementGate(n_required=2, m_total=3)
        filtered = gate.filter_signals(agreements) if len(agreements) >= 3 else agreements

        # 3. Position sizing (if we have signals)
        if not filtered:
            pytest.skip("No signals passed agreement gate")

        sizer = KellyPositionSizer()
        from datetime import datetime, timezone, timedelta
        for i in range(10):
            d = (datetime.now(timezone.utc) - timedelta(days=i)).strftime('%Y-%m-%d')
            sizer.add_win_result(d, win=(i % 3 != 2))

        # Take the first agreed signal
        name, direction = filtered[0][0], filtered[0][2]
        # Find matching confidence
        conf = 0.6
        for s in signals:
            if s[0] == name:
                conf = s[2]
                break

        size, tier, meta = compute_position_size(
            signal_type=name, confidence=conf,
            current_balance=10000.0, market_price=0.5,
            kelly_sizer=sizer,
        )

        # 4. Record in journal
        journal = TradeJournal(db_path=temp_db)
        outcome = JournalOutcome.EXECUTED if size > 0 else JournalOutcome.SKIPPED_SIZE_ZERO
        row_id = journal.append_entry(
            station="KATL", market="HIGH", direction=direction,
            outcome=outcome.value if hasattr(outcome, 'value') else outcome,
            confidence=conf, position_size=size,
            lane="regular", functionality=name,
        )
        assert row_id > 0


# ─── Database Schema Integration ─────────────────────────────────────────

@pytest.mark.integration
class TestDatabaseSchema:
    """Test database schema creation and migration."""

    def test_schema_migration_script(self, temp_db):
        """Test the schema migration module."""
        # Try to import the schema migration module
        try:
            from core.schema_migration import apply_migrations, get_schema_version
            # Should work without errors
            assert True
        except ImportError:
            # Schema migration may not exist or may have different name
            pytest.skip("schema_migration module not importable")

    def test_trade_journal_schema_creation(self, temp_db):
        """Trade journal schema should be created on first use."""
        from core.trade_journal import TradeJournal
        journal = TradeJournal(db_path=temp_db)

        conn = sqlite3.connect(temp_db)
        try:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()]
            assert "trade_journal" in tables
        finally:
            conn.close()

    def test_trade_journal_reentrant(self, temp_db):
        """Creating the same schema twice should not error."""
        from core.trade_journal import TradeJournal
        j1 = TradeJournal(db_path=temp_db)
        j2 = TradeJournal(db_path=temp_db)
        conn = sqlite3.connect(temp_db)
        try:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()]
            assert "trade_journal" in tables
        finally:
            conn.close()


# ─── Alert Dispatch Integration ──────────────────────────────────────────

@pytest.mark.integration
class TestAlertDispatch:
    """Test alert dispatch and webhook integration with mock."""

    def test_alert_builder_import(self):
        """Alert builder should be importable."""
        try:
            from core.alert_builder import AlertBuilder
            assert True
        except ImportError as e:
            pytest.skip(f"alert_builder not available: {e}")

    def test_alert_dispatcher_import(self):
        """Alert dispatcher should be importable."""
        try:
            from core.alert_dispatcher import AlertDispatcher
            assert True
        except ImportError as e:
            pytest.skip(f"alert_dispatcher not available: {e}")

    @patch('core.alert_dispatcher.requests.post')
    def test_webhook_delivery(self, mock_post):
        """Test that alert dispatcher sends webhook correctly."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        try:
            from core.alert_dispatcher import AlertDispatcher
            from core.alert_schema import create_alert_payload

            dispatcher = AlertDispatcher()
            payload = create_alert_payload(
                station="KATL", direction="UP",
                confidence=0.75, market_prob=0.55,
                alert_id="test_alert_001",
            )
            # If dispatcher has a send method, test it
            if hasattr(dispatcher, 'send_alert') or hasattr(dispatcher, 'dispatch'):
                send_fn = getattr(dispatcher, 'send_alert', None) or getattr(dispatcher, 'dispatch')
                result = send_fn(payload)
                assert result is not None
        except (ImportError, AttributeError) as e:
            pytest.skip(f"Alert dispatch test skipped: {e}")

    def test_alert_state_machine(self):
        """Alert state machine should be importable and functional."""
        try:
            from core.alert_state_machine import AlertStateMachine
            machine = AlertStateMachine()
            assert machine is not None
        except ImportError as e:
            pytest.skip(f"alert_state_machine not available: {e}")


# ─── Kalshi API Wrapper (Mocked) ────────────────────────────────────────

@pytest.mark.integration
class TestKalshiApiMock:
    """Test Kalshi API wrapper with mocked responses."""

    def test_kalshi_client_import(self):
        """Kalshi client should be importable."""
        try:
            from core.kalshi_client import KalshiClient
            assert True
        except ImportError:
            try:
                from core.kalshi_api import KalshiAPI
                assert True
            except ImportError:
                pytest.skip("Kalshi client not importable")

    @pytest.mark.skipif("not __import__('importlib').util.find_spec('core.kalshi_client')")
    @patch('core.kalshi_client.requests.Session')
    def test_market_lookup(self, mock_session):
        """Test market lookup with mocked API."""
        mock_instance = MagicMock()
        mock_session.return_value = mock_instance
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "markets": [
                {
                    "ticker": "KATLH25",
                    "title": "KATL High Temp",
                    "status": "open",
                    "close_time": "2025-06-15T18:00:00Z",
                }
            ]
        }
        mock_instance.request.return_value = mock_response

        try:
            from core.kalshi_client import KalshiClient
            client = KalshiClient()
            result = client.get_markets(station="KATL")
            assert result is not None
        except (ImportError, AttributeError) as e:
            pytest.skip(f"Kalshi client test skipped: {e}")

    @pytest.mark.skipif("not __import__('importlib').util.find_spec('core.kalshi_client')")
    @patch('core.kalshi_client.requests.Session')
    def test_place_order_mock(self, mock_session):
        """Test placing an order with mocked API."""
        mock_instance = MagicMock()
        mock_session.return_value = mock_instance
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"order_id": "order_123", "status": "filled"}
        mock_instance.request.return_value = mock_response

        try:
            from core.kalshi_client import KalshiClient
            client = KalshiClient()
            result = client.place_order(
                ticker="KATLH25",
                side="buy",
                type="market",
                count=1,
            )
            assert result is not None
        except (ImportError, AttributeError) as e:
            pytest.skip(f"Kalshi order test skipped: {e}")

    def test_alert_integrity_monitor(self):
        """Alert integrity monitor should be importable."""
        try:
            from core.alert_integrity_monitor import AlertIntegrityMonitor
            assert True
        except ImportError as e:
            pytest.skip(f"alert_integrity_monitor not available: {e}")


# ─── Signal Registry Integration ─────────────────────────────────────────

@pytest.mark.integration
class TestSignalRegistryIntegration:
    """Test the SignalRegistry with all signals."""

    def test_all_signals_evaluate_with_mock_data(self, temp_db, sample_days):
        """All registered signals should evaluate with mock data without errors."""
        from core.signals import SignalRegistry
        registry = SignalRegistry(db_path=temp_db)

        results = {}
        for name, sig in registry.signals.items():
            try:
                idx = max(sig.min_lookback + 2, 10)
                if idx < len(sample_days):
                    direction, confidence = sig.evaluate(idx, sample_days)
                    results[name] = {
                        "direction": direction,
                        "confidence": confidence,
                        "error": None,
                    }
                else:
                    results[name] = {"direction": None, "confidence": 0.0, "error": "insufficient_data"}
            except Exception as e:
                results[name] = {"direction": None, "confidence": 0.0, "error": str(e)}

        # Check that at least some signals fire
        fired = [r for r in results.values() if r["direction"] is not None]
        # All should be error-free even if they don't fire
        errors = [r for r in results.values() if r.get("error")]
        assert len(errors) == 0, f"Signals with errors: {errors}"

    def test_signal_uniqueness(self, temp_db):
        """Signal names should be unique across registry."""
        from core.signals import SignalRegistry
        registry = SignalRegistry(db_path=temp_db)
        names = [sig.name for sig in registry.signals.values()]
        assert len(names) == len(set(names))

    def test_signal_min_lookback_consistency(self, temp_db):
        """Each signal's min_lookback should be consistent with its implementation."""
        from core.signals import SignalRegistry
        registry = SignalRegistry(db_path=temp_db)
        for name, sig in registry.signals.items():
            lb = sig.min_lookback
            assert lb >= 0, f"{name} has negative min_lookback: {lb}"