#!/usr/bin/env python3
"""ITEM 4 (2026-10-16) — the LOW arming gate's `allow_book` bypass is closed.

Cold-eyes BLOCK: `scripts/low_entry_bridge.py` claimed a non-dry-run booking
"refuses unless `LOW_BRIDGE_ENABLED=1`", but the guard was
``if not dry_run and not (LOW_BRIDGE_ENABLED or allow_book)`` — so
``allow_book=True`` alone armed the lane and a stray caller (or harness) could
book LOW trades with the env gate OFF.

Fix (DECISION): the environment flag is the SOLE AUTHORITY. `allow_book` is kept
for signature compatibility but is no longer an arming authority; booking requires
`LOW_BRIDGE_ENABLED=1`. The gate raises BEFORE any I/O, so `LOW_BRIDGE_ENABLED`
unset + `allow_book=True` refuses to book.

Run: python3 -m pytest tests/test_item4_low_arming_gate_2026_10_16.py -v
"""
import importlib.util
import inspect
import os
import sys

import pytest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts"))


def _load_low():
    spec = importlib.util.spec_from_file_location(
        "low_entry_bridge", os.path.join(_REPO, "scripts", "low_entry_bridge.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.unit
def test_allow_book_true_with_flag_unset_refuses_to_book(monkeypatch):
    """The headline Item 4 contract: the override cannot arm the lane."""
    lb = _load_low()
    monkeypatch.setattr(lb, "LOW_BRIDGE_ENABLED", False)
    with pytest.raises(RuntimeError):
        lb.run(dry_run=False, allow_book=True)


@pytest.mark.unit
def test_flag_unset_refuses_to_book(monkeypatch):
    lb = _load_low()
    monkeypatch.setattr(lb, "LOW_BRIDGE_ENABLED", False)
    with pytest.raises(RuntimeError):
        lb.run(dry_run=False)


@pytest.mark.unit
def test_gate_does_not_reference_allow_book():
    """Source contract: the arming condition is the env flag ALONE — no `or
    allow_book` escape hatch remains."""
    lb = _load_low()
    src = inspect.getsource(lb.run)
    assert "LOW_BRIDGE_ENABLED" in src
    # The old bypass forms must be gone.
    assert "LOW_BRIDGE_ENABLED or allow_book" not in src
    assert "(LOW_BRIDGE_ENABLED or allow_book)" not in src


@pytest.mark.unit
def test_default_flag_is_off():
    """`LOW_BRIDGE_ENABLED` defaults OFF (prepare-only) when the env is unset."""
    os.environ.pop("LOW_BRIDGE_ENABLED", None)
    lb = _load_low()
    assert lb.LOW_BRIDGE_ENABLED is False


@pytest.mark.unit
def test_cli_never_passes_allow_book():
    """The CLI cannot arm the lane: `main()` must not pass `allow_book`."""
    lb = _load_low()
    src = inspect.getsource(lb.main)
    assert "allow_book" not in src
