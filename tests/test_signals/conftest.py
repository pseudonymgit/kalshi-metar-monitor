"""Hermetic DB redirection for the signal unit tests.

Several signal classes resolve their SQLite paths into the production ``data/``
directory — either via a module-level constant (``BIAS_DB_PATH``, ``DATA_DIR``),
a computed fallback in ``_resolve_db_path``, or a constructor default
(``gefs_db``/``tigge_db``). The autouse DB-write sandbox in ``tests/conftest.py``
correctly refuses to open those production files for writing, so every such
path must be redirected to a temp location *before* a signal is constructed.

This fixture is pure test scaffolding: it never opens a production DB and never
touches a file under ``data/``. No business logic is changed.
"""
import sqlite3

import pytest


@pytest.fixture(autouse=True)
def hermetic_signal_dbs(monkeypatch, tmp_path):
    """Redirect every signal DB path used under tests/test_signals/ to tmp_path."""
    tmp_data = tmp_path / "data"
    tmp_data.mkdir(parents=True, exist_ok=True)

    # 1) WeatherAPI-archive consumers (CloudCoverIndexSignal, FeelsLikeDeltaSignal,
    #    ...). `_resolve_db_path` rebuilds the default from __file__ at call time,
    #    so patch the resolver to point at a temp archive DB with an (empty)
    #    `hourly` table — evaluate_for_station then degrades to (None, 0.0).
    archive = tmp_data / "weatherapi_archive.db"
    conn = sqlite3.connect(str(archive))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS hourly ("
        "station TEXT, date TEXT, hour INTEGER, cloud REAL, "
        "temp_c REAL, feelslike_c REAL)"
    )
    conn.commit()
    conn.close()

    from core.signals import weatherapi_base_signal as _wa
    monkeypatch.setattr(
        _wa.WeatherAPIBaseSignal,
        "_resolve_db_path",
        lambda self: str(archive),
    )

    # 2) Module-level DB-path constants + DATA_DIR (their __init__ does
    #    os.makedirs(DATA_DIR) and sqlite3.connect(BIAS_DB_PATH)).
    from core.signals import hrrr_bias_corrected_signal as _hrrr
    monkeypatch.setattr(_hrrr, "DATA_DIR", str(tmp_data))
    monkeypatch.setattr(_hrrr, "BIAS_DB_PATH", str(tmp_data / "hrrr_bias.db"))

    from core.signals import ecmwf_bias_corrected_signal as _ecmwf
    monkeypatch.setattr(_ecmwf, "DATA_DIR", str(tmp_data))
    monkeypatch.setattr(_ecmwf, "BIAS_DB_PATH", str(tmp_data / "ecmwf_bias.db"))
    monkeypatch.setattr(_ecmwf, "_ECMWF_CACHE_DIR", str(tmp_data / "ecmwf_cache"))

    # 3) Constructor defaults for the 82-member ensemble (gefs_db / tigge_db).
    #    Patch the function defaults so `EightyTwoMemberEnsembleSignal()` resolves
    #    to temp DBs without altering the class body.
    from core.signals import eighty_two_member_ensemble_signal as _ens
    _init = _ens.EightyTwoMemberEnsembleSignal.__init__
    # Preserve arity/order: (db_path, gefs_db, tigge_db, weighting_mode,
    # threshold, ece_weights_path, forecast_step). Only repoint the DB paths.
    _orig_defaults = tuple(_init.__defaults__ or ())
    monkeypatch.setattr(
        _init,
        "__defaults__",
        (None, str(tmp_data / "gefs_archive.db"), str(tmp_data / "tigge_archive.db"))
        + _orig_defaults[3:],
    )

    yield
