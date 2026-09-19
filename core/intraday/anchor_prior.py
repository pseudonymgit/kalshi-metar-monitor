"""
anchor_prior.py — h-24 daily-LOOP anchor consumer (P2-B wire-in, 2026-09-19)

Wires the daily-LOOP h-24 anchor into the intraday hourly loop as the
documented Bayesian prior seam (core/intraday/recalibration_loop.py).

Chain (producer → consumer):
    scripts/daily_loop_producer.py  → data/daily_loop_output.json
    scripts/intraday_bridge.py      → data/intraday/anchor_h24.json   (h-24 anchor)
    core/intraday/anchor_prior.py   → seeds the intraday belief state  (THIS module)

Contract (verified against producer/bridge schemas):
    anchor_h24.json carries:
      - "per_station": { station: { "probability": P(UP), "confidence": [0,1],
                                     "direction": "UP"|"DOWN"|"NEUTRAL", ... } }
      - "direction" / "confidence" / "mu" / "sigma" (aggregate)
      - "daily_date_utc" / "bridge_ts_utc" (freshness)

The anchor probability is P(UP) directly: UP stations carry 0.70–0.95, DOWN
stations 0.05–0.30 (see scripts/p2b_h24_anchor_ab.py::load_anchor).

Behavior:
  - load_anchor_prior() returns {station: (prob_up, confidence_weight)} or {}.
  - Missing / stale / unreadable anchor → returns {} (fail-graceful). The hourly
    loop is non-blocking: a missing prior degrades to the flat-prior path, it
    does not halt the runner (the bridge itself already fail-closes upstream).

Deterministic. No AI/ML. No trading logic.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
ANCHOR_H24_FILE = REPO_ROOT / "data" / "intraday" / "anchor_h24.json"

# Anchor is a daily artifact; treat it as stale beyond this age (hours).
MAX_ANCHOR_AGE_HOURS = 36

# Anchor prior weight floor/ceiling. The anchor confidence (0.5–0.98) maps to a
# belief weight; we scale it so a high-confidence daily prior has meaningful
# pull but cannot drown out the intraday signal. Weight = confidence * scale.
ANCHOR_WEIGHT_SCALE = 3.0
ANCHOR_WEIGHT_MIN = 0.0
ANCHOR_WEIGHT_MAX = 6.0


def _age_hours(ts_str: Optional[str]) -> Optional[float]:
    """Return anchor age in hours, or None if unparseable."""
    if not ts_str:
        return None
    try:
        if "T" in ts_str:
            generated = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        else:
            generated = datetime.fromisoformat(ts_str + "T23:59:00+00:00")
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - generated).total_seconds() / 3600.0


def load_anchor_prior(
    anchor_path: Optional[Path] = None,
    max_age_hours: float = MAX_ANCHOR_AGE_HOURS,
) -> Dict[str, Tuple[float, float]]:
    """
    Load the h-24 anchor prior as {station: (prob_up, confidence_weight)}.

    Returns an empty dict (no prior) if the anchor is missing, stale, or
    unreadable — the intraday loop degrades to its flat-prior path.
    """
    path = Path(anchor_path) if anchor_path else ANCHOR_H24_FILE
    if not path.exists():
        logger.warning(f"h-24 anchor not found: {path} — no prior (flat fallback)")
        return {}

    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"h-24 anchor unreadable ({path}): {e} — no prior")
        return {}

    # Freshness check (fail-graceful, non-blocking).
    age = _age_hours(
        data.get("bridge_ts_utc")
        or data.get("daily_generated_at_utc")
        or data.get("timestamp_utc")
    )
    if age is not None and age > max_age_hours:
        logger.warning(
            f"h-24 anchor stale ({age:.0f}h > {max_age_hours:.0f}h) — no prior"
        )
        return {}

    per_station = data.get("per_station") or {}
    prior: Dict[str, Tuple[float, float]] = {}
    for station, v in per_station.items():
        if not isinstance(v, dict):
            continue
        prob = v.get("probability")
        if prob is None:
            # Fall back: temperature_distribution.mean is a temperature, not a
            # probability — do NOT use it as a prior. Skip stations with no
            # explicit probability.
            continue
        conf = v.get("confidence", 0.5)
        if not isinstance(conf, (int, float)):
            conf = 0.5
        weight = max(ANCHOR_WEIGHT_MIN, min(ANCHOR_WEIGHT_MAX, float(conf) * ANCHOR_WEIGHT_SCALE))
        prior[station.upper()] = (float(prob), round(weight, 4))

    if prior:
        logger.info(
            f"h-24 anchor prior loaded: {len(prior)} stations "
            f"(date={data.get('daily_date_utc')}, source={data.get('source')})"
        )
    else:
        logger.warning("h-24 anchor present but no per-station probabilities — no prior")

    return prior


def combine_with_anchor(
    station: str,
    intraday_prob: float,
    anchor_prior: Dict[str, Tuple[float, float]],
) -> Tuple[float, float]:
    """
    Combine an intraday calibrated probability with the h-24 anchor prior using
    the documented Bayesian update (recalibration_loop.update_belief formula):

        updated = (prior*prior_w + intraday*1.0) / (prior_w + 1.0)

    Returns (combined_probability, combined_weight). When no anchor prior exists
    for the station, returns (intraday_prob, 1.0) unchanged.
    """
    prior = anchor_prior.get(station.upper())
    if prior is None:
        return intraday_prob, 1.0
    prior_prob, prior_weight = prior
    if prior_weight <= 0.0:
        return intraday_prob, 1.0
    combined = (prior_prob * prior_weight + intraday_prob * 1.0) / (prior_weight + 1.0)
    return round(max(0.01, min(0.99, combined)), 6), round(prior_weight + 1.0, 4)
