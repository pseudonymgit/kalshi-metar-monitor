#!/usr/bin/env python3
"""
Multi-Instance Paper Trading Runner (v1.0 — 2026-07-05)

Runs paper trading in three isolated instances:
  - PROD: Production-like, conservative sizing, real alerts
  - DEV:  Development, smaller sizing, test alerts
  - SBOX: Sandbox, tiny sizing, no real alerts

Each instance has:
  - Separate ledger DB (data/paper_trading_{instance}.db)
  - Separate config (position sizing, fees, balance)
  - Separate Discord webhook (or disabled)

Discord Alert Format:
  📍 Station: KDEN
  📊 Market: HIGH
  📈 Direction: UP
  💰 Size: $93.75
  🌡️ Current bucket: 87
  🎯 Trading bucket: 88
  📉 Market odds: 0.55
  ✅ Trade Conf: HIGH (0.85)
  🔝 Top signals: near_boundary_momentum_up (conf=0.85), late_day_momentum_hourly (conf=0.70)
  📊 Sharpe: 1.2 | Coverage: 12/20 stations
  💵 Running P&L: +$127.50

Alert fires on every non-zero recommended position size.
Trade Conf is informational only — never a hard gate.

No AI execution in the paper trading loop. Scripts only.

Version: v1.0 2026-07-05
"""

import sqlite3
import json
import os
import sys
import time
import math
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
import urllib.request

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "core"))

from position_sizing import (
    compute_position_size,
    extract_confidence_from_signal_context,
    get_config_for_instance,
    ConfidenceTier,
)
from late_day_momentum_hourly import late_day_momentum_hourly as _ldm_hourly_signal
from paper_trading_engine import PaperTrader, MarketSide, TradeType
from kalshi_price_fetcher import build_market_url as _build_market_url
from instance_config import (
    INSTANCE_CONFIGS as _INSTANCE_CONFIGS,
    InstanceConfig,
    InstanceLock,
    write_health_status,
    read_health_status,
    log_alert,
    setup_instance_logger,
)
from alert_builder import (
    build_paper_trade_alert,
    compute_opportunity_grade,
    classify_lane,
    OpportunityGrade,
    LaneType,
)

# ─── Instance Configuration ─────────────────────────────────────────────

# Instance configs are imported from core/instance_config.py
# The old local INSTANCE_CONFIGS dict is replaced by _INSTANCE_CONFIGS

# Keep the local reference for backward compat in this file
INSTANCE_CONFIGS = _INSTANCE_CONFIGS


# ─── Multi-Instance Runner ──────────────────────────────────────────────

class MultiInstancePaperTrader:
    """
    Manages paper trading across PROD/DEV/SBOX instances.
    Each instance has its own DB, config, and Discord webhook.
    """
    
    def __init__(self, instances: List[str] = None):
        if instances is None:
            instances = ["DEV"]  # Default to DEV only
        
        self.instances = {}
        for name in instances:
            name = name.upper().strip()
            if name in INSTANCE_CONFIGS:
                cfg = INSTANCE_CONFIGS[name]
                trader = PaperTrader(
                    paper_db=cfg.db_path,
                    metar_db=cfg.metar_db_path,
                    initial_balance=cfg.initial_balance,
                    fee_rate=cfg.fee_rate,
                )
                self.instances[name] = (cfg, trader)
            else:
                print(f"WARNING: Unknown instance '{name}', skipping")
    
    def run_daily(self, run_date: str = None, stations: List[str] = None):
        """
        Run daily paper trading for all configured instances.
        
        Args:
            run_date: YYYY-MM-DD date string (defaults to today UTC)
            stations: List of ICAO codes (defaults to all available)
        """
        if run_date is None:
            run_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        
        if stations is None:
            stations = [
                'KATL', 'KBOS', 'KLAX', 'KJFK', 'KORD', 'KMIA',
                'KSEA', 'KSFO', 'KHOU', 'KPHX', 'KDEN', 'KAUS',
                'KPHL', 'KMDW', 'KNYC', 'KDFW', 'KMSP',
                'KDTW', 'KCLT',
            ]
            stations = sorted(set(stations))
        
        print(f"\n{'='*70}")
        print(f"MULTI-INSTANCE PAPER TRADING RUN — {run_date}")
        print(f"Instances: {', '.join(self.instances.keys())}")
        print(f"Stations: {len(stations)}")
        print(f"{'='*70}\n")
        
        all_results = {}
        
        for instance_name, (cfg, trader) in self.instances.items():
            # Scheduler guard — prevent concurrent runs of same instance
            lock = InstanceLock(cfg.lock_file)
            try:
                if not lock.acquire():
                    print(f"  ⚠️ {instance_name}: already running (lock held), skipping")
                    write_health_status(instance_name, "locked", {"reason": "concurrent_run_skipped"})
                    continue
            except Exception as e:
                print(f"  ⚠️ {instance_name}: lock error: {e}, skipping")
                continue
            
            write_health_status(instance_name, "running", {"run_date": run_date})
            logger = setup_instance_logger(instance_name)
            logger.info(f"Starting daily run for {run_date}")
            
            print(f"\n{'─'*50}")
            print(f"Instance: {instance_name}")
            print(f"DB: {cfg.db_path}")
            print(f"Balance: ${cfg.initial_balance:,.2f}")
            print(f"Discord: {'enabled' if cfg.discord_enabled else 'disabled'}")
            print(f"Log: {cfg.log_path}")
            print(f"{'─'*50}")
            
            try:
                results = self._run_instance(
                    instance_name=instance_name,
                    cfg=cfg,
                    trader=trader,
                    run_date=run_date,
                    stations=stations,
                    logger=logger,
                )
                all_results[instance_name] = results
                
                # Print summary
                executed = sum(1 for r in results if r.get('status') == 'executed')
                skipped = sum(1 for r in results if r.get('status') == 'skipped')
                alerts_sent = sum(1 for r in results if r.get('alert_sent'))
                total_pnl = sum(r.get('realized_pnl', 0) for r in results)
                
                print(f"\n  Summary: {executed} executed, {skipped} skipped, {alerts_sent} alerts sent")
                print(f"  Running P&L: ${total_pnl:+,.2f}")
                
                write_health_status(instance_name, "healthy", {
                    "run_date": run_date,
                    "executed": executed,
                    "skipped": skipped,
                    "alerts_sent": alerts_sent,
                    "running_pnl": total_pnl,
                })
                logger.info(f"Run complete: {executed} executed, {skipped} skipped, {alerts_sent} alerts")
            except Exception as e:
                logger.error(f"Run failed: {e}", exc_info=True)
                write_health_status(instance_name, "error", {"error": str(e)})
                print(f"  ✗ {instance_name}: run failed — {e}")
                all_results[instance_name] = []
            finally:
                lock.release()
        
        return all_results
    
    def _run_instance(
        self,
        instance_name: str,
        cfg: InstanceConfig,
        trader: PaperTrader,
        run_date: str,
        stations: List[str],
        logger: logging.Logger = None,
    ) -> List[Dict[str, Any]]:
        """Run paper trading for a single instance."""
        results = []
        sizing_config = get_config_for_instance(cfg.sizing_instance)
        metar_conn = sqlite3.connect(cfg.metar_db_path, timeout=10)
        
        for station in stations:
            station = station.strip().upper()
            
            # Generate signals for this station
            signals = self._generate_signals_for_station(
                station=station,
                run_date=run_date,
                trader=trader,
                metar_conn=metar_conn,
            )
            
            for signal in signals:
                signal_type = signal['signal_type']
                direction = signal['direction']
                confidence = signal['confidence']
                market_type = signal.get('market_type', 'HIGH')
                
                # Compute position size using confidence-weighted sizing
                current_balance = trader.get_current_balance(run_date)
                position_size, conf_tier, sizing_meta = compute_position_size(
                    signal_type=signal_type,
                    confidence=confidence,
                    current_balance=current_balance,
                    config=sizing_config,
                )
                
                # Non-zero position size → fire alert
                if position_size <= 0:
                    results.append({
                        'station': station,
                        'signal_type': signal_type,
                        'status': 'skipped',
                        'reason': 'zero_position_size',
                        'alert_sent': False,
                    })
                    continue
                
                # Get market price (live Kalshi API with historical fallback)
                market_price = trader._get_market_price(station, run_date, market_type)
                
                # Build Kalshi market URL for the alert
                market_url = _build_market_url(station, market_type, run_date)
                
                # Get settlement bucket info
                current_bucket = self._get_current_bucket(station, run_date, metar_conn)
                trading_bucket = self._get_trading_bucket(station, run_date, metar_conn)
                
                # Compute running P&L
                running_pnl = self._get_running_pnl(trader)
                
                # Get top signals for this station
                top_signals = self._get_top_signals(station, run_date, metar_conn)
                
                # Get Sharpe and coverage
                sharpe = self._compute_sharpe(trader)
                coverage = f"{len(stations)}/{len(stations)}"
                
                # Build Discord alert with hard filtering
                alert = self._build_discord_alert(
                    station=station,
                    market=market_type,
                    direction=direction,
                    size=position_size,
                    current_bucket=current_bucket,
                    trading_bucket=trading_bucket,
                    market_odds=market_price,
                    trade_conf=conf_tier.value,
                    trade_conf_value=confidence,
                    top_signals=top_signals,
                    sharpe=sharpe,
                    coverage=coverage,
                    running_pnl=running_pnl,
                    instance_tag=cfg.instance_tag,
                    market_url=market_url,
                )
                
                # Check for filtered alerts (hard filter applied in alert_builder)
                if alert.get('filtered') or alert.get('skip_reason'):
                    results.append({
                        'station': station,
                        'signal_type': signal_type,
                        'status': 'skipped',
                        'reason': 'hard_filter',
                        'skip_reason': alert.get('skip_reason'),
                        'alert_sent': False,
                    })
                    print(f"  ↓ {station} {signal_type} {direction} "
                          f"skipped (filter: {alert.get('skip_reason')})")
                    continue
                
                # Send Discord alert if enabled
                alert_sent = False
                if cfg.discord_enabled and cfg.discord_webhook_url:
                    alert_sent = self._send_discord_alert(cfg.discord_webhook_url, alert)
                
                # Log alert to JSONL regardless of Discord delivery
                log_alert(instance_name, alert)
                
                # Place the paper trade
                result = trader.place_paper_trade(
                    station=station,
                    market_type=market_type,
                    signal_direction=MarketSide.UP if direction == "UP" else MarketSide.DOWN,
                    trade_version=f"v3.0_{instance_name.lower()}",
                    functionality=signal_type,
                    date=run_date,
                    notes=f"Instance: {instance_name}, Conf: {confidence:.2f}, Tier: {conf_tier.value}",
                )
                
                result['alert_sent'] = alert_sent
                result['sizing_metadata'] = sizing_meta
                result['discord_alert'] = alert if alert_sent else None
                results.append(result)
                
                # Log to console
                status = "✓" if result.get('status') == 'executed' else "↓"
                print(f"  {status} {station} {signal_type} {direction} "
                      f"size=${position_size:.2f} conf={confidence:.2f} "
                      f"tier={conf_tier.value} "
                      f"{'[ALERT SENT]' if alert_sent else ''}")
        
        metar_conn.close()
        
        # Daily reconciliation
        trader.daily_reconciliation(run_date)
        
        return results
    
    def _generate_signals_for_station(
        self,
        station: str,
        run_date: str,
        trader: PaperTrader,
        metar_conn: sqlite3.Connection,
    ) -> List[Dict[str, Any]]:
        """Generate all signals for a single station."""
        signals = []
        
        # Signal 1: Late-day momentum hourly
        ldm_dir, ldm_conf, ldm_prob = _ldm_hourly_signal(station, run_date, metar_conn)
        if ldm_dir is not None:
            signals.append({
                'signal_type': 'late_day_momentum_hourly',
                'direction': 'UP' if ldm_dir == 'up' else 'DOWN',
                'confidence': max(ldm_conf, 0.5),  # Floor at 0.5
                'market_type': 'HIGH',
                'prob': ldm_prob,
            })
        
        # Signal 2: Reversion signal
        prior_movement = trader._get_prior_day_reversion(station, run_date)
        if prior_movement is not None and abs(prior_movement) > 2:
            if prior_movement > 0:
                signals.append({
                    'signal_type': 'reversion_after_settlement',
                    'direction': 'DOWN',
                    'confidence': 0.65,
                    'market_type': 'HIGH',
                })
            else:
                signals.append({
                    'signal_type': 'reversion_after_settlement',
                    'direction': 'UP',
                    'confidence': 0.65,
                    'market_type': 'HIGH',
                })
        
        # Signal 3: Calendar climatology
        clim_direction = trader._get_calendar_climatology_direction(station, run_date)
        if clim_direction is not None and abs(clim_direction) > 1.5:
            signals.append({
                'signal_type': 'calendar_climatology',
                'direction': 'UP' if clim_direction > 0 else 'DOWN',
                'confidence': 0.55,
                'market_type': 'HIGH',
            })
        
        return signals
    
    def _get_current_bucket(self, station: str, date: str, conn: sqlite3.Connection) -> int:
        """Get current temperature bucket for station.
        
        Falls back to most recent available bucket if no data for exact date.
        """
        c = conn.cursor()
        # First try exact date match
        c.execute("""
            SELECT settlement_bucket FROM settlement_epochs
            WHERE station = ? AND local_trading_date = ? AND epoch_status = 'closed'
            LIMIT 1
        """, (station, date))
        row = c.fetchone()
        if row:
            return row[0]
        
        # Fall back to most recent available data
        c.execute("""
            SELECT settlement_bucket FROM settlement_epochs
            WHERE station = ? AND epoch_status = 'closed'
            ORDER BY local_trading_date DESC
            LIMIT 1
        """, (station,))
        row = c.fetchone()
        return row[0] if row else 0
    
    def _get_trading_bucket(self, station: str, date: str, conn: sqlite3.Connection) -> int:
        """Get the trading bucket (next settlement bucket).
        
        Falls back to most recent available trading bucket if no data for exact date.
        """
        c = conn.cursor()
        # First try exact date match
        c.execute("""
            SELECT prior_settlement_bucket FROM settlement_epochs
            WHERE station = ? AND local_trading_date = ? AND epoch_status = 'closed'
            LIMIT 1
        """, (station, date))
        row = c.fetchone()
        if row:
            return row[0]
        
        # Fall back to most recent available data
        c.execute("""
            SELECT prior_settlement_bucket FROM settlement_epochs
            WHERE station = ? AND epoch_status = 'closed'
            ORDER BY local_trading_date DESC
            LIMIT 1
        """, (station,))
        row = c.fetchone()
        return row[0] if row else 0
    
    def _get_running_pnl(self, trader: PaperTrader) -> float:
        """Get running P&L for a trader."""
        try:
            conn = sqlite3.connect(trader.paper_db)
            c = conn.cursor()
            c.execute("SELECT COALESCE(SUM(realized_pnl), 0) FROM trades WHERE status = 'closed'")
            result = c.fetchone()
            conn.close()
            return float(result[0]) if result else 0.0
        except Exception:
            return 0.0
    
    def _get_top_signals(self, station: str, date: str, conn: sqlite3.Connection) -> List[str]:
        """Get top signal descriptions for a station."""
        signals = []
        
        # Late-day momentum
        ldm_dir, ldm_conf, _ = _ldm_hourly_signal(station, date, conn)
        if ldm_dir is not None:
            signals.append(f"late_day_momentum_hourly (conf={ldm_conf:.2f})")
        
        # Add generic signals
        if len(signals) < 3:
            signals.append("reversion_after_settlement (conf=0.65)")
        if len(signals) < 3:
            signals.append("calendar_climatology (conf=0.55)")
        
        return signals[:3]
    
    def _compute_sharpe(self, trader: PaperTrader) -> float:
        """Compute running Sharpe ratio."""
        try:
            conn = sqlite3.connect(trader.paper_db)
            c = conn.cursor()
            c.execute("""
                SELECT realized_pnl FROM trades
                WHERE status = 'closed' AND realized_pnl IS NOT NULL
                ORDER BY trade_date_utc
            """)
            pnls = [row[0] for row in c.fetchall()]
            conn.close()
            
            if len(pnls) < 2:
                return 0.0
            
            mean_pnl = sum(pnls) / len(pnls)
            std_pnl = (sum((x - mean_pnl) ** 2 for x in pnls) / (len(pnls) - 1)) ** 0.5
            
            if std_pnl == 0:
                return 0.0
            
            # Annualized Sharpe (assuming daily trades, 252 trading days)
            return (mean_pnl / std_pnl) * (252 ** 0.5)
        except Exception:
            return 0.0
    
    def _build_discord_alert(
        self,
        station: str,
        market: str,
        direction: str,
        size: float,
        current_bucket: int,
        trading_bucket: int,
        market_odds: float,
        trade_conf: str,
        trade_conf_value: float,
        top_signals: List[str],
        sharpe: float,
        coverage: str,
        running_pnl: float,
        instance_tag: str = "[DEV]",
        market_url: str = None,
    ) -> Dict[str, Any]:
        """
        Build the Discord alert payload using the slim alert format from alert_builder.py.
        
        Returns a dict with:
        - content: Slim Discord message with Grade (S/A/B/C/D/F), Edge, and Lane tag
        - username: Weather Engine tag
        - embeds: Empty array
        - full_alert_data: Full alert payload for debugging
        """
        # Build the trade_result dict for alert_builder
        trade_result = {
            'confidence': trade_conf_value,
            'market_price': market_odds,
            'position_size_usd': size,
            'sharpe': sharpe,
            'functionality': top_signals[0] if top_signals else 'unknown',
            'trade_uuid': f'{station}_{market}_{direction}',
            'trade_version': 'v3.0',
        }
        
        # Build the slim alert using alert_builder (B-MODE v2)
        alert_data = build_paper_trade_alert(
            trade_result=trade_result,
            station=station,
            market_type=market,
            direction=direction,
            current_bucket=current_bucket,
            trading_bucket=trading_bucket,
            instance=instance_tag.strip('[]'),
            hit_rate=None,
            hit_rate_n=0,
        )
        
        return {
            'content': alert_data.get('content'),
            'username': f"Weather Engine {instance_tag}",
            'embeds': alert_data.get('embeds', []),
            'full_alert_data': alert_data,
        }
    
    def _send_discord_alert(self, webhook_url: str, alert: Dict[str, Any]) -> bool:
        """Send alert to Discord webhook (content and/or embeds)."""
        try:
            payload = {
                "username": alert.get('username', 'Weather Engine'),
            }
            
            if alert.get('content'):
                payload["content"] = alert['content']
            
            if alert.get('embeds'):
                payload["embeds"] = alert['embeds']
            
            json_payload = json.dumps(payload).encode('utf-8')
            req = urllib.request.Request(
                webhook_url,
                data=json_payload,
                headers={
                    'Content-Type': 'application/json',
                    'User-Agent': 'WeatherEngine/1.0 (Paper Trading Bot)',
                },
                method='POST',
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status in (200, 204)
        except Exception as e:
            print(f"  ⚠️ Discord alert failed: {e}")
            return False


# ─── CLI ────────────────────────────────────────────────────────────────

def main():
    """Run multi-instance paper trading."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Multi-instance paper trading runner")
    parser.add_argument("--instances", nargs="+", default=["DEV"],
                       help="Instances to run (PROD, DEV, SBOX)")
    parser.add_argument("--date", default=None,
                       help="Run date (YYYY-MM-DD, default: today)")
    parser.add_argument("--stations", nargs="+", default=None,
                       help="Station list (default: all)")
    parser.add_argument("--write-completion-artifact", action="store_true",
                       help="Write a completion artifact to .meta/continuity/")
    
    args = parser.parse_args()
    
    runner = MultiInstancePaperTrader(instances=args.instances)
    results = runner.run_daily(run_date=args.date, stations=args.stations)
    
    # Print final summary
    print(f"\n{'='*70}")
    print("FINAL SUMMARY")
    print(f"{'='*70}")
    
    for instance_name, instance_results in results.items():
        executed = sum(1 for r in instance_results if r.get('status') == 'executed')
        skipped = sum(1 for r in instance_results if r.get('status') == 'skipped')
        alerts = sum(1 for r in instance_results if r.get('alert_sent'))
        
        print(f"\n{instance_name}:")
        print(f"  Executed: {executed}")
        print(f"  Skipped: {skipped}")
        print(f"  Alerts sent: {alerts}")
    
    # Write completion artifact if requested (for cron runs)
    if args.write_completion_artifact:
        artifact_path = REPO_ROOT.parent.parent / ".meta" / "continuity" / "weather-engine"
        artifact_path.mkdir(parents=True, exist_ok=True)
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M")
        artifact_file = artifact_path / f"dev-run-completion-{ts}.md"
        
        with open(artifact_file, 'w') as f:
            f.write(f"# DEV Paper Trading Run Completion\n\n")
            f.write(f"**Timestamp:** {datetime.now(timezone.utc).isoformat()}\n")
            f.write(f"**Instances:** {', '.join(args.instances)}\n")
            f.write(f"**Date:** {args.date or 'today'}\n\n")
            for instance_name, instance_results in results.items():
                executed = sum(1 for r in instance_results if r.get('status') == 'executed')
                skipped = sum(1 for r in instance_results if r.get('status') == 'skipped')
                alerts = sum(1 for r in instance_results if r.get('alert_sent'))
                f.write(f"## {instance_name}\n")
                f.write(f"- Executed: {executed}\n")
                f.write(f"- Skipped: {skipped}\n")
                f.write(f"- Alerts sent: {alerts}\n\n")
        print(f"\nCompletion artifact written: {artifact_file}")


if __name__ == "__main__":
    main()
