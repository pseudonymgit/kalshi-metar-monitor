"""Regression test for METAR temp/dewpoint parser corruption.

Root cause: `parse_metar` treated ANY token containing a '/' as a temperature/
dewpoint group. A `PK WND 36034/2032` peak-wind token (wind direction/speed +
time) was split on '/' and `36034` parsed as 36034.0 °C -> ~64893 °F, which
overwrote the correct temp_f and corrupted daily_stats.max_temp_f/avg_temp_f.
"""
import importlib.util
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOD_PATH = os.path.join(REPO_ROOT, "scripts", "metar_collect_live.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("metar_collect_live", MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


m = _load_module()


@pytest.mark.parametrize(
    "raw, expected_temp_f, expected_dewpoint_f",
    [
        # The exact corrupt rows recovered from production (were ~64893.2).
        (
            "KMSY 222053Z 02014G34KT 8SM FEW012 BKN020 OVC030 29/26 A2967 "
            "RMK AO2 PK WND 36034/2032 RAB24E38 SLP052 P0000 60000 T0289",
            84.2,
            78.8,
        ),
        (
            "KDCA 282236Z 35024G34KT 1 1/2SM R01/5000VP6000FT +TSRA BR "
            "SCT026 BKN043CB BKN250 21/19 A2960 RMK AO2 PK WND 36034/2234 W",
            69.8,
            66.2,
        ),
        (
            "KDEN 150853Z 35022G30KT 10SM SCT100 15/04 A3015 RMK AO2 "
            "PK WND 36034/0829 SLP129 T01500039 53035",
            59.0,
            39.2,
        ),
        # M-prefixed (negative Celsius) dewpoint must still parse.
        (
            "KDEN 150953Z 34018G26KT 10SM BKN060 BKN100 14/M01 A3020 RMK AO2 "
            "PK WND 36028/0900 PRESRR SLP146 T01391006",
            57.2,
            30.2,
        ),
    ],
)
def test_peak_wind_token_not_misparsed(raw, expected_temp_f, expected_dewpoint_f):
    parsed = m.parse_metar(raw)
    assert parsed is not None
    assert parsed["temp_f"] == pytest.approx(expected_temp_f, abs=0.01)
    assert parsed["dewpoint_f"] == pytest.approx(expected_dewpoint_f, abs=0.01)


def test_garbage_temp_rejected_by_guard():
    # A raw string whose only temp-like token is a wind token must yield no temp.
    # (Peak-wind token is now excluded by _is_temp_dewpoint_token; even if it
    # weren't, the +/-150°F sanity guard rejects the ~64893°F result.)
    raw = "KPHX 121451Z 33011KT 8SM PK WND 36027/1432 SLP109"
    parsed = m.parse_metar(raw)
    assert parsed["temp_f"] is None


def test_is_temp_dewpoint_token():
    assert m._is_temp_dewpoint_token("29/26")
    assert m._is_temp_dewpoint_token("14/M01")
    assert m._is_temp_dewpoint_token("M03/M05")
    # False positives that previously broke parsing:
    assert not m._is_temp_dewpoint_token("36034/2032")  # PK WND wind peak
    assert not m._is_temp_dewpoint_token("R01/5000VP6000FT")  # runway visual range
    assert not m._is_temp_dewpoint_token("1/2SM")  # fractional visibility
    assert not m._is_temp_dewpoint_token("BKN020")  # no slash
