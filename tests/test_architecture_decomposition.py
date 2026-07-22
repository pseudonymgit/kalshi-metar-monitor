"""
Phase 21.3 — Architecture Decomposition Tests

Tests that facade modules re-export correctly after Phase 20 monolith extraction:
- signal_fusion.py: re-exports from fusion_logic.py + compatibility_checks.py
- paper_trading_engine.py: re-exports from signal_pipeline, trade_execution, pnl_tracking, settlement_processor
- kalshi_monitor.py: re-exports from market_monitor, price_fetcher, order_manager
- metar_monitor.py: re-exports from data_collector, data_processor, health_monitor
- signal registry import paths
- __all__ export accessibility
"""

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ─── Signal Fusion Facade ────────────────────────────────────────────────────

@pytest.mark.unit
class TestSignalFusionFacade:
    """Test signal_fusion.py facade re-exports."""

    def test_signal_fusion_engine_imported(self):
        from core.signal_fusion import SignalFusionEngine
        assert SignalFusionEngine is not None

    def test_time_decay_manager_imported(self):
        from core.signal_fusion import TimeDecaySignalManager
        assert TimeDecaySignalManager is not None

    def test_standalone_functions_imported(self):
        from core.signal_fusion import (
            apply_conflict_modulation,
            compute_weights_from_significance,
            dempster_shafer_conflict,
            mutual_information_from_boolean_pairs,
            mutual_information_matrix,
            mutual_information_simple_correlation,
            unique_information_fraction,
        )
        assert callable(apply_conflict_modulation)
        assert callable(compute_weights_from_significance)
        assert callable(dempster_shafer_conflict)
        assert callable(mutual_information_from_boolean_pairs)
        assert callable(mutual_information_matrix)
        assert callable(mutual_information_simple_correlation)
        assert callable(unique_information_fraction)

    def test_compatibility_checks_imported(self):
        from core.signal_fusion import demonstrate_fusion_stack
        assert callable(demonstrate_fusion_stack)

    def test_all_exports_accessible(self):
        from core import signal_fusion
        for name in signal_fusion.__all__:
            assert hasattr(signal_fusion, name), f"{name} missing from signal_fusion"

    def test_signal_fusion_engine_type(self):
        """Verify SignalFusionEngine is a class with expected methods."""
        from core.signal_fusion import SignalFusionEngine
        expected_methods = [
            'fuse_signals', 'compute_signal_agreement_score',
            'compute_local_ece', 'compute_conviction_score',
            'should_take_trade', 'decompose_brier_score',
            'adjust_confidence_by_regime', 'update_signal_weights_bayesian',
            'enhance_with_agreement_and_conviction',
            'get_recent_calibration_performance', 'fit_calibration',
            'compute_fusion_weights',
        ]
        # SignalFusionEngine requires signal_names and city_codes at init
        import inspect
        sig = inspect.signature(SignalFusionEngine.__init__)
        params = list(sig.parameters.keys())
        assert 'signal_names' in params
        assert 'city_codes' in params
        for method in expected_methods:
            assert hasattr(SignalFusionEngine, method), f"SignalFusionEngine missing method: {method}"

    def test_time_decay_manager_methods(self):
        """Verify TimeDecaySignalManager has expected methods."""
        from core.signal_fusion import TimeDecaySignalManager
        expected_methods = [
            'update', 'compute_reliability', 'adjust_confidence',
            'get_lop_weight', 'get_all_reliabilities', 'get_reliability_report',
        ]
        for method in expected_methods:
            assert hasattr(TimeDecaySignalManager, method), f"TimeDecaySignalManager missing method: {method}"


# ─── Paper Trading Engine Facade ─────────────────────────────────────────────

@pytest.mark.unit
class TestPaperTradingEngineFacade:
    """Test paper_trading_engine.py facade re-exports."""

    def test_paper_trader_imported(self):
        from core.paper_trading_engine import PaperTrader
        assert PaperTrader is not None

    def test_facade_imports_from_signal_pipeline(self):
        """Verify re-exports from signal_pipeline work."""
        from core.paper_trading_engine import generate_signals
        assert callable(generate_signals)

    def test_facade_imports_from_trade_execution(self):
        """Verify re-exports from trade_execution are accessible."""
        from core.paper_trading_engine import PaperTrader
        # PaperTrader should have expected methods
        assert hasattr(PaperTrader, 'check_kill_switches')
        assert hasattr(PaperTrader, 'risk_report')

    def test_settlement_processor_imported(self):
        """Verify SettlementProcessor re-exported."""
        from core.paper_trading_engine import SettlementProcessor
        assert SettlementProcessor is not None

    def test_import_from_core_works(self):
        """Import path: core.paper_trading_engine has key attributes."""
        import core.paper_trading_engine as pte
        assert hasattr(pte, 'PaperTrader')
        assert hasattr(pte, 'generate_signals')


# ─── Kalshi Monitor Facade ───────────────────────────────────────────────────

@pytest.mark.unit
class TestKalshiMonitorFacade:
    """Test kalshi_monitor.py facade re-exports."""

    def test_kalshi_monitor_imported(self):
        import core.kalshi_monitor
        assert core.kalshi_monitor is not None

    def test_market_monitor_exports(self):
        """Verify market_monitor re-exports accessible."""
        from core.kalshi_monitor import (
            get_default_config, map_market_to_station,
            discover_kalshi_weather_markets, get_state,
        )
        assert callable(get_default_config)
        assert callable(map_market_to_station)
        assert callable(discover_kalshi_weather_markets)

    def test_price_fetcher_exports(self):
        """Verify price_fetcher re-exports accessible."""
        from core.kalshi_monitor import get_public_markets
        assert callable(get_public_markets)

    def test_order_manager_exports(self):
        """Verify order_manager re-exports accessible."""
        from core.kalshi_monitor import (
            kalshi_execution_domain, set_kalshi_execution_domain,
            reset_kalshi_execution_domain, process_ladder_transition,
        )
        assert kalshi_execution_domain is not None or callable(kalshi_execution_domain)
        assert callable(set_kalshi_execution_domain)
        assert callable(reset_kalshi_execution_domain)

    def test_import_from_core_works(self):
        """Import path: core.kalshi_monitor has key attributes."""
        import core.kalshi_monitor as km
        assert hasattr(km, 'get_default_config')
        assert hasattr(km, 'get_public_markets')
        assert hasattr(km, 'kalshi_execution_domain')
        assert hasattr(km, '__all__')


# ─── METAR Monitor Facade ────────────────────────────────────────────────────

@pytest.mark.unit
class TestMetarMonitorFacade:
    """Test metar_monitor.py facade re-exports."""

    def test_metar_monitor_imported(self):
        import core.metar_monitor
        assert core.metar_monitor is not None

    def test_data_collector_exports(self):
        """Verify data_collector re-exports accessible."""
        from core.metar_monitor import (
            fetch_window, fetch_latest, fetch_now, get_latest_metar,
            verify_webhook_signature,
        )
        assert callable(fetch_window)
        assert callable(fetch_latest)
        assert callable(fetch_now)
        assert callable(get_latest_metar)
        assert callable(verify_webhook_signature)

    def test_data_processor_exports(self):
        """Verify data_processor re-exports accessible."""
        from core.metar_monitor import (
            reset_station_daily_state, get_default_config,
            ensure_state_loaded, get_state, get_latest_station_signal_runtime,
        )
        assert callable(reset_station_daily_state)
        assert callable(get_default_config)
        assert callable(ensure_state_loaded)

    def test_health_monitor_exports(self):
        """Verify health_monitor re-exports accessible."""
        from core.metar_monitor import (
            get_station_ingestion_runtime, get_alert_review_diagnostics,
            get_recent_alerts, prune_old_alerts,
        )
        assert callable(get_station_ingestion_runtime)
        assert callable(get_alert_review_diagnostics)
        assert callable(get_recent_alerts)

    def test_import_from_core_works(self):
        """Import path: core.metar_monitor has key attributes."""
        import core.metar_monitor as mm
        assert hasattr(mm, 'fetch_window')
        assert hasattr(mm, 'reset_station_daily_state')
        assert hasattr(mm, 'get_station_ingestion_runtime')
        assert hasattr(mm, '__all__')


# ─── Signal Registry Import Paths ─────────────────────────────────────────────

@pytest.mark.unit
class TestSignalRegistryImportPaths:
    """Test that signal registry import paths work correctly."""

    def test_registry_from_core_signals(self):
        from core.signals import SignalRegistry, create_signal_registry
        assert SignalRegistry is not None
        assert callable(create_signal_registry)

    def test_base_signal_importable(self):
        from core.signals.base_signal import BaseSignal, _window, _safe_get, validate_signal
        assert BaseSignal is not None
        assert callable(_window)
        assert callable(_safe_get)
        assert callable(validate_signal)

    def test_all_signal_modules_importable(self):
        """Verify each signal module can be imported independently."""
        signal_modules = [
            'core.signals.base_signal',
            'core.signals.persistence_signal',
            'core.signals.gaussian_signal',
            'core.signals.gaussian_v2_signal',
            'core.signals.pressure_delta_signal',
            'core.signals.forecast_disagreement_signal',
            'core.signals.calendar_climatology_signal',
            'core.signals.temperature_advection_signal',
            'core.signals.frontal_detector_signal',
            'core.signals.wind_direction_shift',
            'core.signals.nwp_direct_signal',
            'core.signals.goldilocks_signal',
            'core.signals.intraday_metar_confirmation_signal',
            'core.signals.intraday_metar_confirmation',
            'core.signals.fogr_reversion_signal',
            'core.signals.metar_dtdt_signal',
            'core.signals.pressure_tendency_signal',
            'core.signals.hrrr_bias_corrected_signal',
            'core.signals.esdr_signal',
            'core.signals.nwp_dtdt_fusion_signal',
            'core.signals.spread_based_entry_signal',
            'core.signals.volume_momentum_signal',
            'core.signals.settlement_arbitrage_signal',
            'core.signals.dual_polarity_signal',
        ]
        import importlib
        for mod_path in signal_modules:
            try:
                mod = importlib.import_module(mod_path)
                assert mod is not None, f"Failed to import {mod_path}"
            except ImportError as e:
                pytest.fail(f"Cannot import {mod_path}: {e}")

    def test_registry_creates_all_signals(self):
        """Verify SignalRegistry creates all 22 signals without error."""
        from core.signals import SignalRegistry
        registry = SignalRegistry(db_path="/tmp/test_registry_paths.db")
        assert len(registry.signals) == 23
        for name, sig in registry.signals.items():
            assert hasattr(sig, 'evaluate'), f"{name} missing evaluate"
            assert hasattr(sig, 'name'), f"{name} missing name"
            assert hasattr(sig, 'min_lookback'), f"{name} missing min_lookback"

    def test_factory_creates_registry(self):
        """Verify factory function works."""
        from core.signals import create_signal_registry
        registry = create_signal_registry(db_path="/tmp/test_factory.db")
        assert len(registry.signals) == 23


# ─── Core Package Import Paths ────────────────────────────────────────────────

@pytest.mark.unit
class TestCorePackageImportPaths:
    """Test that core package imports work correctly."""

    def test_core_init_imports(self):
        import core
        assert hasattr(core, 'sqlite_utils')
        assert hasattr(core, 'db_schema')
        assert hasattr(core, 'signal_fusion')
        assert hasattr(core, 'trading_modes')
        assert hasattr(core, 'paper_trader')
        assert hasattr(core, 'live_trader')
        assert hasattr(core, 'signals')

    def test_sqlite_utils_importable(self):
        from core.sqlite_utils import (
            get_sqlite_connection, get_pooled_connection,
            ensure_schema, run_migration_test, get_schema_report,
        )
        assert callable(get_sqlite_connection) or callable(get_pooled_connection)
        assert callable(ensure_schema) or callable(run_migration_test)

    def test_db_schema_importable(self):
        from core.db_schema import ensure_all_tables, get_table_names
        assert callable(ensure_all_tables) or callable(get_table_names)

    def test_trading_modes_importable(self):
        from core.trading_modes import TradingMode, TradingModeConfig, get_config_for_mode
        assert TradingMode is not None
        assert callable(get_config_for_mode)

    def test_paper_trader_importable(self):
        from core.paper_trader import PaperTrader
        assert PaperTrader is not None

    def test_live_trader_importable(self):
        from core.live_trader import LiveTrader
        assert LiveTrader is not None

    def test_position_sizing_importable(self):
        from core.position_sizing import (
            ConfidenceTier, KellyPositionSizer, compute_position_size,
        )
        assert ConfidenceTier is not None
        assert callable(KellyPositionSizer) or isinstance(KellyPositionSizer, type)

    def test_agreement_gate_importable(self):
        from core.agreement_gate import AgreementGate, SimpleAgreementChecker
        assert AgreementGate is not None
        assert SimpleAgreementChecker is not None

    def test_trade_journal_importable(self):
        from core.trade_journal import TradeJournal, record_decision
        assert TradeJournal is not None
        assert callable(record_decision)


# ─── Regression: Same Behavior as Before Extraction ──────────────────────────

@pytest.mark.unit
class TestBehavioralRegression:
    """Verify behavior is the same after facades were extracted."""

    def test_registry_import_path_unchanged(self):
        """from core.signals import SignalRegistry should still work."""
        from core.signals import SignalRegistry
        registry = SignalRegistry(db_path="/tmp/test_regression.db")
        assert len(registry.signals) == 23

    def test_agreement_gate_import_path_unchanged(self):
        """from core.agreement_gate import AgreementGate should still work."""
        from core.agreement_gate import AgreementGate
        gate = AgreementGate()
        assert gate.n_required == 3

    def test_signal_fusion_import_path_unchanged(self):
        """from core.signal_fusion import * should work."""
        from core.signal_fusion import SignalFusionEngine, TimeDecaySignalManager
        # Verify classes are importable and accessible
        assert SignalFusionEngine is not None
        assert TimeDecaySignalManager is not None
        # Verify they have expected methods
        assert hasattr(SignalFusionEngine, 'fuse_signals')
        assert hasattr(TimeDecaySignalManager, 'compute_reliability')

    def test_kalshi_monitor_import_path_unchanged(self):
        """from core.kalshi_monitor import * should work."""
        import core.kalshi_monitor as km
        assert hasattr(km, 'get_default_config')
        assert hasattr(km, 'kalshi_execution_domain')

    def test_metar_monitor_import_path_unchanged(self):
        """from core.metar_monitor import * should work."""
        import core.metar_monitor as mm
        assert hasattr(mm, 'fetch_window')
        assert hasattr(mm, 'get_state')