"""Loop-3 (2026-10-16): the entry ladder must FAIL CLOSED on an unresolved pin.

Regression: `strike_ladder(..., before_iso=_pin_iso or now_iso)` silently
substituted wall-clock time when the pin was unresolved, reopening the exact
post-pin look-ahead leak the pin bound was meant to close.
"""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "scripts" / "sbox_intraday_ev_bridge.py"


def test_no_or_now_iso_fallback_for_pin():
    """The entry ladder must never fall back to now_iso when the pin is None."""
    text = SRC.read_text()
    assert "before_iso=_pin_iso or now_iso" not in text, (
        "residual look-ahead: the ladder still falls back to now on an unresolved pin"
    )
    assert "before_iso=_pin_iso" in text


def test_pin_unresolved_skips():
    """An unresolved pin must produce an explicit fail-closed skip reason."""
    text = SRC.read_text()
    assert '"pin_unresolved"' in text, "missing explicit pin_unresolved skip"
