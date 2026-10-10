#!/usr/bin/env python3
"""
Keep-fresh step — the daily upkeep must append finalized canonical settlement
to the frozen `settlement_snapshot` so the forward settle path has canonical
truth for newly settled days (else new trades stay open forever).

Locks the 2026-10-10 forward-population fix (scripts/kalshi_settlement_upkeep.py):
  * a settled station-day missing from the snapshot is APPENDED from canonical
    truth (Kalshi live `expiration_value`);
  * re-running is idempotent (identical value -> skipped, no error);
  * a fetched canonical value that would MOVE a canonically sourced frozen row
    FAILS LOUD (ImmutableSettlementError) and is never overwritten;
  * a frozen row seeded from a non-canonical last-resort proxy (weatherapi/GHCN)
    that diverges from canonical truth is surfaced as provenance drift WITHOUT
    raising and WITHOUT being overwritten;
  * the current UTC day is never frozen (HIGH settles T+1); a past day the live
    API has not reported falls back to the canonical cross-check.

Run: python3 -m pytest tests/test_settlement_upkeep_keepfresh.py -v
"""
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from core.canonical_settlement import (  # noqa: E402
    ImmutableSettlementError, ensure_snapshot_table, freeze_snapshot,
)
import kalshi_settlement_upkeep as upk  # noqa: E402

_NOW = datetime(2026, 10, 10, 8, 0, 0, tzinfo=timezone.utc)  # Oct-10 run
_STATION = "KATL"


def _snapshot_path(tmp_path):
    return str(tmp_path / "metar_backfill.db")


def _freeze_row(db, date, temp, source):
    conn = sqlite3.connect(db)
    ensure_snapshot_table(conn)
    freeze_snapshot(conn, [{
        "station": _STATION, "market_type": "HIGH", "local_trading_date": date,
        "settlement_temp_f": temp, "temp_source": source, "settled_at_utc": "x",
    }], frozen_at_utc="t0")
    conn.commit()
    conn.close()


def _read(db, date):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    row = conn.execute(
        "SELECT settlement_temp_f, temp_source, settled_at_utc FROM settlement_snapshot "
        "WHERE station=? AND market_type='HIGH' AND local_trading_date=?",
        (_STATION, date)).fetchone()
    conn.close()
    return row


def _live_finalized_except(today_tkr_last):
    """Live fetcher: finalized (80.0) for every event except the given one."""
    def f(ev):
        if ev.endswith(today_tkr_last):
            return None
        return (80.0, "2026-10-11T05:00:00Z")
    return f


# ─── append ───────────────────────────────────────────────────────────────
def test_appends_settled_day_from_live_and_skips_current_day(tmp_path):
    db = _snapshot_path(tmp_path)
    stats = upk.freeze_canonical_snapshots(
        days_back=3, snapshot_db=db, stations=[_STATION], now=_NOW,
        live_fetcher=_live_finalized_except("26OCT10"), fallback_value=lambda s, d: None)
    assert stats["snapshot_inserted"] == 2          # Oct-08 + Oct-09
    assert _read(db, "2026-10-08")[0] == 80.0
    assert _read(db, "2026-10-08")[1] == "kalshi_live_api"
    assert _read(db, "2026-10-08")[2] == "2026-10-11T05:00:00Z"  # as-of anchor
    assert _read(db, "2026-10-09")[0] == 80.0
    assert _read(db, "2026-10-10") is None          # current day never frozen


def test_fallback_used_for_past_day_live_has_not_reported(tmp_path):
    db = _snapshot_path(tmp_path)
    stats = upk.freeze_canonical_snapshots(
        days_back=3, snapshot_db=db, stations=[_STATION], now=_NOW,
        live_fetcher=lambda ev: None,                # live reports nothing
        fallback_value=lambda s, d: 84.0 if d == "2026-10-08" else None)
    assert stats["snapshot_inserted"] == 1
    assert _read(db, "2026-10-08") == (84.0, "fallback_cross_check", None)
    assert _read(db, "2026-10-09") is None           # no canonical value -> unfrozen
    assert _read(db, "2026-10-10") is None           # current day never frozen


# ─── idempotency ──────────────────────────────────────────────────────────
def test_rerun_is_idempotent(tmp_path):
    db = _snapshot_path(tmp_path)
    f = _live_finalized_except("26OCT10")
    first = upk.freeze_canonical_snapshots(
        days_back=3, snapshot_db=db, stations=[_STATION], now=_NOW,
        live_fetcher=f, fallback_value=lambda s, d: None)
    second = upk.freeze_canonical_snapshots(
        days_back=3, snapshot_db=db, stations=[_STATION], now=_NOW,
        live_fetcher=f, fallback_value=lambda s, d: None)
    assert first["snapshot_inserted"] == 2
    assert second["snapshot_inserted"] == 0
    assert second["snapshot_identical"] == 2
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    n = conn.execute("SELECT COUNT(*) FROM settlement_snapshot").fetchone()[0]
    conn.close()
    assert n == 2                                    # nothing added, nothing moved


# ─── immutability ─────────────────────────────────────────────────────────
def test_canonical_value_that_would_move_fails_loud(tmp_path):
    db = _snapshot_path(tmp_path)
    _freeze_row(db, "2026-10-08", 85.0, "kalshi_live_api")   # canonical-frozen
    with pytest.raises(ImmutableSettlementError):
        upk.freeze_canonical_snapshots(
            days_back=3, snapshot_db=db, stations=[_STATION], now=_NOW,
            live_fetcher=lambda ev: (90.0, "a") if ev.endswith("26OCT08") else None,
            fallback_value=lambda s, d: None)
    assert _read(db, "2026-10-08")[0] == 85.0         # NOT overwritten


def test_proxy_seed_drift_is_surfaced_not_overwritten(tmp_path):
    db = _snapshot_path(tmp_path)
    # Seeded by the backfill's non-canonical last-resort proxy.
    _freeze_row(db, "2026-10-08", 99.5, "weatherapi")
    stats = upk.freeze_canonical_snapshots(
        days_back=3, snapshot_db=db, stations=[_STATION], now=_NOW,
        live_fetcher=lambda ev: (104.0, "a") if ev.endswith("26OCT08") else None,
        fallback_value=lambda s, d: None)
    assert stats["snapshot_provenance_drift"] == 1
    assert stats["snapshot_conflicts"] == 0           # proxy drift is not a "move"
    assert _read(db, "2026-10-08") == (99.5, "weatherapi", "x")  # untouched


# ─── CLI wiring ───────────────────────────────────────────────────────────
def test_run_upkeep_accepts_freeze_toggle():
    import inspect
    assert "freeze_snapshots" in inspect.signature(upk.run_upkeep).parameters
