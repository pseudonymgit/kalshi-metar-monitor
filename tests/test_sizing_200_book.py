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

from sbox_intraday_ev_bridge import (  # noqa: E402
    _position_size_usd, kelly_fraction, kelly_position_size,
)
from core.position_sizer import (  # noqa: E402
    MAX_BANKROLL_PCT, PositionSizer, get_config_for_instance,
)


@pytest.mark.unit
def test_bridge_sizing_is_kelly():
    # AF (2026-10-14): Kelly replaces the retired confidence ladder.
    # p=0.55 @ 0.50 -> f*=0.10 -> $20 -> capped at $10
    assert kelly_position_size(0.55, 0.50) == 10.0
    # p=0.65 @ 0.50 -> f*=0.30 -> $60 -> capped at $10
    assert kelly_position_size(0.65, 0.50) == 10.0
    # p=0.55 @ 0.30 -> f*=0.833 -> capped at $10
    assert kelly_position_size(0.55, 0.30) == 10.0
    # no edge -> no trade
    assert kelly_position_size(0.50, 0.50) == 0.0
    # negative edge at a rich price -> no trade (kills the 19:1 asymmetry)
    assert kelly_position_size(0.55, 0.94) == 0.0
    # _position_size_usd delegates to Kelly
    assert _position_size_usd(0.65, 0.50) == kelly_position_size(0.65, 0.50)


@pytest.mark.unit
def test_bridge_kelly_capped_at_5pct_of_bankroll():
    # f* > 0.05 is capped -> $10 max on the $200 book.
    for p, m in [(0.60, 0.50), (0.70, 0.50), (0.99, 0.50), (0.90, 0.10)]:
        assert kelly_position_size(p, m) <= 10.0
    # A small edge gives a small (sub-cap) size, so cheap entries no longer
    # balloon into absurd contract counts.
    sz = kelly_position_size(0.52, 0.50)   # f*=0.04 -> $8
    assert 0.0 < sz < 10.0
    assert kelly_fraction(0.52, 0.50) == pytest.approx(0.04)


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
