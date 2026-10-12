#!/usr/bin/env python3
"""
Dan's $200-bankroll position-sizing rules (2026-10-12).

Locks the ladder that replaces the retired 2026-10-01 one:
  - regular trades        $5-10
  - high-confidence       $25 max (sure things)
  - max position          $25 (never $50 / 25% of a $200 bankroll)
"""
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from sbox_intraday_ev_bridge import _position_size_usd  # noqa: E402
from core.position_sizer import (  # noqa: E402
    MAX_BANKROLL_PCT, PositionSizer, get_config_for_instance,
)


@pytest.mark.unit
def test_bridge_ladder_dan_rules():
    assert _position_size_usd(0.90) == 25.0   # sure-thing max
    assert _position_size_usd(0.80) == 10.0   # high
    assert _position_size_usd(0.60) == 5.0    # medium
    assert _position_size_usd(0.50) == 0.0    # no trade
    assert _position_size_usd(0.99) == 25.0   # never $50


@pytest.mark.unit
def test_bridge_ladder_never_exceeds_25():
    for c in [0.0, 0.3, 0.5, 0.55, 0.7, 0.86, 0.95, 1.0]:
        assert _position_size_usd(c) <= 25.0, f"conf={c} exceeded $25"


@pytest.mark.unit
def test_sizer_cap_is_25_on_200_book():
    sizer = PositionSizer(bankroll=200.0)
    size, _ = sizer.compute_position_size(confidence=0.9, win_rate=0.8, edge=0.5)
    assert size == 25.0, f"Expected $25 cap on $200 book, got ${size}"


@pytest.mark.unit
def test_sizer_cap_fraction():
    assert abs(MAX_BANKROLL_PCT - 0.125) < 1e-12


@pytest.mark.unit
def test_instance_configs_cap_at_25():
    for inst in ("PROD", "DEV", "SBOX"):
        cfg = get_config_for_instance(inst)
        assert cfg.max_size_usd == 25.0, f"{inst} max_size_usd != 25"
        assert cfg.max_size_usd <= 10.0 * 2.5  # never 25% of the book


@pytest.mark.unit
def test_resized_book_caps_at_25():
    """The clean copy has no $50 rows; the source book is left untouched."""
    src = REPO / "data" / "paper_trading_sbox.db"
    dst = REPO / "data" / "paper_trading_sbox_resized.db"
    if not dst.exists():
        pytest.skip("resized book not built")
    c = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
    mx = c.execute("SELECT MAX(position_size_usd) FROM trades").fetchone()[0]
    dist = dict(c.execute(
        "SELECT ROUND(position_size_usd,2), COUNT(*) FROM trades "
        "GROUP BY 1").fetchall())
    c.close()
    assert mx is not None and mx <= 25.0, f"resized book has ${mx} rows"
    assert dist.get(25.0, 0) == 46
    assert sum(dist.values()) == 80
