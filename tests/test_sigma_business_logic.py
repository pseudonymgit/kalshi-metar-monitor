"""
E9 — Sigma authoring: business-logic tests (Σ.1 P1).

Covers the trading business-logic layer that drives the intraday cycle:

  Σ.1f — RiskManager (core.risk_controls): daily-loss, drawdown,
         consecutive-loss checks and RiskState aggregation.
  Σ.1g — StopLossMonitor (core.stop_loss): multi-condition stop conditions
         and the recovery protocol.
  Σ.1h — intraday_daemon: PID/state helpers and the fail-safe loop contract.
  Σ.1i — intraday_runner: trading-hours gating, station universe, and
         freshness-warning plumbing.

All deterministic, no network, no DB writes, no daemon restart. Pure math and
helper-function contracts only — the actual `run()` loop and `run_intraday_cycle`
are NOT executed (they would touch DBs / network / wall-clock sleep).

Production code is untouched; these tests pin existing behavior.
"""

import datetime
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.risk_controls import (
    RiskConfig,
    RiskManager,
    RiskState,
    TradeResult,
    DEFAULT_RISK_CONFIG,
)
from core.stop_loss import (
    PRIMARY_LOSS_LIMIT,
    PRIMARY_DAY_LIMIT,
    DRAWDOWN_STOP_PCT,
    WIN_RATE_STOP_PCT,
    SHARPE_STOP_THRESHOLD,
    WIN_RATE_WINDOW,
    SHARPE_WINDOW,
    COOLDOWN_DAYS,
    PAPER_TRADES_REQUIRED,
    StopLossMonitor,
)
from scripts import intraday_daemon as daemon
from scripts import intraday_runner as runner


# ──────────────────────────────────────────────────────────────
# Σ.1f — RiskManager
# ──────────────────────────────────────────────────────────────

class TestRiskManager:
    def test_default_config_thresholds(self):
        cfg = DEFAULT_RISK_CONFIG
        assert 0.0 < cfg.max_daily_loss_percentage <= 1.0
        assert 0.0 < cfg.max_drawdown_percent <= 1.0
        assert cfg.max_consecutive_losses >= 1
        assert cfg.initial_capital > 0.0

    def test_initial_state_passes_all_checks(self):
        rm = RiskManager()
        assert rm.check_daily_loss()[0] is True
        assert rm.check_drawdown()[0] is True
        assert rm.check_consecutive_losses()[0] is True
        state = rm.evaluate()
        assert state.passed is True
        assert state.halted is False

    def test_profitable_trade_passes(self):
        rm = RiskManager()
        tr = TradeResult("t1", pnl=10.0, is_profitable=True, trade_date="2026-09-17")
        result = rm.update_after_trade(tr)
        assert result["passed"] is True

    def test_consecutive_losses_threshold_halts(self):
        rm = RiskManager()
        limit = rm.config.max_consecutive_losses
        # One loss under the limit → still passing
        rm.update_after_trade(TradeResult("t", pnl=-1.0, is_profitable=False, trade_date="2026-09-17"))
        assert rm.consecutive_losses == 1
        # Exceed the limit
        for i in range(limit):
            rm.update_after_trade(TradeResult(f"t{i}", pnl=-1.0, is_profitable=False, trade_date="2026-09-17"))
        assert rm.check_consecutive_losses()[0] is False

    def test_win_resets_consecutive_losses(self):
        rm = RiskManager()
        rm.update_after_trade(TradeResult("a", pnl=-1.0, is_profitable=False, trade_date="2026-09-17"))
        rm.update_after_trade(TradeResult("b", pnl=-1.0, is_profitable=False, trade_date="2026-09-17"))
        assert rm.consecutive_losses == 2
        rm.update_after_trade(TradeResult("c", pnl=1.0, is_profitable=True, trade_date="2026-09-17"))
        assert rm.consecutive_losses == 0

    def test_daily_loss_exceeds_percentage_halts(self):
        rm = RiskManager()
        # Each loss of 6% of initial capital → 1 trade already exceeds 5%
        big_loss = TradeResult("big", pnl=-rm.config.initial_capital * 0.06,
                               is_profitable=False, trade_date="2026-09-17")
        rm.update_after_trade(big_loss)
        assert rm.check_daily_loss()[0] is False

    def test_daily_loss_within_limit_passes(self):
        rm = RiskManager()
        small = TradeResult("small", pnl=-rm.config.initial_capital * 0.02,
                            is_profitable=False, trade_date="2026-09-17")
        rm.update_after_trade(small)
        assert rm.check_daily_loss()[0] is True

    def test_drawdown_from_peak_halts(self):
        rm = RiskManager()
        # Build a peak first
        rm.update_after_trade(TradeResult("win", pnl=500.0, is_profitable=True, trade_date="2026-09-17"))
        peak = rm.peak_capital
        # Lose enough to exceed max_drawdown_percent (default 15%)
        # Need current_capital < peak * (1 - 0.15)
        target_loss = peak * rm.config.max_drawdown_percent + 1.0
        rm.update_after_trade(TradeResult("crash", pnl=-target_loss,
                                          is_profitable=False, trade_date="2026-09-17"))
        assert rm.check_drawdown()[0] is False

    def test_daily_reset_on_new_day(self):
        rm = RiskManager()
        rm.update_after_trade(TradeResult("a", pnl=-10.0, is_profitable=False, trade_date="2026-09-17"))
        assert rm.daily_pnl == -10.0
        # New day → daily P&L resets
        rm.update_after_trade(TradeResult("b", pnl=-5.0, is_profitable=False, trade_date="2026-09-18"))
        assert rm.daily_pnl == -5.0

    def test_evaluate_returns_risk_state_with_metrics(self):
        rm = RiskManager()
        rm.update_after_trade(TradeResult("a", pnl=5.0, is_profitable=True, trade_date="2026-09-17"))
        state = rm.evaluate()
        assert isinstance(state, RiskState)
        assert state.metrics.total_trades == 1
        assert state.metrics.winning_trades == 1

    def test_trade_result_from_dict(self):
        d = {"trade_id": "x", "pnl": 3.5, "is_profitable": True, "trade_date": "2026-09-17"}
        tr = TradeResult.from_dict(d)
        assert tr.trade_id == "x"
        assert tr.pnl == 3.5
        assert tr.is_profitable is True


# ──────────────────────────────────────────────────────────────
# Σ.1g — StopLossMonitor
# ──────────────────────────────────────────────────────────────

class TestStopLossMonitor:
    def test_no_stops_with_winning_trades(self):
        slm = StopLossMonitor(budget=250.0)
        for i in range(10):
            slm.record_trade(1.5, True, f"2026-09-{i+1:02d}")
        stopped, reason, _ = slm.check_stop_conditions()
        assert stopped is False

    def test_primary_loss_limit(self):
        slm = StopLossMonitor(budget=250.0, enable_day_limit=False)
        for i in range(PRIMARY_LOSS_LIMIT):
            slm.record_trade(-1.0, False, f"2026-09-{i+1:02d}")
        stopped, reason, details = slm.check_stop_conditions()
        assert stopped is True
        assert details["trigger"] == "primary_loss_limit"

    def test_day_limit_elapsed(self):
        slm = StopLossMonitor(budget=250.0, enable_day_limit=True)
        # Two trades >= PRIMARY_DAY_LIMIT days apart
        slm.record_trade(1.0, True, "2026-01-01")
        slm.record_trade(1.0, True, "2026-02-15")
        stopped, reason, details = slm.check_stop_conditions()
        assert stopped is True
        assert details["trigger"] == "primary_day_limit"

    def test_day_limit_disabled_when_flag_false(self):
        slm = StopLossMonitor(budget=250.0, enable_day_limit=False)
        slm.record_trade(1.0, True, "2026-01-01")
        slm.record_trade(1.0, True, "2026-02-15")
        stopped, _, _ = slm.check_stop_conditions()
        # Day limit disabled → no stop from calendar elapsed
        assert stopped is False

    def test_drawdown_stop(self):
        slm = StopLossMonitor(budget=250.0, enable_day_limit=False)
        # Build peak
        for i in range(5):
            slm.record_trade(5.0, True, f"2026-09-{i+1:02d}")
        # Lose more than 10% of budget ($25)
        for i in range(5):
            slm.record_trade(-6.0, False, f"2026-09-{i+6:02d}")
        stopped, reason, details = slm.check_stop_conditions()
        assert stopped is True
        assert details["trigger"] == "drawdown"

    def test_win_rate_stop(self):
        slm = StopLossMonitor(budget=250.0, enable_day_limit=False)
        # 10 wins then 10 losses → 50% < 52% over rolling 20
        for i in range(10):
            slm.record_trade(2.0, True, f"2026-09-{i+1:02d}")
        for i in range(10):
            slm.record_trade(-2.0, False, f"2026-09-{i+11:02d}")
        stopped, reason, details = slm.check_stop_conditions()
        assert stopped is True
        assert details["trigger"] == "win_rate"

    def test_sharpe_stop(self):
        # Rolling 50 with negative mean PnL → Sharpe < 0.5, while the rolling-20
        # win rate stays >= 52% so win-rate doesn't fire first.
        slm = StopLossMonitor(budget=250.0, enable_day_limit=False)
        first30 = [True] * 24 + [False] * 6
        last20 = [True] * 11 + [False] * 9
        pattern = first30 + last20
        assert len(pattern) == SHARPE_WINDOW
        base = datetime.datetime(2026, 9, 1)
        for i, is_win in enumerate(pattern):
            pnl = 0.1 if is_win else -0.8
            d = (base + datetime.timedelta(days=i)).strftime("%Y-%m-%d")
            slm.record_trade(pnl, is_win, d)
        stopped, reason, details = slm.check_stop_conditions()
        assert stopped is True
        assert details["trigger"] == "sharpe"

    def test_recovery_requires_cooldown_then_paper_trades(self):
        slm = StopLossMonitor(budget=250.0, enable_day_limit=False)
        for i in range(PRIMARY_LOSS_LIMIT):
            slm.record_trade(-1.0, False, f"2026-09-{i+1:02d}")
        stopped, _, _ = slm.check_stop_conditions()
        assert stopped is True

        # Immediately after stop → cooldown not met
        stop_date = slm._stop_date
        recovered, _ = slm.attempt_recovery(stop_date.strftime("%Y-%m-%d"))
        assert recovered is False

        # After cooldown but no paper trades
        after_cooldown = stop_date + datetime.timedelta(days=COOLDOWN_DAYS + 1)
        recovered, _ = slm.attempt_recovery(after_cooldown.strftime("%Y-%m-%d"))
        assert recovered is False

        # After cooldown + 10 paper trades
        for _ in range(PAPER_TRADES_REQUIRED):
            slm.record_paper_trade(True)
        recovered, _ = slm.attempt_recovery(after_cooldown.strftime("%Y-%m-%d"))
        assert recovered is True
        assert slm._state == "ACTIVE"

    def test_paper_trade_failure_resets_count(self):
        slm = StopLossMonitor(budget=250.0, enable_day_limit=False)
        slm._state = "RECOVERING"
        slm._stop_date = datetime.datetime(2026, 9, 1)
        for _ in range(5):
            slm.record_paper_trade(True)
        assert len(slm._paper_trade_results) == 5
        slm.record_paper_trade(False)  # failure resets
        assert len(slm._paper_trade_results) == 0

    def test_evaluate_state_snapshot(self):
        slm = StopLossMonitor(budget=250.0, enable_day_limit=False)
        slm.record_trade(1.0, True, "2026-09-01")
        snap = slm.evaluate_state()
        assert snap["total_trades"] == 1
        assert snap["state"] == "ACTIVE"
        assert "drawdown_pct" in snap
        assert "paper_trades_required" in snap

    def test_drawdown_zero_on_empty(self):
        slm = StopLossMonitor(budget=250.0, enable_day_limit=False)
        assert slm._calculate_drawdown() == 0.0


# ──────────────────────────────────────────────────────────────
# Σ.1h — intraday_daemon
# ──────────────────────────────────────────────────────────────

class TestIntradayDaemon:
    def test_pid_write_and_remove(self, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon, "PID_FILE", str(tmp_path / "daemon.pid"))
        daemon._write_pid()
        pid_path = Path(daemon.PID_FILE)
        assert pid_path.exists()
        assert int(pid_path.read_text()) == os.getpid()
        daemon._remove_pid()
        assert not pid_path.exists()

    def test_save_state_updates_last_updated(self, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon, "STATE_FILE", str(tmp_path / "state.json"))
        daemon._save_state(dry_run=True)
        assert "last_updated" in daemon._STATE
        assert daemon._STATE["dry_run"] is True
        assert Path(daemon.STATE_FILE).exists()

    def test_remove_pid_noop_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon, "PID_FILE", str(tmp_path / "nope.pid"))
        # Should not raise when the PID file does not exist
        daemon._remove_pid()

    def test_check_pid_writes_pid_when_no_existing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon, "PID_FILE", str(tmp_path / "fresh.pid"))
        daemon._check_pid()
        assert Path(daemon.PID_FILE).exists()


# ──────────────────────────────────────────────────────────────
# Σ.1i — intraday_runner
# ──────────────────────────────────────────────────────────────

class TestIntradayRunner:
    def test_trading_hours_boundaries(self):
        # 14:00–20:59 UTC inclusive of 14, exclusive of 21
        assert runner._is_trading_hours(14) is True
        assert runner._is_trading_hours(20) is True
        assert runner._is_trading_hours(13) is False
        assert runner._is_trading_hours(21) is False

    def test_station_universe_size(self):
        # 20 stations, all uppercase ICAO
        assert len(runner.STATIONS) == 20
        for st in runner.STATIONS:
            assert st == st.upper()
            assert st.startswith("K")

    def test_risk_defaults_positive(self):
        assert 0.0 < runner.DEFAULT_MIN_PROB_GATE < 1.0
        assert runner.DEFAULT_MAX_POSITION_USD > 0.0
        assert 0.0 < runner.DEFAULT_STOP_LOSS_PCT < 1.0

    def test_current_hour_utc_is_valid(self):
        hour = runner._get_current_hour_utc()
        assert 0 <= hour <= 23

    def test_freshness_warnings_for_missing_dbs(self, tmp_path, monkeypatch):
        # Point DB paths at a missing directory → every source reports NOT FOUND
        monkeypatch.setattr(runner, "METAR_DB", tmp_path / "missing" / "metar.db")
        monkeypatch.setattr(runner, "ASOS_DB", tmp_path / "missing" / "asos.db")
        monkeypatch.setattr(runner, "NWP_DB", tmp_path / "missing" / "nwp.db")
        monkeypatch.setattr(runner, "GEFS_DB", tmp_path / "missing" / "gefs.db")
        warnings = runner._check_data_freshness()
        assert len(warnings) == 4
        for w in warnings:
            assert "NOT FOUND" in w

    def test_freshness_warnings_empty_for_fresh_files(self, tmp_path, monkeypatch):
        # Freshly-created files → no warnings
        for name in ("metar.db", "asos.db", "nwp.db", "gefs.db"):
            (tmp_path / name).write_text("")
        monkeypatch.setattr(runner, "METAR_DB", tmp_path / "metar.db")
        monkeypatch.setattr(runner, "ASOS_DB", tmp_path / "asos.db")
        monkeypatch.setattr(runner, "NWP_DB", tmp_path / "nwp.db")
        monkeypatch.setattr(runner, "GEFS_DB", tmp_path / "gefs.db")
        assert runner._check_data_freshness() == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
