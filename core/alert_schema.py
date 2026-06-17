"""Shared alert schema constants.

Layer 4: Alert type categorization
"""

ALERT_SCHEMA_VERSION = "2"

# Layer 4: Alert type categorization
ALERT_TYPE_CATEGORIES = {
    "transition": ["instant_up", "instant_down", "settlement_up", "reversion_after_settlement", "goldilocks_reversion"],
    "signal": ["near_boundary_momentum_up", "near_boundary_momentum_down", "goldilocks_momentum_down"],
    "ladder_missing": ["missing_ladder", "missing_directional_ladder"],
}

ALERT_TYPE_TO_CATEGORY = {}
for category, types in ALERT_TYPE_CATEGORIES.items():
    for alert_type in types:
        ALERT_TYPE_TO_CATEGORY[alert_type] = category
