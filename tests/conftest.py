from pathlib import Path
import os
import sqlite3
import sys

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT_STR = str(_REPO_ROOT)

if _REPO_ROOT_STR not in sys.path:
    sys.path.insert(0, _REPO_ROOT_STR)

# R/T (2026-09-29): production data directory — tests must never OPEN these for
# writing. Corruption of data/*.db is how a test run can destroy a real archive.
_PROD_DATA_DIR = (_REPO_ROOT / "data").resolve()

# Sentinel so tests can intentionally opt out (rare; must be deliberate).
_ALLOW_PROD_WRITE_ENV = "OPENCLAW_TESTS_ALLOW_PROD_WRITE"


@pytest.fixture(autouse=True)
def restore_alert_db_path_env():
    original = os.environ.get("ALERT_DB_PATH")
    try:
        yield
    finally:
        if original is None:
            os.environ.pop("ALERT_DB_PATH", None)
        else:
            os.environ["ALERT_DB_PATH"] = original


def _is_prod_write(database, uri: bool) -> str | None:
    """Return the offending path if `database` is a production DB opened for
    writing. Read-only (uri mode=ro) opens are allowed."""
    if database is None:
        return None
    # Memory / temp DBs are always fine.
    s = str(database)
    if s in (":memory:", "") or s.startswith("file::memory:"):
        return None

    path_part = s
    is_readonly = False
    if uri or s.startswith("file:"):
        # parse file:...?mode=ro
        if "mode=ro" in s or "mode=memory" in s:
            is_readonly = True
        path_part = s[len("file:"):].split("?", 1)[0] if s.startswith("file:") else s
    if is_readonly:
        return None

    try:
        p = Path(path_part)
        if not p.is_absolute():
            p = (_REPO_ROOT / p)
        p = p.resolve()
    except Exception:
        return None

    # Only guard the real data/ directory.
    if p != _PROD_DATA_DIR and _PROD_DATA_DIR not in p.parents:
        return None
    # Temp DBs created under data/ by a fixture are unlikely; treat all as prod.
    return str(p)


@pytest.fixture(autouse=True)
def db_write_sandbox(monkeypatch):
    """Fail any test that opens a production data/*.db for WRITING.

    Read-only opens (uri mode=ro, or get_readonly_sqlite_connection) are allowed.
    Escape hatch for a deliberate migration test: set
    OPENCLAW_TESTS_ALLOW_PROD_WRITE=1.
    """
    if os.environ.get(_ALLOW_PROD_WRITE_ENV) == "1":
        yield
        return

    real_connect = sqlite3.connect

    def guarded_connect(database, *args, **kwargs):
        offending = _is_prod_write(database, bool(kwargs.get("uri", False)))
        if offending:
            raise AssertionError(
                f"DB sandbox: test tried to open production DB for WRITING: {offending}. "
                f"Use a temp DB (tmp_path / tempfile) or open read-only "
                f"(`sqlite3.connect('file:...?mode=ro', uri=True)`). "
                f"Deliberate override: {_ALLOW_PROD_WRITE_ENV}=1"
            )
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    yield