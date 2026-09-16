'''
Intraday Health Monitor (v1.0 — 2026-09-04)

L0 production healthcheck for the intraday daemon stack (Alpha workstream A.3).

Checks (each emits a ✅/⚠️/❌ console line; problems also emit a loud alert line):
  1. Daemon running        — PID file exists, process alive, cmdline matches
  2. Log freshness         — daemon log mtime within --log-max-age-min
                             (only enforced while daemon is running)
  3. DB integrity          — PRAGMA quick_check on small DBs, header+trivial
                             query on large DBs (integrity scan would take hours)
  4. DB writable           — probe write into infra-owned table in signal_cache.db
  5. NWP data recent       — data/nwp_forecasts.db mtime within --nwp-max-age-h
  6. Signal fires          — per-station fire count > 0 for today
                             (from intraday_signal_fires; alert only when the
                             daemon actually ran cycles today — fresh deploys
                             with no cycle yet are WARN, not FAIL)

Exit codes:
  0 = healthy
  1 = one or more FAIL checks
  2 = operational error (could not run the checks at all)

Designed for cron (hourly). No email/SMS — console/log lines only.

Usage:
    python3 scripts/intraday_healthcheck.py
    python3 scripts/intraday_healthcheck.py --json        # machine-readable
    python3 scripts/intraday_healthcheck.py --nwp-max-age-h 96

'''

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from scripts.intraday_db import DBPool, write_txn
except ImportError:
    from intraday_db import DBPool, write_txn


PID_FILE = Path('/tmp/intraday-daemon.pid')
DAEMON_LOG = REPO_ROOT / 'logs' / 'intraday_daemon.log'
NWP_DB = REPO_ROOT / 'data' / 'nwp_forecasts.db'
STATE_DB = REPO_ROOT / 'data' / 'signal_cache.db'
DAEMON_HEALTH = REPO_ROOT / 'data' / 'intraday_health.json'


QUICKCHECK_MAX_BYTES = 524288000

CORE_DBS = [
    'metar_backfill.db',
    'nwp_forecasts.db',
    'iem_asos_1min.db',
    'signal_cache.db',
]

ALL_STATIONS = [
    'KATL', 'KAUS', 'KBOS', 'KDCA', 'KDEN', 'KDFW', 'KHOU', 'KLAS', 'KLAX', 'KMDW',
    'KMIA', 'KMSP', 'KMSY', 'KNYC', 'KOKC', 'KPHL', 'KPHX', 'KSAT', 'KSEA', 'KSFO',
]


results: List[Dict] = []


def _record(name, status, detail):
    icon = {'ok': '✅', 'warn': '⚠️ ', 'fail': '❌'}[status]
    line = f'{icon} [{status.upper():4}] {name}: {detail}'
    print(line)
    results.append({'check': name, 'status': status, 'detail': detail})
    if status in ('warn', 'fail'):

        print(f'🚨🚨 ALERT [{name}] {detail} 🚨🚨', flush=True)



def check_daemon():
    pid = None
    try:
        pid = int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        pass

    if pid is None:
        _record('daemon_running', 'fail', f'PID file missing or unreadable ({PID_FILE})')
        return False

    try:
        os.kill(pid, 0)
    except OSError:
        _record('daemon_running', 'fail', f'PID {pid} from PID file is not alive (stale PID file?)')
        return False

    try:
        cmdline = (Path('/proc') / str(pid) / 'cmdline').read_bytes().decode(errors='replace')
        if 'intraday_daemon' not in cmdline:
            _record('daemon_running', 'fail', f'PID {pid} alive but is not intraday_daemon (PID reuse?)')
            return False
    except OSError:
        pass

    _record('daemon_running', 'ok', f'pid {pid} alive')
    return True



def check_log_freshness(daemon_running, max_age_min):
    if not DAEMON_LOG.exists():
        if daemon_running:
            _record('log_fresh', 'fail', f'daemon log missing but daemon is running: {DAEMON_LOG}')
        else:
            _record('log_fresh', 'warn', 'daemon log missing (daemon down — expected)')
        return
    age_min = (time.time() - DAEMON_LOG.stat().st_mtime) / 60
    if not daemon_running:
        _record('log_fresh', 'warn', f'log age {age_min:.0f}min (daemon down — freshness not enforced)')
        return
    if age_min > max_age_min:
        _record('log_fresh', 'fail', f'daemon log stale: {age_min:.0f}min old (max {max_age_min}min)')
        return
    _record('log_fresh', 'ok', f'log age {age_min:.1f}min')



def check_db_integrity():
    data_dir = REPO_ROOT / 'data'
    for name in CORE_DBS:
        p = data_dir / name
        if not p.exists():
            _record(f'db_integrity:{name}', 'fail', 'DB file missing')
            continue
        try:
            size = p.stat().st_size
            con = sqlite3.connect(f'file:{p}?mode=ro', uri=True, timeout=10)
            try:
                if size <= QUICKCHECK_MAX_BYTES:
                    row = con.execute('PRAGMA quick_check').fetchone()
                    verdict = str(row[0]) if row else 'no-result'
                    if verdict.lower() == 'ok':
                        _record(f'db_integrity:{name}', 'ok', f'quick_check ok ({size // 1048576}MB)')
                    else:
                        _record(f'db_integrity:{name}', 'fail', f'quick_check: {verdict}')
                else:
                    # integrity scan would take hours on large DBs — probe header + catalog
                    mode = con.execute('PRAGMA journal_mode').fetchone()[0]
                    con.execute('SELECT count(*) FROM sqlite_master').fetchone()
                    _record(f'db_integrity:{name}', 'ok',
                            f'header+catalog probe ok ({size // 1048576}MB, {mode})')
            finally:
                con.close()
        except sqlite3.DatabaseError as e:
            _record(f'db_integrity:{name}', 'fail', f'DB error (possible corruption): {e}')
            continue


def check_db_writable():
    if not STATE_DB.exists():
        _record('db_writable', 'fail', f'state DB missing: {STATE_DB}')
        return
    pool = DBPool()
    now = datetime.now(timezone.utc).isoformat()
    try:
        with write_txn(pool, str(STATE_DB)) as con:
            con.execute('CREATE TABLE IF NOT EXISTS _healthcheck_probe (k TEXT PRIMARY KEY, ts_utc TEXT)')
            con.execute(
                "INSERT INTO _healthcheck_probe (k, ts_utc) VALUES ('probe', ?) "
                "ON CONFLICT(k) DO UPDATE SET ts_utc = excluded.ts_utc",
                (now,))

        ts = pool.get(str(STATE_DB)).execute(
            "SELECT ts_utc FROM _healthcheck_probe WHERE k='probe'"
        ).fetchone()[0]
        if ts == now:
            _record('db_writable', 'ok', f'probe write committed to {STATE_DB.name}')
        else:
            _record('db_writable', 'fail', f'probe write not visible (read back {ts!r})')
    except Exception as e:
        _record('db_writable', 'fail', f'probe write failed: {e}')
    finally:
        pool.recycle_all()



def check_nwp_freshness(max_age_h):
    if not NWP_DB.exists():
        _record('nwp_fresh', 'fail', f'NWP DB missing: {NWP_DB}')
        return
    age_h = (time.time() - NWP_DB.stat().st_mtime) / 3600
    if age_h > max_age_h:
        _record('nwp_fresh', 'fail', f'NWP data stale: {age_h:.1f}h old (max {max_age_h}h)')
        return

    try:
        con = sqlite3.connect(f'file:{NWP_DB}?mode=ro', uri=True, timeout=10)
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        detail = f'file mtime {age_h:.1f}h old'
        con.close()
        _record('nwp_fresh', 'ok', detail + f' ({len(tables)} tables)')
    except sqlite3.Error as e:
        _record('nwp_fresh', 'fail', f'NWP DB unreadable: {e}')
        return


def check_signal_fires():
    if not STATE_DB.exists():
        _record('signal_fires', 'fail', f'state DB missing: {STATE_DB}')
        return
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    try:
        con = sqlite3.connect(f'file:{STATE_DB}?mode=ro', uri=True, timeout=10)
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'intraday_signal_fires' not in tables:
            con.close()
            _record('signal_fires', 'warn',
                    'fires table not created yet (daemon has never run — fresh deploy)')
            return
        fires = dict(con.execute('SELECT station, fires FROM intraday_signal_fires WHERE date_utc = ?',
                                 (today,)).fetchall())

        cycles_today = 0
        if 'intraday_daemon_cycles' in tables:
            cycles_today = con.execute('SELECT COUNT(*) FROM intraday_daemon_cycles WHERE ts_utc LIKE ?',
                                       (f'{today}%',)).fetchone()[0]

        con.close()

        if cycles_today == 0:
            _record('signal_fires', 'warn', 'no daemon cycle today — fire counts not yet meaningful')
            return

        if not fires:
            _record('signal_fires', 'fail',
                    f'daemon ran {cycles_today} cycle(s) today but zero stations recorded fires')
            return

        zero = [st for st in ALL_STATIONS if fires.get(st, 0) == 0]
        positive = {st: n for st, n in fires.items() if n > 0}

        if zero and positive:
            _record('signal_fires', 'warn',
                    f'fires today: {positive}; zero-fire stations: {len(zero)}/{len(ALL_STATIONS)}')
            return
        if positive:
            _record('signal_fires', 'ok', f'fires today: {positive}')
            return
        _record('signal_fires', 'fail', f'all 20 stations have zero fires despite {cycles_today} cycle(s)')
    except sqlite3.Error as e:
        _record('signal_fires', 'fail', f'state DB error: {e}')


def check_daemon_health_file(daemon_running):
    if not DAEMON_HEALTH.exists():
        if daemon_running:
            _record('daemon_healthfile', 'fail', 'daemon running but no health heartbeat file')
        else:
            _record('daemon_healthfile', 'warn', 'no health heartbeat file (daemon down)')
        return
    try:
        h = json.loads(DAEMON_HEALTH.read_text())
        age_s = _age_of_iso(h.get('updated_at_utc', ''))
        if daemon_running and age_s is not None and age_s > 300:
            _record('daemon_healthfile', 'fail', f'heartbeat stale: {age_s / 60:.0f}min old')
            return

        _record('daemon_healthfile', 'ok',
                f"status={h.get('status')} cycles={h.get('cycle_count')} updated={h.get('updated_at_utc')}")
    except (OSError, json.JSONDecodeError, ValueError) as e:
        _record('daemon_healthfile', 'fail', f'health file unreadable: {e}')


def _age_of_iso(iso):
    try:
        then = datetime.fromisoformat(iso)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - then).total_seconds()
    except ValueError:
        return None



def main():
    ap = argparse.ArgumentParser(description='Intraday stack health monitor (console alerts only)')
    ap.add_argument('--json', action='store_true', help='print JSON summary at end')
    ap.add_argument('--log-max-age-min', type=int, default=90,
                    help='max daemon log age while running (default 90)')
    ap.add_argument('--nwp-max-age-h', type=float, default=72.0,
                    help='max NWP DB age in hours (default 72, matches runner)')
    args = ap.parse_args()

    print(f'── Intraday healthcheck @ {datetime.now(timezone.utc).isoformat()} ──')

    daemon_running = check_daemon()
    check_daemon_health_file(daemon_running)
    check_log_freshness(daemon_running, args.log_max_age_min)
    check_db_integrity()
    check_db_writable()
    check_nwp_freshness(args.nwp_max_age_h)
    check_signal_fires()

    fails = sum(1 for r in results if r['status'] == 'fail')
    warns = sum(1 for r in results if r['status'] == 'warn')
    print(f'── Summary: {len(results)} checks, {fails} FAIL, {warns} WARN ──')
    if args.json:
        print(json.dumps(results, indent=2))

    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())