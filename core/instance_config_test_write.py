#!/usr/bin/env python3
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
    
    Raises:
        ValueError: If required webhooks are not configured
    """
    env_vars = {
        "PROD": "DISCORD_WEBHOOK_PROD",
        "DEV":  "DISCORD_WEBHOOK_DEV", 
        "SBOX": "DISCORD_WEBHOOK_SBOX"
    }
    
    missing_webhooks = []
    
    for instance, env_var in env_vars.items():
        webhook_url = os.getenv(env_var)
        if not webhook_url:
            missing_webhooks.append(f"{env_var} (required for {instance})")
    
    if missing_webhooks:
        raise ValueError(
            f"Missing required Discord webhook environment variables: {', '.join(missing_webhooks)}. "
            f"Please configure these before starting the application.\n"
            f"Example: export {missing_webhooks[0].split(' ')[0]}='<webhook_url>'"
        )

    # Additionally validate that the webhook configurations don't use default placeholder values
    for instance, default_webhook in _DEFAULT_WEBHOOKS.items():
        env_var = env_vars[instance]
        env_webhook = os.getenv(env_var)
        if env_webhook == default_webhook and default_webhook == "":
            # This means a new default was set but environment hasn't overridden it
            missing_webhooks.append(f"{env_var} (required for {instance} and set to placeholder)")
    
    if missing_webhooks:
        raise ValueError(
            f"Discord webhook environment variables set to placeholder values: {', '.join(missing_webhooks)}. "
            f"Please set them to actual webhook URLs."
        )


# Initialize INSTANCE_CONFIGS after validation
INSTANCE_CONFIGS = {
    "PROD": InstanceConfig(
        name="PROD",
        db_path=str(REPO_ROOT / "data" / "paper_trading_prod.db"),
        metar_db_path=str(REPO_ROOT / "data" / "metar_backfill.db"),
        initial_balance=10000.0,
        fee_rate=0.0,
        discord_webhook_url=os.getenv("DISCORD_WEBHOOK_PROD", _DEFAULT_WEBHOOKS["PROD"]),
        discord_enabled=os.getenv("DISCORD_ENABLED_PROD", "true").lower() in ("1", "true", "yes"),
        sizing_instance="PROD",
    ),
    "DEV": InstanceConfig(
        name="DEV",
        db_path=str(REPO_ROOT / "data" / "paper_trading_dev.db"),
        metar_db_path=str(REPO_ROOT / "data" / "metar_backfill.db"),
        initial_balance=5000.0,
        fee_rate=0.0,
        discord_webhook_url=os.getenv("DISCORD_WEBHOOK_DEV", _DEFAULT_WEBHOOKS["DEV"]),
        discord_enabled=os.getenv("DISCORD_ENABLED_DEV", "true").lower() in ("1", "true", "yes"),
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


# Rest of the file would continue from the original...