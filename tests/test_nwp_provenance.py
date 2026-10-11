"""Tests for the NWP run-provenance as-of accessor (Sprint B / I4).

All tests build their own throwaway SQLite DB under tmp_path — they never open
the production data/nwp_forecasts.db.
"""
from datetime import datetime, timezone

import pytest

from core.nwp_provenance import (
    ProvenanceMissingError,
    asof_params,
    backfill_fetched_at,
    backfill_run_init_utc_derived,
    ensure_provenance_columns,
    is_provenance_visible,
    provenance_present,
    query_asof,
    resolve_decision_instant,
    run_init_utc_from_cycle,
)

LEGACY_SCHEMA = """
CREATE TABLE nwp_forecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fetch_date TEXT NOT NULL,
    target_date TEXT NOT NULL,
    station TEXT NOT NULL,
    model TEXT NOT NULL,
    variable TEXT NOT NULL,
    value REAL,
    fetch_timestamp TEXT NOT NULL,
    UNIQUE(fetch_date, target_date, station, model, variable)
)
"""


def _legacy_db(path, rows):
    import sqlite3
    c = sqlite3.connect(path)
    c.execute(LEGACY_SCHEMA)
    c.executemany(
        "INSERT INTO nwp_forecasts "
        "(fetch_date,target_date,station,model,variable,value,fetch_timestamp) "
        "VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    c.commit()
    return c


# --------------------------------------------------------------------------
# cycle attribution
# --------------------------------------------------------------------------

@pytest.mark.unit
def test_cycle_flooring():
    assert run_init_utc_from_cycle(datetime(2026, 10, 11, 6, 12, tzinfo=timezone.utc)) \
        == "2026-10-11T06:00:00+00:00"
    assert run_init_utc_from_cycle(datetime(2026, 10, 11, 0, 5, tzinfo=timezone.utc)) \
        == "2026-10-11T00:00:00+00:00"
    assert run_init_utc_from_cycle(datetime(2026, 10, 11, 23, 59, tzinfo=timezone.utc)) \
        == "2026-10-11T18:00:00+00:00"


@pytest.mark.unit
def test_resolve_decision_instant_z_suffix():
    assert resolve_decision_instant("2026-10-11T09:00Z") == "2026-10-11T09:00:00+00:00"


# --------------------------------------------------------------------------
# pure predicate
# --------------------------------------------------------------------------

@pytest.mark.unit
def test_predicate_fails_closed_on_missing_run_init():
    d = "2026-10-11T09:00:00+00:00"
    assert is_provenance_visible("2026-10-11T06:00:00+00:00",
                                 "2026-10-11T08:00:00+00:00", d) is True
    assert is_provenance_visible("2026-10-11T12:00:00+00:00",
                                 "2026-10-11T12:30:00+00:00", d) is False
    # Unknown vintage is not evidence.
    assert is_provenance_visible(None, "2026-10-11T08:00:00+00:00", d) is False
    # Documented conservative fallback.
    assert is_provenance_visible(None, "2026-10-11T08:00:00+00:00", d, strict=False) is True


# --------------------------------------------------------------------------
# migration
# --------------------------------------------------------------------------

@pytest.mark.unit
def test_migration_leaves_rows_intact(tmp_path):
    db = tmp_path / "nwp.db"
    c = _legacy_db(db, [("2026-10-01", "2026-10-02", "KNYC", "gfs",
                         "temperature_2m_max", 50.0, "2026-10-01T08:00:00+00:00")])
    before = c.execute("SELECT COUNT(*) FROM nwp_forecasts").fetchone()[0]
    assert provenance_present(c) is False

    status = ensure_provenance_columns(c)
    assert status["run_init_utc"] == "added"
    assert status["fetched_at"] == "added"
    assert provenance_present(c) is True
    after = c.execute("SELECT COUNT(*) FROM nwp_forecasts").fetchone()[0]
    assert before == after == 1
    # Idempotent.
    assert ensure_provenance_columns(c) == {"run_init_utc": "present",
                                            "fetched_at": "present"}
    # fetched_at backfilled from legacy column; run_init_utc stays NULL.
    assert backfill_fetched_at(c) == 1
    row = c.execute("SELECT fetched_at, run_init_utc FROM nwp_forecasts").fetchone()
    assert row[0] == "2026-10-01T08:00:00+00:00"
    assert row[1] is None


@pytest.mark.unit
def test_derived_run_init_is_opt_in_and_fails_closed(tmp_path):
    db = tmp_path / "nwp.db"
    c = _legacy_db(db, [("2026-10-01", "2026-10-02", "KNYC", "gfs",
                         "temperature_2m_max", 50.0, "2026-10-01T08:05:00+00:00")])
    ensure_provenance_columns(c)
    backfill_fetched_at(c)
    # dry-run reports but does not write
    assert backfill_run_init_utc_derived(c, dry_run=True) == 1
    assert c.execute("SELECT run_init_utc FROM nwp_forecasts").fetchone()[0] is None
    n = backfill_run_init_utc_derived(c)
    assert n == 1
    assert c.execute("SELECT run_init_utc FROM nwp_forecasts").fetchone()[0] \
        == "2026-10-01T06:00:00+00:00"


# --------------------------------------------------------------------------
# as-of query accessor
# --------------------------------------------------------------------------

@pytest.mark.unit
def test_query_asof_raises_without_provenance(tmp_path):
    db = tmp_path / "nwp.db"
    c = _legacy_db(db, [])
    with pytest.raises(ProvenanceMissingError):
        query_asof(c, decision_instant="2026-10-11T00:00:00+00:00", station="KNYC")


@pytest.mark.unit
def test_query_asof_refuses_post_decision_run(tmp_path):
    db = tmp_path / "nwp.db"
    c = _legacy_db(db, [
        ("2026-10-11", "2026-10-12", "ZTST", "gfs", "temperature_2m_max", 50.0,
         "2026-10-11T08:05:00+00:00"),
        ("2026-10-11", "2026-10-12", "ZTST", "gfs", "temperature_2m_min", 99.0,
         "2026-10-11T12:30:00+00:00"),
    ])
    ensure_provenance_columns(c)
    c.execute("UPDATE nwp_forecasts SET run_init_utc='2026-10-11T06:00:00+00:00',"
              " fetched_at=fetch_timestamp WHERE variable='temperature_2m_max'")
    c.execute("UPDATE nwp_forecasts SET run_init_utc='2026-10-11T12:00:00+00:00',"
              " fetched_at=fetch_timestamp WHERE variable='temperature_2m_min'")
    c.commit()

    rows = query_asof(c, decision_instant="2026-10-11T09:00:00+00:00", station="ZTST")
    assert [r["variable"] for r in rows] == ["temperature_2m_max"]

    rows13 = query_asof(c, decision_instant="2026-10-11T13:00:00+00:00", station="ZTST")
    assert len(rows13) == 2


@pytest.mark.unit
def test_asof_params_binds_decision_twice():
    assert asof_params("2026-10-11T09:00:00+00:00") == (
        "2026-10-11T09:00:00+00:00", "2026-10-11T09:00:00+00:00")
