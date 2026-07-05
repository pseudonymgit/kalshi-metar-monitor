"""
Alert Schema v1.0 — Frozen (2026-07-05)

See docs/ALERT-SCHEMA-V1.0.md for full specification.

Layer 4: Alert type categorization and schema version constants.
"""

# ─── Schema Version ──────────────────────────────────────────────────────
# Frozen at v1.0. Legacy integer "2" accepted on ingress for backward compat.
ALERT_SCHEMA_VERSION = "1.0"
LEGACY_SCHEMA_VERSION = "2"

# ─── Alert Type Categories ───────────────────────────────────────────────
# Category: transition — structural temperature transitions
# Category: signal — deterministic signal layer alerts
# Category: ladder_missing — market data gaps

ALERT_TYPE_CATEGORIES = {
    "transition": [
        "instant_up",
        "instant_down",
        "settlement_up",
        "reversion_after_settlement",
        "goldilocks_reversion",
    ],
    "signal": [
        "near_boundary_momentum_up",
        "near_boundary_momentum_down",
        "goldilocks_momentum_down",
    ],
    "ladder_missing": [
        "missing_ladder",
        "missing_directional_ladder",
    ],
}

# Flat lookup: alert_type → category
ALERT_TYPE_TO_CATEGORY = {}
for _category, _types in ALERT_TYPE_CATEGORIES.items():
    for _alert_type in _types:
        ALERT_TYPE_TO_CATEGORY[_alert_type] = _category

# ─── Direction Mapping ──────────────────────────────────────────────────
# Explicit direction for each alert type. Used by _emit_signal_alert and
# audit logging. Replaces fragile endswith() checks.

ALERT_TYPE_DIRECTION = {
    "instant_up": "UP",
    "instant_down": "DOWN",
    "settlement_up": "UP",
    "reversion_after_settlement": "REVERSAL",
    "goldilocks_reversion": "REVERSAL",
    "near_boundary_momentum_up": "UP",
    "near_boundary_momentum_down": "DOWN",
    "goldilocks_momentum_down": "DOWN",
    "missing_ladder": "N/A",
    "missing_directional_ladder": "N/A",
}

# ─── Outcome Classification ─────────────────────────────────────────────
# Structured outcome taxonomy for signal evaluation.

OUTCOME_ALERT_SENT = "ALERT_SENT"
OUTCOME_ELIGIBLE_NOT_ALERTABLE = "ELIGIBLE_NOT_ALERTABLE"
OUTCOME_NO_ELIGIBLE_MARKET = "NO_ELIGIBLE_MARKET"
OUTCOME_HYDRATION_BLOCKED = "HYDRATION_BLOCKED"
OUTCOME_NO_SIGNAL_CONDITION_MATCH = "NO_SIGNAL_CONDITION_MATCH"

ALL_OUTCOMES = (
    OUTCOME_ALERT_SENT,
    OUTCOME_ELIGIBLE_NOT_ALERTABLE,
    OUTCOME_NO_ELIGIBLE_MARKET,
    OUTCOME_HYDRATION_BLOCKED,
    OUTCOME_NO_SIGNAL_CONDITION_MATCH,
)

# ─── Tier 1 Protected Alert Types ───────────────────────────────────────
# Goldilocks/reversion alerts are Tier 1 protected side features.
# They bypass the standard signal suppression path and always emit
# if their conditions are met, regardless of market eligibility.

TIER_1_PROTECTED_TYPES = frozenset({
    "goldilocks_reversion",
    "goldilocks_reversion_alert",
    "reversion_after_settlement",
    "goldilocks_momentum_down",
})

# ─── Channel Routing ────────────────────────────────────────────────────
# Per-city distribution topology. Maps station → Discord channel ID.
# Configured via DISCORD_CHANNEL_MAP env var (JSON).
# If not set, all alerts go to the default webhook URL.

def get_channel_for_station(station: str, channel_map: dict = None) -> str | None:
    """Return Discord channel ID for a station, or None for default."""
    import os
    import json
    if channel_map is None:
        raw = os.getenv("DISCORD_CHANNEL_MAP", "")
        if not raw:
            return None
        try:
            channel_map = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
    return channel_map.get(station.strip().upper())
