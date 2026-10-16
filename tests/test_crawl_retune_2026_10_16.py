#!/usr/bin/env python3
"""ITEM3 (2026-10-16) — crawl rules re-tuned for the Kelly/sole paradigm.

The pre-Kelly rules were built for the old fused p (saturated ~0.99, only trade
p>=0.6). Under the sole om_gfs + honest calibration + Kelly paradigm the decision
variable is EDGE (model p minus the market price), so a deep-OTM penny is a GOOD
trade at a LOW raw p (KBOS: p=0.079 @ 0.025). The old rules flagged 10/10 live
rows with rule-vs-paradigm mismatches.

Re-tune (documented in each script):
  R3: absolute p_fused<0.6 -> edge < 0 (negative edge).
  R4: p_fused>=0.9 -> the SOLE raw YES p pinned at the per-station ceiling.
  R5: RETIRED raw %-edge -> honest calibration bound check.
  v2: old absolute-p flags are paradigm-mismatch provenance only; the only
      honest selection defect is a NEGATIVE honest edge.

Run: python3 -m pytest tests/test_crawl_retune_2026_10_16.py -v
"""
import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts"))


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_REPO, "scripts", f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _book(path):
    c = sqlite3.connect(path)
    c.execute(
        """CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_uuid TEXT NOT NULL UNIQUE, trade_date_utc TEXT NOT NULL,
            station TEXT NOT NULL, market_type TEXT, signal_direction TEXT,
            forecast_prob REAL, market_price REAL, trade_type TEXT,
            quantity INTEGER, position_size_usd REAL, trade_price REAL,
            trade_cost REAL, trade_version TEXT, functionality TEXT,
            status TEXT, notes TEXT, implied_prob REAL, analytical_prob REAL,
            calibration_error REAL, confidence_indicator REAL,
            created_at_utc TEXT, p_gefs REAL, p_analog REAL, p_trajectory REAL,
            p_fused REAL, price_source TEXT, settled_value REAL,
            settlement_date_utc TEXT, realized_pnl REAL, settlement_confirmed INT,
            settlement_outcome TEXT, market_ticker TEXT)""")
    c.execute(
        """CREATE TABLE trade_forecaster_probs (
            trade_uuid TEXT UNIQUE, station TEXT, trade_date_utc TEXT,
            strike REAL, side TEXT, ticker TEXT, p_fused REAL,
            calibrated_p REAL, ceiling REAL, composite_conf REAL,
            grade TEXT)""")
    c.commit()
    return c


def _row(c, uuid, st, p_yes, mkt, leg, pf, ceiling, cal_yes=None):
    cal = cal_yes if cal_yes is not None else p_yes
    booked = p_yes if leg == "buy_yes" else 1.0 - p_yes
    c.execute(
        "INSERT INTO trades (trade_uuid, trade_date_utc, station, "
        "market_price, trade_type, forecast_prob, analytical_prob, p_gefs, "
        "p_fused, status, position_size_usd) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (uuid, "2026-10-15", st, mkt, leg, booked, booked, p_yes, pf,
         "open", 10.0))
    c.execute(
        "INSERT INTO trade_forecaster_probs (trade_uuid, station, "
        "trade_date_utc, calibrated_p, ceiling) VALUES (?,?,?,?,?)",
        (uuid, st, "2026-10-15", cal, ceiling))


@pytest.mark.unit
def test_r3_flags_negative_edge_not_low_p(tmp_path, monkeypatch):
    """R3 must flag a NEGATIVE edge, never a low absolute p (deep-OTM penny)."""
    b = _load("bullshit_crawl")
    db = tmp_path / "book.db"
    c = _book(db)
    # KBOS-like: p=0.079 > market 0.025 -> positive edge -> NOT flagged.
    _row(c, "u-good", "KBOS", 0.079, 0.025, "buy_yes", 0.079, 0.989)
    # A genuinely negative-edge entry: p=0.10 below market 0.30 -> flagged.
    _row(c, "u-bad", "KPHL", 0.10, 0.30, "buy_yes", 0.10, 0.989)
    c.commit()
    monkeypatch.setattr(b, "BOOK", db)
    monkeypatch.setattr(b, "OUT", tmp_path / "out.json")
    b.main()
    res = json.loads((tmp_path / "out.json").read_text())
    flagged = {f["trade_uuid"]: f for f in res["flagged"]}
    assert "u-good" not in flagged, "a low absolute p is not a flag under Kelly"
    assert "u-bad" in flagged
    assert "negative edge entered" in flagged["u-bad"]["rules"]
    c.close()


@pytest.mark.unit
def test_r5_retired_raw_edge_does_not_flag_penny(tmp_path, monkeypatch):
    """The retired raw %-edge rule must never fire on a legitimate penny entry."""
    b = _load("bullshit_crawl")
    db = tmp_path / "book.db"
    c = _book(db)
    # KAUS-like: raw edge 3031% by construction on a 0.025 penny -> not flagged.
    _row(c, "u-penny", "KAUS", 0.7775, 0.025, "buy_yes", 0.7829, 0.99)
    c.commit()
    monkeypatch.setattr(b, "BOOK", db)
    monkeypatch.setattr(b, "OUT", tmp_path / "out.json")
    b.main()
    res = json.loads((tmp_path / "out.json").read_text())
    assert res["flags_per_rule"]["calibration bound violated"] == 0
    assert not any(f["trade_uuid"] == "u-penny" for f in res["flagged"])
    c.close()


@pytest.mark.unit
def test_r4_flags_only_true_saturation(tmp_path, monkeypatch):
    """R4 flags the raw YES p pinned at the ceiling; a below-ceiling p is fine."""
    b = _load("bullshit_crawl")
    db = tmp_path / "book.db"
    c = _book(db)
    _row(c, "u-sat", "KNYC", 1.0, 0.035, "buy_yes", 1.0, 0.9878)
    _row(c, "u-ok", "KMSP", 0.30, 0.19, "buy_no", 0.3024, 0.99)
    c.commit()
    monkeypatch.setattr(b, "BOOK", db)
    monkeypatch.setattr(b, "OUT", tmp_path / "out.json")
    b.main()
    res = json.loads((tmp_path / "out.json").read_text())
    flagged = {f["trade_uuid"]: f for f in res["flagged"]}
    assert "u-sat" in flagged
    assert "saturated flat signal" in flagged["u-sat"]["rules"]
    assert "u-ok" not in flagged
    c.close()


@pytest.mark.unit
def test_v2_only_honest_negative_edge_is_a_defect(tmp_path, monkeypatch):
    """v2: a positive-edge penny is clean; only a negative honest edge is a defect."""
    b = _load("bullshit_crawl_v2")
    db = tmp_path / "book.db"
    c = _book(db)
    _row(c, "u-good", "KBOS", 0.079, 0.025, "buy_yes", 0.079, 0.989)
    _row(c, "u-bad", "KPHL", 0.10, 0.30, "buy_yes", 0.10, 0.989)
    c.commit()
    monkeypatch.setattr(b, "BOOK", db)
    monkeypatch.setattr(b, "OUT", tmp_path / "out.json")
    b.main()
    res = json.loads((tmp_path / "out.json").read_text())
    assert res["honest_negative_edge"] == 1, res["honest_negative_edge"]
    c.close()


@pytest.mark.unit
def test_live_crawl_json_shows_no_pathology():
    """The committed live crawl JSON must not report the 10 rows as pathology:
    v1 flags only honest saturation; v2 reports zero honest negative edges."""
    for name in ("bullshit_crawl.json", "bullshit_crawl_v2.json"):
        p = Path(_REPO) / "data" / name
        if not p.exists():
            pytest.skip(f"{name} not present")
        d = json.loads(p.read_text())
        if name == "bullshit_crawl.json":
            # The only permitted flag class on the current book is saturation.
            assert d["flags_per_rule"]["bankroll/position mismatch"] == 0
            assert d["flags_per_rule"]["over max position"] == 0
            assert d["flags_per_rule"]["negative edge entered"] == 0
            assert d["flags_per_rule"]["calibration bound violated"] == 0
        else:
            assert d["honest_negative_edge"] == 0
