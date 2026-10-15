#!/usr/bin/env python3
"""F6 (2026-10-14) — P&L basis naming consistency (contract-notional dollars).

Dan's ruling: `P&L = contract-notional dollars` — `(payout - price) * quantity`.
The register claimed "constant-1" in places while the shipped SBOX book is
qty-scaled (43/43 rows match qty*(S-price); 0 match constant-1 — PWW-BUG-HUNT
F7). This pins the register + code comments to the qty-scaled ruling so a
dashboard repointed off the wrong label cannot report a number off by ~x qty.

Run: python3 -m pytest tests/test_f6_pnl_basis_naming.py -v
"""
import os
import re

import pytest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_WORKSPACE = os.path.abspath(os.path.join(_REPO, "..", ".."))
_REGISTER = os.path.join(_WORKSPACE, "docs", "plans",
                         "WE-COMPLETE-REGISTER-2026-10-13.md")


def _read(path):
    with open(path) as fh:
        return fh.read()


@pytest.mark.skipif(not os.path.exists(_REGISTER),
                    reason="workspace register not present in this checkout")
def test_register_names_the_contract_notional_basis():
    txt = _read(_REGISTER)
    assert "contract-notional" in txt.lower()


@pytest.mark.skipif(not os.path.exists(_REGISTER),
                    reason="workspace register not present in this checkout")
def test_register_does_not_assert_constant_1_as_the_forward_basis():
    """Any remaining 'constant-1' must sit inside a retirement/supersede note,
    not stand as a forward basis claim (the label wraps across lines)."""
    txt = _read(_REGISTER)
    markers = ("retire", "supersede", "stale", "superseded", "corrected",
               "earlier", "label")
    for m in re.finditer("constant-1", txt):
        window = txt[max(0, m.start() - 400):m.end() + 400].lower()
        assert any(k in window for k in markers), \
            f"unqualified constant-1 basis claim near: {txt[m.start()-60:m.end()+60]!r}"


def test_engine_ddl_comment_names_contract_notional():
    txt = _read(os.path.join(_REPO, "core", "paper_trading_engine.py"))
    assert "CONTRACT-NOTIONAL" in txt.upper()
    assert "quantity" in txt  # the DDL comment ties the basis to quantity


def test_pnl_tracking_comment_names_contract_notional_and_retires_constant_1():
    txt = _read(os.path.join(_REPO, "core", "pnl_tracking.py"))
    assert "CONTRACT-NOTIONAL" in txt.upper()
    assert "RETIRED" in txt.upper()
