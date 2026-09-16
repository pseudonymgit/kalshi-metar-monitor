import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.cron_artifact_check import check_post_run
from scripts.z8_kalshi_market_cron import EXPECTED_STATIONS


def manifest(tmp_path, duplicate=False):
    jobs = [{"script": "scripts/z8_kalshi_market_cron.py"}]
    if duplicate:
        jobs.append({"script": "scripts/z8_kalshi_market_cron.py"})
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"jobs": jobs}))
    return path


def db_with_snapshots(path, latest, stations, prior_rows=0, latest_rows=None):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE market_snapshots (snapshot_time TEXT, station TEXT)")
        prior = (datetime.fromisoformat(latest) - timedelta(minutes=5)).isoformat()
        conn.executemany("INSERT INTO market_snapshots VALUES (?, ?)",
                         [(prior, "KATL")] * prior_rows)
        rows = [(latest, s) for s in stations]
        if latest_rows is not None:
            rows = rows[:latest_rows]
        conn.executemany("INSERT INTO market_snapshots VALUES (?, ?)", rows)


def test_passes_exact_20_fresh_snapshot_and_manifest(tmp_path):
    now = datetime(2026, 9, 11, tzinfo=timezone.utc)
    db = tmp_path / "z8.db"
    db_with_snapshots(db, (now - timedelta(minutes=1)).isoformat(), sorted(EXPECTED_STATIONS), prior_rows=1)
    result = check_post_run(str(manifest(tmp_path)), str(db), now=now)
    assert result["ok"] is True
    assert result["artifact"]["exact_station_coverage"] is True


def test_rejects_missing_and_stale_artifact(tmp_path):
    now = datetime(2026, 9, 11, tzinfo=timezone.utc)
    missing = check_post_run(str(manifest(tmp_path)), str(tmp_path / "missing.db"), now=now)
    assert missing["ok"] is False and missing["artifact"]["missing"] is True
    db = tmp_path / "stale.db"
    db_with_snapshots(db, (now - timedelta(hours=2)).isoformat(), sorted(EXPECTED_STATIONS), prior_rows=1)
    stale = check_post_run(str(manifest(tmp_path)), str(db), now=now)
    assert stale["ok"] is False and stale["artifact"]["fresh"] is False


def test_rejects_duplicate_z8_and_bad_coverage_or_row_growth(tmp_path):
    now = datetime(2026, 9, 11, tzinfo=timezone.utc)
    db = tmp_path / "bad.db"
    db_with_snapshots(db, (now - timedelta(minutes=1)).isoformat(), sorted(EXPECTED_STATIONS)[:-1], prior_rows=50)
    result = check_post_run(str(manifest(tmp_path, duplicate=True)), str(db), now=now)
    assert result["ok"] is False
    assert result["manifest"]["duplicate_z8_entries"] == 1
    assert result["artifact"]["exact_station_coverage"] is False
    db2 = tmp_path / "no-growth.db"
    db_with_snapshots(db2, (now - timedelta(minutes=1)).isoformat(), sorted(EXPECTED_STATIONS), prior_rows=20)
    # A single latest row count of 20 cannot be a regression if previous total is greater.
    with sqlite3.connect(db2) as conn:
        conn.execute("DELETE FROM market_snapshots WHERE snapshot_time < ?", ((now - timedelta(minutes=1)).isoformat(),))
        conn.executemany("INSERT INTO market_snapshots VALUES (?, ?)",
                         [((now - timedelta(minutes=2)).isoformat(), "KATL")] * 30)
    result2 = check_post_run(str(manifest(tmp_path)), str(db2), now=now)
    assert result2["ok"] is False and result2["artifact"]["row_growth"] is False


def test_rejects_unknown_station_even_when_count_is_20(tmp_path):
    now = datetime(2026, 9, 11, tzinfo=timezone.utc)
    db = tmp_path / "unknown-station.db"
    stations = sorted(EXPECTED_STATIONS)
    stations[-1] = "KZZZ"
    db_with_snapshots(db, (now - timedelta(minutes=1)).isoformat(), stations,
                      prior_rows=1)
    result = check_post_run(str(manifest(tmp_path)), str(db), now=now)
    assert result["ok"] is False
    assert result["artifact"]["exact_station_coverage"] is False
    assert result["artifact"]["unknown_stations"] == ["KZZZ"]


def test_cli_subprocess_contract_from_unrelated_working_directory(tmp_path):
    """Explicit paths make the monitor safe to invoke from a scheduler cwd."""
    now = datetime.now(timezone.utc)
    script = (Path(__file__).parent.parent / "scripts" / "cron_artifact_check.py").resolve()
    unrelated_cwd = tmp_path / "unrelated-cwd"
    unrelated_cwd.mkdir()
    cases = []

    passing = tmp_path / "passing.db"
    db_with_snapshots(passing, (now - timedelta(seconds=30)).isoformat(),
                      sorted(EXPECTED_STATIONS), prior_rows=1)
    cases.append(("pass", manifest(tmp_path), passing, 0))

    missing = tmp_path / "missing.db"
    cases.append(("missing", manifest(tmp_path), missing, 1))

    stale = tmp_path / "stale.db"
    db_with_snapshots(stale, (now - timedelta(hours=2)).isoformat(),
                      sorted(EXPECTED_STATIONS), prior_rows=1)
    cases.append(("stale", manifest(tmp_path), stale, 1))

    duplicate_manifest = tmp_path / "duplicate-manifest.json"
    duplicate_manifest.write_text(json.dumps({"jobs": [
        {"script": "scripts/z8_kalshi_market_cron.py"},
        {"script": "scripts/z8_kalshi_market_cron.py"},
    ]}))
    duplicate_db = tmp_path / "duplicate.db"
    db_with_snapshots(duplicate_db, (now - timedelta(seconds=30)).isoformat(),
                      sorted(EXPECTED_STATIONS), prior_rows=1)
    cases.append(("duplicate", duplicate_manifest, duplicate_db, 1))

    bad_coverage = tmp_path / "bad-coverage.db"
    db_with_snapshots(bad_coverage, (now - timedelta(seconds=30)).isoformat(),
                      sorted(EXPECTED_STATIONS)[:-1], prior_rows=50)
    cases.append(("bad-coverage", manifest(tmp_path), bad_coverage, 1))

    unknown_stations = sorted(EXPECTED_STATIONS)
    unknown_stations[-1] = "KZZZ"
    unknown = tmp_path / "unknown-station.db"
    db_with_snapshots(unknown, (now - timedelta(seconds=30)).isoformat(),
                      unknown_stations, prior_rows=1)
    cases.append(("unknown-station", manifest(tmp_path), unknown, 1))

    no_growth = tmp_path / "no-growth.db"
    db_with_snapshots(no_growth, (now - timedelta(seconds=30)).isoformat(),
                      sorted(EXPECTED_STATIONS), prior_rows=1)
    with sqlite3.connect(no_growth) as conn:
        conn.execute("DELETE FROM market_snapshots WHERE snapshot_time < ?",
                     ((now - timedelta(seconds=30)).isoformat(),))
        conn.executemany("INSERT INTO market_snapshots VALUES (?, ?)",
                         [((now - timedelta(minutes=2)).isoformat(), "KATL")] * 30)
    cases.append(("row-growth", manifest(tmp_path), no_growth, 1))

    for name, manifest_path, db_path, expected_code in cases:
        completed = subprocess.run(
            [sys.executable, str(script), "--manifest", str(manifest_path),
             "--db", str(db_path)], cwd=unrelated_cwd,
            capture_output=True, text=True, check=False)
        assert completed.returncode == expected_code, name
        assert completed.stderr == "", name
        payload = json.loads(completed.stdout)
        assert set(("checked_at_utc", "manifest", "artifact", "ok")) <= payload.keys()
        assert payload["ok"] is (expected_code == 0), name
