import json
import sqlite3

from scripts.b2_real_price_sweep import run


def test_b2_fails_closed_without_72_hours(tmp_path):
    db = tmp_path / "z8.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE market_snapshots (snapshot_time TEXT)")
        conn.executemany("INSERT INTO market_snapshots VALUES (?)", [(f"2026-09-10T{i:02d}:00:00+00:00",) for i in range(3)])
    out = tmp_path / "b2.json"
    try:
        run(str(db), str(tmp_path / "missing.csv"), str(out))
    except SystemExit as exc:
        assert exc.code == 2
    receipt = json.loads(out.read_text())
    assert receipt["status"] == "gated"
    assert receipt["inputs"]["distinct_hours"] == 3
