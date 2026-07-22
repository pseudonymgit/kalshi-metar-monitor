#!/usr/bin/env python3

# CHANGELOG (last 10 broad changes):
# 1. [2026-07-21 Phase 3-7: Agreement gate, signal enhancements, alert infra, production readiness, Kalshi API integration]
# 2. [2026-07-19 Phase 2: Add 850-mb temperature advection signal + wire into engine]
#

"""
Heartbeat System v1.0 — Phase 6.2

System health reporting with delivery routing integration.
Reports on scheduler status, signal counts, last trade timestamps,
account balance, and kill switch status via configured delivery channels.
"""

import asyncio
import json
import logging
import aiohttp
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, Union
import time

# Import delivery components
try:
    from .delivery_router import create_default_delivery_router, HeartbeatDeliverer
except ImportError:
    from delivery_router import create_default_delivery_router, HeartbeatDeliverer

# Also import the paper trading engine components
try:
    from .paper_trading_engine import PaperTradingEngine, get_latest_account_balance
    from .alert_schema import parse_alert_from_db_row
except ImportError:
    from paper_trading_engine import PaperTradingEngine, get_latest_account_balance
    from alert_schema import parse_alert_from_db_row

try:
    from .metar_monitor import MetarMonitor
except ImportError:
    # MetarMonitor may not be available in this module, we'll handle it gracefully
    MetarMonitor = None

# Try to import settings
try:
    from .instance_config import INSTANCE_CONFIG
except ImportError:
    # Fallback to simple config dict
    INSTANCE_CONFIG = {
        'instance_id': 'weather-engine-local',
        'version': '6.2.heartbear-0.1',
    }

_LOGGER = logging.getLogger(__name__)

# Global singleton for heartbeat manager
_HEARTBEAT_MANAGER = None


class HeartbeatManager:
    """Manages periodic heartbeat generation and status collection"""
    
    def __init__(self, db_path: str = "./data/weather_alerts.db"):
        self.db_path = db_path
        self.delivery_router = None
        self.heartbeat_deliverer = None
        self.is_running = False
        self._last_heartbeat_time = None
        self.alert_db_path = db_path
        self.instance_id = INSTANCE_CONFIG.get('instance_id', 'weather-engine-standalone')
        self.version = INSTANCE_CONFIG.get('version', 'unversioned')
        
        # Tracking various metrics
        self.kill_switch_activated = False
        self.paper_trading_engine = None
        
    async def initialize(self):
        """Initialize heartbeat system with delivery router"""
        # Create delivery components
        self.delivery_router = await create_default_delivery_router()
        self.heartbeat_deliverer = HeartbeatDeliverer(self.delivery_router)
        _LOGGER.info("Heartbeat system initialized with delivery router integration")
    
    def get_scheduler_status(self) -> Optional[Dict[str, Any]]:
        """
        Get scheduler status from running MetarMonitor instance or from database logs.
        This depends on which modules are available in the codebase.
        """
        if MetarMonitor is not None:
            try:
                # Look for active MetarMonitor in globals or as a daemon
                import gc
                for obj in gc.get_objects():
                    if isinstance(obj, MetarMonitor) and hasattr(obj, '__dict__'):
                        status = {
                            'scheduler_running': getattr(obj, 'scheduler_running', False),
                            'poll_count': getattr(obj, 'poll_count', 0),
                            'last_poll_utc': getattr(obj, 'last_poll_utc', 'N/A'),
                            'last_loop_utc': getattr(obj, 'last_loop_utc', 'N/A'),
                            'timeout_count': getattr(obj, 'timeout_count', 0),
                            'last_timeout_station': getattr(obj, 'last_timeout_station', 'N/A'),
                            'last_timeout_utc': getattr(obj, 'last_timeout_utc', 'N/A'),
                        }
                        return status
            except Exception as e:
                _LOGGER.debug(f"Could not get scheduler status from active MetarMonitor: {e}")
        
        # Extract scheduler info from database if available
        try:
            with get_sqlite_connection(self.db_path) as conn:
                latest_poll_log = conn.execute("""
                    SELECT created_utc, metadata_json 
                    FROM alerts 
                    WHERE alert_type LIKE '%heartbeat%' OR alert_type LIKE '%status%'
                    ORDER BY created_utc DESC LIMIT 1
                """).fetchone()
                
                if latest_poll_log:
                    # Attempt to parse any stored scheduler data
                    metadata_json = latest_poll_log[1]
                    if metadata_json:
                        try:
                            metadata = json.loads(metadata_json)
                            if 'scheduler' in metadata:
                                return metadata['scheduler']
                        except Exception as e:
                            pass
                        
                # Try to estimate scheduler activity from overall alert patterns
                recent_alerts = conn.execute("""
                    SELECT COUNT(*) FROM alerts 
                    WHERE created_utc > datetime('now', '-30 minutes')
                """).fetchone()[0]
                
                return {
                    'recent_activity_count': recent_alerts,
                    'estimation_method': 'database_activity_recent',
                }
        except Exception as e:
            _LOGGER.debug(f"Could not determine scheduler status from database: {e}")
        
        # If nothing else available, return a minimal status
        return {
            'status_estimation': 'not_monitored',
            'reason': 'MetarMonitor not found in context',
            'instance_type': self.instance_id,
        }
    
    def get_signal_counts(self) -> Dict[str, int]:
        """Count recent signals and alerts in the system"""
        try:
            with get_sqlite_connection(self.db_path) as conn:
                # Total alerts count
                total_alerts = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
                
                # Recent alerts (last 24 hours)
                recent_alerts = conn.execute("""
                    SELECT COUNT(*) FROM alerts 
                    WHERE created_utc > datetime('now', '-24 hours')
                """).fetchone()[0]
                
                # Recent alerts (last 6 hours)
                recent_short_alerts = conn.execute("""
                    SELECT COUNT(*) FROM alerts 
                    WHERE created_utc > datetime('now', '-6 hours')
                """).fetchone()[0]
                
                # Alerts by type breakdown
                type_counts = [0, 0, 0, 0, 0, 0]  # Initialize counters  
                
                alert_types = conn.execute("""
                    SELECT alert_type, COUNT(*) 
                    FROM alerts 
                    GROUP BY alert_type
                """).fetchall()
                
                type_breakdown = {}
                for type_name, count in alert_types:
                    type_breakdown[type_name or 'UNKNOWN'] = count
                
                return {
                    'total_signals_all_time': total_alerts,
                    'recent_signals_24h': recent_alerts,
                    'recent_signals_6h': recent_short_alerts,
                    'by_type': type_breakdown,
                }
        except Exception as e:
            _LOGGER.error(f"Error getting signal counts: {e}")
            return {
                'error': str(e),
                'total_signals_all_time': 0,
                'recent_signals_24h': 0,
                'recent_signals_6h': 0,
                'by_type': {}
            }
    
    def get_last_trade_timestamp(self) -> Optional[str]:
        """Get timestamp of the last trade from the paper trading engine log if available"""
        if not self.paper_trading_engine:
            try:
                # Look for available paper trading engine instance
                import gc
                for obj in gc.get_objects():
                    if (
                        obj.__class__.__module__.startswith('paper_trading_engine') or 
                        obj.__class__.__name__ == 'PaperTradingEngine'
                    ):
                        if hasattr(obj, '_get_latest_trade_timestamp'):
                            return obj._get_latest_trade_timestamp()
                        elif hasattr(obj, 'log_manager') and hasattr(obj.log_manager, 'get_last_record'):
                            # Check trade log if we find a paper trading engine
                            try:
                                last_trade = obj.log_manager.get_last_record('trades.jsonl')
                                if last_trade and 'timestamp' in last_trade:
                                    return last_trade['timestamp']
                            except Exception as e:
                                pass
            except Exception as e:
                _LOGGER.debug(f"Could not find active paper trading engine for trade timestamp: {e}")
        
        # If no live engine available, extract from trade logs
        try:
            trade_logs_path = self.db_path.replace('.db', '') + '_trades.jsonl'
            import os
            if os.path.exists(trade_logs_path):
                latest_line = None
                with open(trade_logs_path, 'rb') as f:
                    f.seek(-2, 2)  # Go to second last character
                    while f.read(1) != b'\n':
                        f.seek(-2, 1)
                        if f.tell() == 0:
                            f.seek(0)
                            break
                    latest_line = f.readline().decode()
                
                if latest_line:
                    record = json.loads(latest_line.strip())
                    return record.get('timestamp', 'Unknown')
        except Exception as e:
            _LOGGER.debug(f"Could not get last trade timestamp from trade log: {e}")
        
        # Fall back to database trade tracking
        try:
            with get_sqlite_connection(self.db_path) as conn:
                latest_trade = conn.execute("""
                    SELECT created_utc FROM alerts 
                    WHERE alert_type LIKE '%trade%' OR metadata_json LIKE '%"trade_id"%'
                    ORDER BY created_utc DESC LIMIT 1
                """).fetchone()
                if latest_trade:
                    return latest_trade[0]
        except Exception as e:
            pass
        
        return 'N/A'
    
    def get_account_balance(self) -> Union[float, str]:
        """Get latest available account balance from engine or log data"""
        try:
            # Try directly from paper trading engine if present
            if self.paper_trading_engine:
                if hasattr(self.paper_trading_engine, 'get_current_balance'):
                    return self.paper_trading_engine.get_current_balance()
            else:
                # Try to import and use function from paper_trading_engine
                return get_latest_account_balance(self.db_path)
        except Exception as e:
            _LOGGER.debug(f"Could not get account balance: {e}")
            try:
                # Look in database for balance information
                with get_sqlite_connection(self.db_path) as conn:
                    row = conn.execute("""
                        SELECT metadata_json 
                        FROM alerts 
                        WHERE metadata_json LIKE '%balance%' 
                        ORDER BY created_utc DESC LIMIT 1
                    """).fetchone()
                    
                    if row and row[0]:
                        md = json.loads(row[0])
                        # Look for balance in common keys
                        for key in ['balance', 'account_balance', 'current_balance']:
                            if key in md:
                                return float(md[key])
            except Exception as e:
                pass
        
        return 'Untracked'
    
    def get_kill_switch_status(self) -> bool:
        """Check if the kill switch is activated"""
        # If kill_switch_status file exists, consider it active
        import os
        kill_switch_path = self.db_path.replace('.db', '_kill_switch.lock')
        self.kill_switch_activated = os.path.exists(kill_switch_path)
        
        # Also check from live engine status
        try:
            import gc
            for obj in gc.get_objects():
                if hasattr(obj, 'kill_switch_enabled'):
                    self.kill_switch_activated = obj.kill_switch_enabled
                    break
        except Exception as e:
            pass
        
        return self.kill_switch_activated
    
    async def collect_system_metrics(self) -> Dict[str, Any]:
        """
        Collect various system metrics for heartbeat.
        
        Combines scheduler status, signal data, trading data, and system health.
        """
        # Get scheduler status
        scheduler_status = self.get_scheduler_status()
        
        # Get signal data
        signal_data = self.get_signal_counts()
        
        # Get trade data
        last_trade_ts = self.get_last_trade_timestamp()
        
        # Get account balance
        account_balance = self.get_account_balance()
        
        # Get kill switch status
        kill_switch_status = self.get_kill_switch_status()
        
        # System uptime estimation (since process start)
        if self._last_heartbeat_time is not None:
            uptime_seconds = (datetime.now(timezone.utc) - self._last_heartbeat_time).total_seconds()
        else:
            # Initial heartbeat, use time since some fixed marker or approximate
            uptime_seconds = 0
        
        # Compile full heartbeat package
        heartbeat_data = {
            'instance_id': self.instance_id,
            'version': self.version,
            'status': 'healthy' if not kill_switch_status else 'kill-switch activated',
            'uptime_seconds': int(uptime_seconds),
            'timestamp_utc': datetime.now(timezone.utc).isoformat(),
            
            # Scheduler metrics
            'scheduler': scheduler_status,
            
            # Signal metrics
            'signals': signal_data,
            
            # Trading metrics
            'last_trade_timestamp': last_trade_ts,
            'account_balance_currency': f"${account_balance}" if isinstance(account_balance, (int, float)) else str(account_balance),
            'kill_switch_activated': kill_switch_status,
            
            # System metrics
            'cpu_usage_approximation': 'not_available',
            'memory_usage_approximation': 'not_available',
            'disk_space_warning_level': 'not_measured'
        }
        
        return heartbeat_data
    
    async def send_heartbeat(self, custom_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Generate heartbeat data and deliver via delivery router.
        """
        self._last_heartbeat_time = datetime.now(timezone.utc)
        
        # Collect system metrics
        system_metrics = await self.collect_system_metrics()
        
        # Add any custom data provided
        if custom_data:
            system_metrics.update(custom_data)
        
        # Use heartbeat deliverer to send across all configured channels
        if self.heartbeat_deliverer:
            result = await self.heartbeat_deliverer.send_heartbeat(
                system_metrics,
                heartbeat_id=f"{int(time.time())}-{self.instance_id.split('-')[0]}"
            )
            
            _LOGGER.info(f"Heartbeat status: success={result.get('success', 'unknown')}")
            return result
        else:
            _LOGGER.warning("Heartbeat deliverer not initialized")
            return {
                'success': False,
                'error': 'Heartbeat deliverer not initialized',
                'system_data': system_metrics
            }
    
    async def start_periodic_heartbeat(self, interval_seconds: int = 3600):  # 1 hour default
        """Start periodic heartbeat generation"""
        self.is_running = True
        _LOGGER.info(f"Starting periodic heartbeat every {interval_seconds} seconds")
        
        while self.is_running:
            try:
                await self.send_heartbeat()
                await asyncio.sleep(interval_seconds)
            except asyncio.CancelledError:
                _LOGGER.info("Heartbeat cancelled")
                self.is_running = False
                break
            except Exception as e:
                _LOGGER.error(f"Error in heartbeat loop: {e}")
                logger.error(f"Stack trace: {traceback.format_exc()}")
                await asyncio.sleep(min(300, interval_seconds))  # Retry after 5 minutes if error
    
    def stop(self):
        """Stop the heartbeat system"""
        self.is_running = False
        _LOGGER.info("Heartbeat system stopped")


async def get_heartbeat_manager(alert_db_path: str = "./data/weather_alerts.db") -> HeartbeatManager:
    """Get singleton instance of heartbeat manager, creating if needed"""
    global _HEARTBEAT_MANAGER
    
    if _HEARTBEAT_MANAGER is None:
        _HEARTBEAT_MANAGER = HeartbeatManager(db_path=alert_db_path)
        await _HEARTBEAT_MANAGER.initialize()
    
    return _HEARTBEAT_MANAGER


# Convenience functions for external use
async def send_single_heartbeat() -> Dict[str, Any]:
    """Send a single heartbeat for ad-hoc use"""
    manager = await get_heartbeat_manager()
    return await manager.send_heartbeat()


def is_kill_switch_activated() -> bool:
    """Check kill switch status directly"""
    manager = None
    try:
        import gc
        for obj in gc.get_objects():
            if hasattr(obj, 'get_kill_switch_status'):
                if callable(getattr(obj, 'get_kill_switch_status')):
                    return obj.get_kill_switch_status()
    except Exception as e:
        pass
    
    # Default: kill switch not active
    return False


# Compatibility functions that may be referenced elsewhere
async def send_heartbeat_if_configured() -> Optional[Dict[str, Any]]:
    """Backwards compatibility: conditional heartbeat sending"""
    return await send_single_heartbeat()


async def run_heartbeat_cycle() -> Dict[str, Any]:
    """Execute one full heartbeat cycle (for use in main application loops)"""
    return await send_single_heartbeat()


# Main execution for testing
async def main():
    """Test heartbeat functionality directly"""
    hb_manager = await get_heartbeat_manager()
    print("Sending test heartbeat...")
    result = await hb_manager.send_heartbeat()
    print(json.dumps(result, indent=2, default=str))

if __name__ == "__main__":
    import traceback
from .sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection
    asyncio.run(main())