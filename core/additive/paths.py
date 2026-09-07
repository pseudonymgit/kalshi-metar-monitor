"""
paths.py — central DB path resolution for the additive stack.

Single source of truth for data file locations. Works from any CWD.
"""

import os
from pathlib import Path

__all__ = [
    "REPO_ROOT", "DATA_DIR",
    "METAR_DB", "ASOS_DB", "ASOS_1MIN_DB",
    "WEATHERAPI_DB", "NWP_DB", "KALSHI_SETTLEMENTS_DB",
    "WARM_SEASON_MONTHS", "MIN_BUCKET_OBS",
    "require", "asos_hourly_db",
]

_REPO_ROOT = Path(__file__).resolve().parents[3]
REPO_ROOT = _REPO_ROOT
DATA_DIR = _REPO_ROOT / "data"

METAR_DB = DATA_DIR / "metar_backfill.db"
ASOS_DB = DATA_DIR / "iem_asos.db"
ASOS_1MIN_DB = DATA_DIR / "iem_asos_1min.db"
WEATHERAPI_DB = DATA_DIR / "weatherapi_archive.db"
NWP_DB = DATA_DIR / "nwp_forecasts.db"
KALSHI_SETTLEMENTS_DB = DATA_DIR / "kalshi_settlements.db"

# Months considered warm season (dry-line corridor active)
WARM_SEASON_MONTHS = {4, 5, 6, 7, 8, 9}

# Minimum observations in a bucket for a reliable aggregate
MIN_BUCKET_OBS = 2


def require(path: Path) -> Path:
    """Assert that a DB file exists; raise FileNotFoundError otherwise."""
    if not path.exists():
        raise FileNotFoundError(f"Required database missing: {path}")
    return path


def asos_hourly_db() -> Path:
    """Prefer the primary hourly ASOS db; fall back to the trial copy."""
    if ASOS_DB.exists():
        return ASOS_DB
    trial = DATA_DIR / "iem_asos_trial.db"
    if trial.exists():
        return trial
    raise FileNotFoundError("No ASOS hourly database found")