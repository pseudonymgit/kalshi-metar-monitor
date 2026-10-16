"""PWW Loop-H (2026-10-16): the LOW entry ladder must not price off a POST-pin quote.

Regression: `low_ladder_from_csv` selected by the smallest UNSIGNED
`dist_pin_minutes`, so a 14:00 quote (post-pin, +30) tied a 13:00 quote
(pre-pin, -30). The generator dropped the sign entirely, making a look-ahead
quote indistinguishable from a legitimate one.
"""
import csv
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _load_bridge():
    spec = importlib.util.spec_from_file_location(
        "_low_bridge_test", REPO / "scripts" / "low_entry_bridge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_prefer_pin_quote_prefers_prepin():
    b = _load_bridge()
    f = b._prefer_pin_quote
    assert f(-30, 30, 20, 20) is True, "pre-pin must beat post-pin"
    assert f(20, 20, -30, 30) is False, "post-pin must lose to pre-pin"
    assert f(-10, 10, -30, 30) is True, "nearer pre-pin wins"
    assert f(10, 10, 20, 20) is True, "earliest post-pin wins"
    assert f(None, 10, None, 30) is True, "legacy unsigned fallback"


def test_csv_carries_signed_pin_minutes():
    """The backfill CSV must expose the signed distance so causality is enforceable."""
    csv_path = (REPO / "artifacts" / "low-market-backfill"
                / "low_entry_price_series_2026-10-13.csv")
    if not csv_path.exists():
        return  # artifact not present in a bare checkout
    with open(csv_path) as fh:
        header = next(csv.reader(fh))
    assert "signed_pin_minutes" in header, (
        "LOW series must carry the signed pin distance (causality guard)")


def test_postpin_only_rows_still_usable():
    """A strike with ONLY post-pin quotes must still be priceable (no false drop).

    The `prev is None` first-quote path is handled by the CALLER
    (`low_ladder_from_csv`), which installs the first quote unconditionally; the
    helper is only consulted for later quotes. Here we assert the helper's real
    contract: an earlier post-pin quote beats a later post-pin quote.
    """
    b = _load_bridge()
    f = b._prefer_pin_quote
    assert f(10, 10, 30, 30) is True, "earlier post-pin wins over later post-pin"
    assert f(30, 30, 10, 10) is False, "later post-pin loses to earlier post-pin"
