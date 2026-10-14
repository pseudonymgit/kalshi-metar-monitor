#!/usr/bin/env python3
"""Agreement-based additive bump (2026-10-14, Dan) — explicit n-of-k concurrence.

The log-odds pool blends the weaker signals implicitly; this tests the EXPLICIT
concurrence term (gated behind USE_AGREEMENT_BOOST, default OFF).

  * >= 3 members agree -> +max_boost * min(count-2,3)/3 (3->0.01, 4->0.02, 5->0.03)
  * <= 1 member agrees -> -max_boost * min(2-count,3)/3 (1->-0.01, 0->-0.02)
  * else 0

Run: python3 -m pytest tests/test_agreement_boost.py -q
"""
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
os.environ.pop("USE_AGREEMENT_BOOST", None)

from core.intraday.multi_forecaster_fusion import (  # noqa: E402
    fuse, agreement_bump, agreement_boost_enabled, AGREEMENT_BOOST_MAX,
)


# ── bump arithmetic ──────────────────────────────────────────────────────────
def test_bump_scales_with_concurrence():
    # five members well above 0.5, core high -> 5 concur -> full boost
    b, c, d = agreement_bump([0.9, 0.85, 0.8, 0.7, 0.6], 0.8)
    assert (b, c, d) == (0.03, 5, "high")
    b, c, _ = agreement_bump([0.9, 0.85, 0.8, 0.7], 0.8)
    assert (b, c) == (0.02, 4)
    b, c, _ = agreement_bump([0.9, 0.85, 0.8], 0.8)
    assert (b, c) == (0.01, 3)


def test_bump_penalizes_lone_agree():
    b, c, d = agreement_bump([0.9, 0.2, 0.3, 0.1, 0.4], 0.8)
    assert c == 1 and d == "high"
    assert b == -0.01
    b, c, _ = agreement_bump([0.1, 0.2, 0.3, 0.4, 0.9], 0.2)
    assert c == 4, "low-direction concurrence counted on the <0.5 side"
    assert b == 0.02


def test_bump_zero_midconcurrence():
    # 2 members both on the core side -> count 2, between the <=1 and >=3 bands.
    b, c, _ = agreement_bump([0.9, 0.8], 0.8)
    assert b == 0.0 and c == 2
    # a lone concurring member in a small set is a penalty per the <=1 band.
    b, c, _ = agreement_bump([0.9, 0.1], 0.8)
    assert b == -0.01 and c == 1


def test_bump_skips_none_members():
    b, c, _ = agreement_bump([0.9, 0.9, None, float("nan")], 0.8)
    assert c == 2 and b == 0.0, "only present members vote"


def test_bump_empty():
    b, c, _ = agreement_bump([None, None], 0.8)
    assert (b, c) == (0.0, 0)


# ── gate: OFF by default ─────────────────────────────────────────────────────
def test_boost_off_by_default_byte_identical():
    assert agreement_boost_enabled() is False
    pf_off, meta = fuse(0.9, 0.85, 1.0, p_market=0.8, p_om_gfs=0.9)
    assert meta["agreement_boost"] == {"enabled": False}
    # explicit False == default path
    pf_off2, _ = fuse(0.9, 0.85, 1.0, p_market=0.8, p_om_gfs=0.9,
                      use_agreement_boost=False)
    assert abs(pf_off - pf_off2) < 1e-12


def test_boost_on_raises_concurrence():
    # 5 members all high -> bump 0.03 applied to p_core (before ceiling clamp).
    pf_on, meta = fuse(0.9, 0.85, 1.0, p_market=0.8, p_om_gfs=0.9,
                       use_agreement_boost=True, ceiling=0.99)
    assert meta["agreement_boost"]["enabled"] is True
    assert meta["agreement_boost"]["count"] == 5
    assert meta["agreement_boost"]["bump"] == 0.03
    assert meta["p_core"] >= 0.5


def test_boost_direction_low():
    pf_on, meta = fuse(0.1, 0.15, 0.001, p_market=0.2, p_om_gfs=0.1,
                       use_agreement_boost=True, ceiling=0.99)
    assert meta["agreement_boost"]["direction"] == "low"
    assert meta["agreement_boost"]["count"] == 5
