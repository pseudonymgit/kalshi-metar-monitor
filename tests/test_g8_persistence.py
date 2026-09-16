import json

from core.additive.base import AdditiveSignalResult
from core.additive.monitor import load_persisted_results, persist_results, run_daily_dashboard, link_t_plus_one_outcomes


def test_g8_persistence_and_dashboard_are_nonempty(tmp_path):
    results = [
        AdditiveSignalResult(signal="cloud_clearing", offset_f=1.0, confidence=0.8, fired=True),
        AdditiveSignalResult(signal="dryline_cooling", offset_f=0.0, confidence=0.2, fired=False),
    ]
    assert persist_results(str(tmp_path), "KNYC", "2026-09-09", results) == 2
    loaded, errors = load_persisted_results(str(tmp_path))
    assert len(loaded) == 1
    assert errors == {}
    report = run_daily_dashboard(str(tmp_path))
    assert report["summary"]["n_evaluated_days"] == 1
    assert report["summary"]["persistence_rows"] == 2
    snapshot = tmp_path / "intraday" / "dashboard" / "G8_last_snapshot.json"
    assert json.loads(snapshot.read_text())["summary"]["n_evaluated_days"] == 1


def test_t_plus_one_links_read_only_settlement_source(tmp_path):
    import sqlite3
    results = [AdditiveSignalResult(signal="cloud_clearing")]
    persist_results(str(tmp_path), "KNYC", "2026-09-09", results)
    with sqlite3.connect(tmp_path / "metar_backfill.db") as conn:
        conn.execute("CREATE TABLE settlement_epochs (station TEXT, local_trading_date TEXT, settlement_bucket INTEGER, epoch_status TEXT)")
        conn.execute("INSERT INTO settlement_epochs VALUES ('KNYC','2026-09-09',84,'closed')")
        conn.commit()
    receipt = link_t_plus_one_outcomes(str(tmp_path))
    assert receipt["matched"] == 1
    with sqlite3.connect(tmp_path / "intraday" / "additive_results.db") as conn:
        assert conn.execute("SELECT outcome_bucket, outcome_source FROM additive_results").fetchone() == (84, "metar_backfill.settlement_epochs")
