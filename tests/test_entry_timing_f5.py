#!/usr/bin/env python3
"""F5 (2026-10-13) — entry timing must be real per-station T-minus, not 6.

`scripts/sbox_intraday_ev_bridge.py` passed a hardcoded `hour_before_settlement=6`
to the entry engine, so every station got the same timing regardless of its
timezone. The bridge now derives hours-to-settlement from
`station_time.settlement_time_utc`, which varies by station longitude.

Run: python3 -m pytest tests/test_entry_timing_f5.py -v
"""
import importlib.util
import os
import sys
from datetime import datetime, timezone

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts"))


def _load_bridge():
    spec = importlib.util.spec_from_file_location(
        "sbox_intraday_ev_bridge", os.path.join(_REPO, "scripts", "sbox_intraday_ev_bridge.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_hours_to_settlement_is_timezone_dependent():
    bridge = _load_bridge()
    now = datetime(2026, 10, 13, 3, 0, tzinfo=timezone.utc)
    east = bridge._hours_before_settlement("KNYC", "2026-10-13", now)   # UTC-4
    west = bridge._hours_before_settlement("KLAX", "2026-10-13", now)   # UTC-7
    assert east is not None and west is not None
    # The west coast settles ~3h later in UTC than the east coast.
    assert west - east > 2.0
    assert east != 6.0 or west != 6.0


def test_hours_to_settlement_during_trading_hours_is_plausible():
    bridge = _load_bridge()
    # 2026-10-13 16:00Z is midday ET: KNYC settlement 2026-10-14T04:00Z -> 12h.
    now = datetime(2026, 10, 13, 16, 0, tzinfo=timezone.utc)
    hb = bridge._hours_before_settlement("KNYC", "2026-10-13", now)
    assert hb is not None
    assert 11.0 < hb < 13.0


def test_hours_to_settlement_unknown_station_defaults_to_east():
    bridge = _load_bridge()
    # An unknown ICAO falls back to America/New_York in station_time, so it still
    # resolves a settlement instant (the bridge's 6.0 fallback only fires when a
    # settlement instant cannot be resolved at all).
    now = datetime(2026, 10, 13, 16, 0, tzinfo=timezone.utc)
    hb = bridge._hours_before_settlement("KZZZ", "2026-10-13", now)
    east = bridge._hours_before_settlement("KNYC", "2026-10-13", now)
    assert hb is not None and hb == east
