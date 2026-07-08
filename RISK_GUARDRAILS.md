# Weather Engine - Risk Guardrails (Phase 1)
Date: 2026-07-08 | Review: Marty Byrde (B1.5)
max_daily_loss: $300 - halt + alert
max_drawdown_pct: 10% - suspension
consecutive_loss_limit: 5 - kill switch
correlation_threshold: 0.70 - kill switch
signal_conflict_threshold: 0.95 (2d) - kill switch
skill_gated_stations: KNYC KLAX KMDW KBOS KATL KSFO KSEA only
Usage: from core.risk_controls import risk_manager
if not risk_manager.is_station_allowed(station): return
if not (risk_manager.check_daily_loss() and risk_manager.check_drawdown() and risk_manager.check_consecutive_losses()): return
risk_manager.update_after_trade(pnl)
Status: Implemented.
