#!/usr/bin/env python3
"""
Intraday → Daily Bridge (v1.0 — 2026-09-04)

Pushes daily LOOP output (direction, confidence, μ, σ) into the hourly pipeline
as the h-24 anchor every 7AM ET (11:00 UTC).

Reads from:  data/daily_loop_output.json     (daily pipeline output)
Writes to:   data/intraday/anchor_h24.json   (h-24 anchor for hourly pipeline)

Schedule: Cron at 11:00 UTC daily (7AM ET standard time).

Architecture:
  - The daily pipeline produces LOOP (Learning Online Outlier Prediction) consolidated
    output at end-of-day. That output represents the best directional probability estimate
    based on 24h of data.
  - The hourly pipeline consumes this as a "h-24 prior" — a Bayesian prior that gets
    updated as intraday observations roll in.
  - This bridge is the seam between the daily (epoch) and hourly (real-time) pipelines.

Usage:
    python3 scripts/intraday_bridge.py                    # Run the bridge (default)
    python3 scripts/intraday_bridge.py --dry-run           # Log what would be written
    python3 scripts/intraday_bridge.py --force             # Force push even if stale

No trading logic. No AI/ML.
"""

import argparse
import json
import logging
import logging.handlers
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"
INTRADAY_DIR = DATA_DIR / "intraday"
LOGS_DIR = REPO_ROOT / "logs"

# ─── Paths ────────────────────────────────────────────────────────────────
DAILY_LOOP_OUTPUT = DATA_DIR / "daily_loop_output.json"
ANCHOR_H24_FILE = INTRADAY_DIR / "anchor_h24.json"

# ─── Constants ────────────────────────────────────────────────────────────
MAX_DAILY_AGE_HOURS = 36

ALL_STATIONS = [
    "KATL", "KAUS", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU", "KLAS",
    "KLAX", "KMDW", "KMIA", "KMSP", "KMSY", "KNYC", "KOKC", "KPHL",
    "KPHX", "KSAT", "KSEA", "KSFO",
]

logger = logging.getLogger("intraday_bridge")

# ─── Default anchor (fallback when no daily output) ──────────────────────
DEFAULT_ANCHOR = {
    "direction": "NEUTRAL",
    "confidence": 0.5,
    "mu": 0.0,
    "sigma": 1.0,
    "source": "default_fallback",
    "bridge_ts_utc": None,
}


def _read_daily_loop() -> Optional[Dict[str, Any]]:
    """Read the daily LOOP output JSON. Returns None if missing/invalid."""
    if not DAILY_LOOP_OUTPUT.exists():
        logger.warning(f"Daily loop output not found: {DAILY_LOOP_OUTPUT}")
        return None

    try:
        data = json.loads(DAILY_LOOP_OUTPUT.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Failed to read daily loop output: {e}")
        return None

    if "consensus" not in data:
        logger.warning(f"Daily loop output missing 'consensus' key: {DAILY_LOOP_OUTPUT}")
        return None

    return data


def _check_freshness(data: Dict[str, Any]) -> bool:
    """Return True if the daily output is fresh enough to use."""
    ts_str = (
        data.get("generated_at_utc")
        or data.get("date_utc")
        or data.get("timestamp_utc")
    )
    if not ts_str:
        logger.warning("Daily loop output has no timestamp — assuming stale")
        return False

    try:
        if "T" in ts_str:
            generated = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        else:
            generated = datetime.fromisoformat(ts_str + "T23:59:00+00:00")
    except ValueError:
        logger.warning(f"Cannot parse timestamp: {ts_str!r}")
        return False

    age_hours = (datetime.now(timezone.utc) - generated).total_seconds() / 3600
    if age_hours > MAX_DAILY_AGE_HOURS:
        logger.warning(
            f"Daily loop output is {age_hours:.0f}h old (max {MAX_DAILY_AGE_HOURS}h) — stale"
        )
        return False

    logger.info(f"Daily loop output is {age_hours:.0f}h old — fresh")
    return True


def _aggregate_stations(per_station: Dict[str, Any]) -> tuple:
    """Aggregate per-station directional data into a single anchor tuple.

    Returns (direction, confidence, mu, sigma).

    - direction: majority vote of per-station directions (UP/DOWN/NEUTRAL).
    - confidence: mean of per-station confidences.
    - mu: mean of per-station `probability` (or temperature_distribution.mean).
    - sigma: mean of per-station temperature_distribution.std (or 1.0).
    """
    directions: List[str] = []
    confidences: List[float] = []
    mus: List[float] = []
    sigmas: List[float] = []

    for st in per_station.values():
        if not isinstance(st, dict):
            continue
        d = st.get("direction")
        if d in ("UP", "DOWN", "NEUTRAL"):
            directions.append(d)

        c = st.get("confidence")
        if isinstance(c, (int, float)):
            confidences.append(float(c))

        # mu: prefer explicit probability, else temperature mean
        p = st.get("probability")
        if isinstance(p, (int, float)):
            mus.append(float(p))
        else:
            tdist = st.get("temperature_distribution") or {}
            m = tdist.get("mean")
            if isinstance(m, (int, float)):
                mus.append(float(m))

        tdist = st.get("temperature_distribution") or {}
        s = tdist.get("std")
        if isinstance(s, (int, float)):
            sigmas.append(float(s))

    if not directions:
        return ("NEUTRAL", 0.5, 0.0, 1.0)

    # Majority vote for direction
    up = directions.count("UP")
    down = directions.count("DOWN")
    if up > down:
        direction = "UP"
    elif down > up:
        direction = "DOWN"
    else:
        direction = "NEUTRAL"

    confidence = sum(confidences) / len(confidences) if confidences else 0.5
    mu = sum(mus) / len(mus) if mus else 0.0
    sigma = sum(sigmas) / len(sigmas) if sigmas else 1.0

    return (direction, confidence, mu, sigma)


def _build_anchor(daily_data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Build the h-24 anchor from daily LOOP output (or default fallback)."""
    if daily_data is None:
        anchor = dict(DEFAULT_ANCHOR)
        anchor["bridge_ts_utc"] = datetime.now(timezone.utc).isoformat()
        anchor["source"] = "default_fallback"
        logger.info("Using default anchor (no daily output)")
        return anchor

    consensus = daily_data.get("consensus", {})

    # Per-station directional data lives in `stations_data` (current schema) or
    # `consensus.stations` (legacy schema).
    per_station = daily_data.get("stations_data") or consensus.get("stations", {})

    # Aggregate a single directional anchor from per-station data when the
    # consensus block does not carry direction/confidence/mu/sigma directly.
    direction = consensus.get("direction")
    confidence = consensus.get("confidence")
    mu = consensus.get("mu")
    sigma = consensus.get("sigma")

    if direction is None and per_station:
        direction, confidence, mu, sigma = _aggregate_stations(per_station)

    anchor = {
        "direction": direction if direction is not None else "NEUTRAL",
        "confidence": confidence if confidence is not None else 0.5,
        "mu": mu if mu is not None else 0.0,
        "sigma": sigma if sigma is not None else 1.0,
        "source": "daily_loop",
        "daily_date_utc": daily_data.get("date_utc") or daily_data.get("run_date"),
        "daily_generated_at_utc": (
            daily_data.get("generated_at_utc") or daily_data.get("timestamp_utc")
        ),
        "bridge_ts_utc": datetime.now(timezone.utc).isoformat(),
        "method": daily_data.get("method") or daily_data.get("source"),
        "per_station": per_station,
    }

    logger.info(
        f"Anchor built: direction={anchor['direction']} "
        f"confidence={anchor['confidence']:.3f} "
        f"μ={anchor['mu']:.1f} σ={anchor['sigma']:.1f} "
        f"source={anchor['source']}"
    )
    return anchor


def _write_anchor(anchor: Dict[str, Any], dry_run: bool = False) -> None:
    """Write the anchor to disk (or log only in dry-run)."""
    if dry_run:
        logger.info(f"[DRY RUN] Would write anchor to {ANCHOR_H24_FILE}")
        logger.info(f"[DRY RUN] Anchor content: {json.dumps(anchor, indent=2)}")
        return

    INTRADAY_DIR.mkdir(parents=True, exist_ok=True)
    ANCHOR_H24_FILE.write_text(json.dumps(anchor, indent=2) + "\n")
    logger.info(f"Anchor written to {ANCHOR_H24_FILE} ({len(str(anchor))} bytes)")


def _verify_anchor(anchor: Dict[str, Any]) -> List[str]:
    """Verify anchor integrity. Returns list of warning strings."""
    warnings: List[str] = []
    required = ["direction", "confidence", "mu", "sigma"]
    for key in required:
        if key not in anchor:
            warnings.append(f"Missing required key: {key}")

    if anchor.get("direction") not in ("UP", "DOWN", "NEUTRAL"):
        warnings.append(f"Invalid direction: {anchor.get('direction')!r}")

    if not (0 <= anchor.get("confidence", 0) <= 1):
        warnings.append(f"Confidence out of range: {anchor.get('confidence')!r}")

    return warnings


def run_bridge(dry_run: bool = False, force: bool = False) -> int:
    """Run the bridge end-to-end. Returns exit code."""
    logger.info(
        f"Intraday bridge: dry_run={dry_run} force={force} "
        f"daily_source={DAILY_LOOP_OUTPUT}"
    )

    daily_data = _read_daily_loop()

    if daily_data is not None and not force:
        if not _check_freshness(daily_data):
            logger.warning("Daily data stale — forcing use of default anchor")
            daily_data = None

    anchor = _build_anchor(daily_data)

    warnings = _verify_anchor(anchor)
    for w in warnings:
        logger.warning(f"Anchor verification: {w}")

    _write_anchor(anchor, dry_run=dry_run)

    if warnings:
        logger.warning(f"Bridge complete with {len(warnings)} warning(s)")
        return 0 if anchor["source"] == "daily_loop" else 1

    logger.info("Bridge complete — anchor pushed to hourly pipeline")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Push daily LOOP output as h-24 anchor into hourly pipeline"
    )
    ap.add_argument("--dry-run", action="store_true", help="Log only, no write")
    ap.add_argument(
        "--force", action="store_true",
        help="Force push even if daily data is stale",
    )
    args = ap.parse_args()

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.handlers.RotatingFileHandler(
                LOGS_DIR / "intraday_bridge.log",
                maxBytes=5242880,
                backupCount=3,
            ),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )

    return run_bridge(dry_run=args.dry_run, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
