from pathlib import Path
import os
import sys

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT_STR = str(_REPO_ROOT)

if _REPO_ROOT_STR not in sys.path:
    sys.path.insert(0, _REPO_ROOT_STR)


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
