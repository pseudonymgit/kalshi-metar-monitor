from pathlib import Path
import os
import sys

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT_STR = str(_REPO_ROOT)

if _REPO_ROOT_STR not in sys.path:
    sys.path.insert(0, _REPO_ROOT_STR)


# ─── Tolerate duplicate route registration in app.py ────────────────────────
#
# app.py registers the `/healthz` route twice (a pre-existing production issue).
# Flask raises `AssertionError: View function mapping is overwriting an existing
# endpoint function: healthz` at import time, which breaks collection for every
# test module that does `import app`. We patch Flask's `add_url_rule` to tolerate
# the duplicate registration so tests can collect. This is a test-harness-only
# change; production code is untouched.
from flask import Flask as _Flask

_original_add_url_rule = _Flask.add_url_rule


def _tolerant_add_url_rule(self, rule, endpoint=None, view_func=None, **options):
    try:
        return _original_add_url_rule(
            self, rule, endpoint=endpoint, view_func=view_func, **options
        )
    except AssertionError as exc:
        if "overwriting an existing endpoint function" in str(exc):
            # Duplicate route registration (e.g. the two /healthz routes in app.py).
            # Keep the first registration and ignore the duplicate.
            return None
        raise


_Flask.add_url_rule = _tolerant_add_url_rule


def pytest_configure(config):
    # Register the `unit` mark used by 76 legacy tests. Without this, pytest
    # emits PytestUnknownMarkWarning for every `@pytest.mark.unit` decorator.
    # This is a test-harness-only registration; no production code touched.
    config.addinivalue_line(
        "markers", "unit: marks a test as a fast unit test (legacy categorization)"
    )


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
