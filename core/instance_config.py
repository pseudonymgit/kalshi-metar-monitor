#!/usr/bin/env python3

# CHANGELOG (last 10 broad changes):
# 1. [2026-07-18 Fix Bug 7: Remove hardcoded fee rates - replace 0.05/0.001/0.002 with proper zero commissions (Kalshi charges 0 commission)]
# 2. [2026-07-16 T2: Remove 4 dead signals from all code paths]
# 3. [2026-07-12 B-MODE: Initial commit for full ensemble backtest suite scripts]
# 4. [2026-07-05 R4-1.1: Fix P&L mark-to-market + thread-safe price cache]
#

"""
Instance Configuration for Three-Lane Parallelism (v1.1 — 2026-07-05)

Defines per-instance configuration for PROD/DEV/SBOX parallelism:
  - Separate DB paths, webhooks, sizing configs
  - Scheduler guard (prevents concurrent runs of same instance)
  - Health endpoint paths
  - Log paths
  - Lock files
  - Instance tags in alert format ([PROD], [DEV], [SBOX])

Webhook URLs are now required via environment variables (no defaults).

All script/config work — no AI in the loop.
"""

import os
import json
import fcntl
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]

# ─── Default Webhook URLs (empty by default) ──────────────
# Production/DEV/SBOX webhooks must be set via environment variables.
_DEFAULT_WEBHOOKS = {
    "PROD": "",  # Required: set via DISCORD_WEBHOOK_PROD environment variable
    "DEV":  "",  # Required: set via DISCORD_WEBHOOK_DEV environment variable
    "SBOX": "",  # Required: set via DISCORD_WEBHOOK_SBOX environment variable
}


@dataclass
class InstanceConfig:
    """Complete configuration for a paper trading instance."""
    name: str
    db_path: str
    metar_db_path: str
    initial_balance: float
    fee_rate: float
    discord_webhook_url: str
    discord_enabled: bool
    sizing_instance: str  # PROD, DEV, SBOX for position sizing
    
    # Three-lane additions
    log_path: str = ""
    lock_file: str = ""
    health_file: str = ""
    alert_log_path: str = ""
    
    def __post_init__(self):
        if not self.log_path:
            self.log_path = str(REPO_ROOT / "logs" / f"paper_trading_{self.name.lower()}.log")
        if not self.lock_file:
            self.lock_file = str(REPO_ROOT / "data" / f".{self.name.lower()}.lock")
        if not self.health_file:
            self.health_file = str(REPO_ROOT / "data" / f"{self.name.lower()}_health.json")
        if not self.alert_log_path:
            self.alert_log_path = str(REPO_ROOT / "logs" / f"alerts_{self.name.lower()}.jsonl")
    
    @property
    def instance_tag(self) -> str:
        """Tag used in alert messages, e.g. [DEV], [PROD], [SBOX]."""
        return f"[{self.name}]"


# ─── Validation and Configuration ─────────────────────────────────────────

def validate_webhook_configuration():
    """Validate that webhook URLs are set via environment variables.
    
    Only enforce webhook requirements for instances where discord_enabled=True.
    DEV and SBOX instances with Discord disabled are allowed to run without webhooks.
    
    Raises:
        ValueError: If any instance with discord_enabled=True has a missing webhook
    """
    # Check environment variables - only enforce for enabled instances
    env_vars = {
        "PROD": ("DISCORD_WEBHOOK_PROD", os.getenv("DISCORD_ENABLED_PROD", "false").lower() in ("1", "true", "yes")),
        "DEV":  ("DISCORD_WEBHOOK_DEV", os.getenv("DISCORD_ENABLED_DEV", "false").lower() in ("1", "true", "yes")),
        "SBOX": ("DISCORD_WEBHOOK_SBOX", os.getenv("DISCORD_ENABLED_SBOX", "false").lower() in ("1", "true", "yes")),
    }
    
    missing_webhooks = []
    
    for instance, (env_var, discord_enabled) in env_vars.items():
        # Only enforce webhook requirement if Discord is enabled for this instance
        if discord_enabled:
            webhook_url = os.getenv(env_var)
            if not webhook_url:
                missing_webhooks.append(f"{env_var} (required for {instance})")
    
    if missing_webhooks:
        raise ValueError(
            f"Missing required Discord webhook environment variables: {', '.join(missing_webhooks)}. "
            f"Please configure these before starting the application.\n"
            f"Example: export {missing_webhooks[0].split(' ')[0]}='<webhook_url>'"
        )


# Initialize INSTANCE_CONFIGS after validation
def _initialize_with_validation():
    """Initialize INSTANCE_CONFIGS after validating webhooks."""
    # Validate configuration before creating instances
    validate_webhook_configuration()
    
    return {
        "PROD": InstanceConfig(
            name="PROD",
            db_path=str(REPO_ROOT / "data" / "paper_trading_prod.db"),
            metar_db_path=str(REPO_ROOT / "data" / "metar_backfill.db"),
            initial_balance=10000.0,
            fee_rate=0.0,
            discord_webhook_url=os.getenv("DISCORD_WEBHOOK_PROD", _DEFAULT_WEBHOOKS["PROD"]),
            discord_enabled=os.getenv("DISCORD_ENABLED_PROD", "false").lower() in ("1", "true", "yes"),
            sizing_instance="PROD",
        ),
        "DEV": InstanceConfig(
            name="DEV",
            db_path=str(REPO_ROOT / "data" / "paper_trading_dev.db"),
            metar_db_path=str(REPO_ROOT / "data" / "metar_backfill.db"),
            initial_balance=5000.0,
            fee_rate=0.0,
            discord_webhook_url=os.getenv("DISCORD_WEBHOOK_DEV", _DEFAULT_WEBHOOKS["DEV"]),
            discord_enabled=os.getenv("DISCORD_ENABLED_DEV", "false").lower() in ("1", "true", "yes"),
            sizing_instance="DEV",
        ),
        "SBOX": InstanceConfig(
            name="SBOX",
            db_path=str(REPO_ROOT / "data" / "paper_trading_sbox.db"),
            metar_db_path=str(REPO_ROOT / "data" / "metar_backfill.db"),
            initial_balance=1000.0,
            fee_rate=0.0,
            discord_webhook_url=os.getenv("DISCORD_WEBHOOK_SBOX", _DEFAULT_WEBHOOKS["SBOX"]),
            discord_enabled=os.getenv("DISCORD_ENABLED_SBOX", "true").lower() in ("1", "true", "yes"),
            sizing_instance="SBOX",
        ),
    }


# Initialize the actual config 
INSTANCE_CONFIGS = _initialize_with_validation()


# ─── Scheduler Guard ────────────────────────────────────────────────────

class InstanceLock:
    """
    File-based lock to prevent concurrent runs of the same instance.
    Uses fcntl.flock for process-level mutual exclusion.
    """
    
    def __init__(self, lock_file: str):
        self.lock_file = lock_file
        self._fd = None
    
    def acquire(self) -> bool:
        """Try to acquire the lock. Returns True if acquired, False if already held."""
        os.makedirs(os.path.dirname(self.lock_file), exist_ok=True)
        self._fd = open(self.lock_file, 'w')
        try:
            fcntl.flock(self._fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fd.write(str(os.getpid()))
            self._fd.flush()
            return True
        except (IOError, OSError):
            # Lock is held by another process
            self._fd.close()
            self._fd = None
            return False
    
    def release(self):
        """Release the lock."""
        if self._fd is not None:
            try:
                fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
            except (IOError, OSError):
                pass
            self._fd.close()
            self._fd = None
            try:
                os.unlink(self.lock_file)
            except OSError:
                pass
    
    def __enter__(self):
        if not self.acquire():
            raise RuntimeError(f"Instance lock already held: {self.lock_file}")
        return self
    
    def __exit__(self, *args):
        self.release()


# ─── Health Check ───────────────────────────────────────────────────────

def write_health_status(instance_name: str, status: str, details: dict = None):
    """
    Write health status to a JSON file for monitoring.
    
    Args:
        instance_name: PROD, DEV, or SBOX
        status: "healthy", "running", "error", "idle"
        details: Optional dict with additional info
    """
    cfg = INSTANCE_CONFIGS.get(instance_name.upper())
    if cfg is None:
        return
    
    from datetime import datetime, timezone
    health = {
        "instance": instance_name.upper(),
        "status": status,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "details": details or {},
    }
    
    os.makedirs(os.path.dirname(cfg.health_file), exist_ok=True)
    with open(cfg.health_file, 'w') as f:
        json.dump(health, f, indent=2)


def read_health_status(instance_name: str) -> Optional[dict]:
    """Read health status for an instance."""
    cfg = INSTANCE_CONFIGS.get(instance_name.upper())
    if cfg is None:
        return None
    
    try:
        with open(cfg.health_file, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def get_all_health() -> dict:
    """Get health status for all instances."""
    result = {}
    for name in INSTANCE_CONFIGS:
        result[name] = read_health_status(name) or {
            "instance": name,
            "status": "unknown",
        }
    return result


# ─── Alert Logging ──────────────────────────────────────────────────────

def log_alert(instance_name: str, alert_payload: dict):
    """
    Log an alert to the instance's JSONL alert log.
    Each line is a JSON object.
    """
    cfg = INSTANCE_CONFIGS.get(instance_name.upper())
    if cfg is None:
        return
    
    from datetime import datetime, timezone
    entry = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "instance": instance_name.upper(),
        **alert_payload,
    }
    
    os.makedirs(os.path.dirname(cfg.alert_log_path), exist_ok=True)
    with open(cfg.alert_log_path, 'a') as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")


# ─── Logger Setup ───────────────────────────────────────────────────────

def setup_instance_logger(instance_name: str) -> logging.Logger:
    """Set up a logger for a specific instance that writes to its log file."""
    cfg = INSTANCE_CONFIGS.get(instance_name.upper())
    if cfg is None:
        return logging.getLogger("paper_trading")
    
    logger = logging.getLogger(f"paper_trading.{instance_name.lower()}")
    logger.setLevel(logging.INFO)
    
    # Avoid duplicate handlers
    if logger.handlers:
        return logger
    
    # File handler
    os.makedirs(os.path.dirname(cfg.log_path), exist_ok=True)
    file_handler = logging.FileHandler(cfg.log_path)
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s [%(name)s] %(levelname)s: %(message)s'
    ))
    logger.addHandler(file_handler)
    
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.WARNING)
    logger.addHandler(console_handler)
    
    return logger


# ─── CLI ────────────────────────────────────────────────────────────────

def main():
    """Print instance configuration and health status."""
    print("\nWeather Engine — Instance Configuration (v1.1)")
    print("=" * 70)
    
    for name, cfg in INSTANCE_CONFIGS.items():
        health = read_health_status(name) or {}
        print(f"\n{name}:")
        print(f"  DB: {cfg.db_path}")
        print(f"  Balance: ${cfg.initial_balance:,.2f}")
        print(f"  Fee rate: {cfg.fee_rate}")
        print(f"  Discord: {'enabled' if cfg.discord_enabled else 'disabled'}")
        print(f"  Webhook: {'configured' if cfg.discord_webhook_url else 'not set'}")
        print(f"  Instance tag: {cfg.instance_tag}")
        print(f"  Sizing: {cfg.sizing_instance}")
        print(f"  Log: {cfg.log_path}")
        print(f"  Lock: {cfg.lock_file}")
        print(f"  Health: {cfg.health_file}")
        print(f"  Alert log: {cfg.alert_log_path}")
        print(f"  Status: {health.get('status', 'unknown')}")
    
    print("\nAll Health:")
    print(json.dumps(get_all_health(), indent=2))


if __name__ == "__main__":
    main()