import csv, json, sqlite3
import pytest
from scripts.b2_input_schema_gate import convert_price, inspect, run


def make_db(path, rows):
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE market_snapshots (snapshot_time TEXT, station TEXT, series_ticker TEXT, market_ticker TEXT, strike_temp REAL, strike_source_field TEXT, strike_provenance_status TEXT, market_type TEXT, yes_bid REAL, yes_ask REAL, mid_price REAL, price_unit TEXT, bid_source_field TEXT, ask_source_field TEXT, api_contract_version TEXT, provenance_status TEXT)')
        enriched = [
            (r[0], r[1], r[2], r[3], r[4], 'strike_price', 'verified', r[5],
             r[6], r[7], r[8], 'cents', 'yes_bid', 'yes_ask',
             'kalshi-contract-test', 'verified')
            for r in rows
        ]
        c.executemany('INSERT INTO market_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', enriched)


def test_price_units_are_explicit_and_deterministic():
    assert convert_price(25, 'cents') == (0.25, None)
    assert convert_price(0.25, 'dollars') == (0.25, None)
    assert convert_price(25, 'dollars')[1] == 'outside_open_probability_range'
    assert convert_price(25, 'auto')[1] == 'unit_not_declared'


def test_gate_classifies_missing_calibration_and_short_hours(tmp_path):
    db = tmp_path / 'z8.db'; make_db(db, [('2026-09-10T01:00:00+00:00','KATL','S','M',72,'binary',20,40,30)])
    result = inspect(str(db), str(tmp_path / 'missing.csv'), 'dollars')
    assert result['eligible'] is False
    assert 'snapshot_hour_coverage_lt_72' in result['missing_inputs']
    assert 'row_level_calibrated_probability_artifact' in result['missing_inputs']
    assert result['snapshot']['prices_valid'] is True


def test_gate_accepts_complete_fixture_and_exact_join(tmp_path):
    db = tmp_path / 'z8.db'; rows = [('2026-09-%02dT01:00:00+00:00' % (d+1),'KATL','S','M%d'%d,72,'binary',20,40,30) for d in range(72)]
    make_db(db, rows)
    cal = tmp_path / 'cal.csv'
    fields = ['snapshot_hour','station','series_ticker','market_ticker','market_type','strike_temp','strike_source_field','strike_provenance_status','calibrated_probability','calibration_source','calibration_version']
    with cal.open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
        for r in rows: w.writerow(dict(zip(fields,[r[0][:13],r[1],r[2],r[3],'binary',r[4],'strike_price','verified','.55','fixture','v1'])))
    result = inspect(str(db), str(cal), 'dollars')
    assert result['eligible'] is True
    assert result['join']['exact_key_complete'] is True
    assert result['snapshot']['distinct_hours'] == 72


def test_run_writes_gate_receipt_and_fails_closed(tmp_path):
    db = tmp_path / 'z8.db'; make_db(db, [('2026-09-10T01:00:00+00:00','KATL','S','M',72,'binary',20,40,30)])
    out = tmp_path / 'receipt.json'
    with pytest.raises(SystemExit) as exc: run(str(db), str(tmp_path/'none.csv'), str(out), 'dollars')
    assert exc.value.code == 2
    assert json.loads(out.read_text())['status'] == 'gated'


def test_unknown_unverified_price_provenance_is_not_b2_eligible(tmp_path):
    db = tmp_path / 'z8.db'
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE market_snapshots (snapshot_time TEXT, station TEXT, series_ticker TEXT, market_ticker TEXT, strike_temp REAL, strike_source_field TEXT, strike_provenance_status TEXT, market_type TEXT, yes_bid REAL, yes_ask REAL, mid_price REAL, price_unit TEXT, bid_source_field TEXT, ask_source_field TEXT, api_contract_version TEXT, provenance_status TEXT)')
        c.execute("INSERT INTO market_snapshots VALUES ('2026-09-10T01:00:00+00:00','KATL','S','M',72,'strike_price','verified','binary',31,37,34,'cents','yes_bid','yes_ask','UNKNOWN_UNVERIFIED','unverified')")
    result = inspect(str(db), str(tmp_path / 'missing.csv'), 'dollars')
    assert result['eligible'] is False
    assert 'validated_price_rows' in result['missing_inputs']
