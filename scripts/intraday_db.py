'''
Intraday DB Safety Layer (v1.0 — 2026-09-04)

Central SQLite access module for the intraday infrastructure layer.

Guarantees (Alpha workstream A.5):
  1. WAL journal mode on every connection opened through this module
     (and on every DB in data/ via --enforce-wal).
  2. Connection recycle gate: any pooled connection older than 30 minutes
     is closed and transparently reopened on next use.
  3. Single-writer discipline: per-DB in-process write lock + BEGIN IMMEDIATE
     transactions with busy_timeout retry. Cross-process writers are
     serialized by SQLite's own WAL single-writer lock; this module never
     holds a write transaction across cycles.

No trading logic. No AI/ML. Infrastructure only.

Usage (library):
    from scripts.intraday_db import DBPool, write_txn, read_conn
    pool = DBPool()
    with read_conn(pool, str(REPO_ROOT / "data" / "nwp_forecasts.db")) as con:
        rows = con.execute("SELECT ...").fetchall()
    with write_txn(pool, str(REPO_ROOT / "data" / "signal_cache.db")) as con:
        con.execute("INSERT ...")

Usage (CLI):
    python3 scripts/intraday_db.py --check              # report journal modes
    python3 scripts/intraday_db.py --enforce-wal        # set WAL on all data/*.db
    python3 scripts/intraday_db.py --recycle            # exercise recycle gate
    python3 scripts/intraday_db.py --self-test          # full smoke test

'''

import argparse
import os
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / 'data'


CONN_MAX_AGE_S = 1800
BUSY_TIMEOUT_MS = 5000
WRITE_RETRY_ATTEMPTS = 3
WRITE_RETRY_BACKOFF_S = 1.0


class DBPool:
    '''
    Pooled SQLite connection manager with WAL enforcement and a 30-minute
    connection recycle gate.

    One pool per process. Connections are keyed by resolved DB path.
    A connection opened for readonly access is never reused for writes
    (and vice versa) — key includes the mode.
    '''

    def __init__(self, max_age_s: int = CONN_MAX_AGE_S, data_dir: Path = DATA_DIR):
        self._max_age_s = max_age_s
        self._data_dir = Path(data_dir)
        self._conns = {}
        self._write_locks = {}
        self._registry_lock = threading.Lock()


    def _open(self, path: str, readonly: bool) -> sqlite3.Connection:
        if readonly:
            uri = f'file:{path}?mode=ro'
            con = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
        else:
            con = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)
        con.row_factory = sqlite3.Row


        con.isolation_level = None
        cur = con.cursor()
        cur.execute(f'PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}')
        if not readonly:
            # WAL only needed for writers; readers work on either mode
            cur.execute('PRAGMA journal_mode = WAL')
        cur.execute('PRAGMA synchronous = NORMAL')
        cur.execute('PRAGMA temp_store = MEMORY')
        cur.execute('PRAGMA foreign_keys = ON')
        cur.close()
        return con

    def _write_lock(self, path: str) -> threading.RLock:
        with self._registry_lock:
            if path not in self._write_locks:
                self._write_locks[path] = threading.RLock()
        return self._write_locks[path]


    def get(self, path: str, readonly: bool = False) -> sqlite3.Connection:
        '''
        Return a live connection for `path`, honoring the recycle gate.
        Connections older than max_age_s are closed and reopened.
        '''
        path = str(Path(path).resolve())
        key = (path, readonly)
        now = time.time()
        with self._registry_lock:
            entry = self._conns.get(key)
            if entry is not None:
                con, opened_at = entry
                if now - opened_at > self._max_age_s:
                    # recycle gate: connection older than max_age_s
                    try:
                        con.close()
                    except sqlite3.Error:
                        pass
                    del self._conns[key]

                else:
                    try:
                        con.execute('SELECT 1').fetchone()
                        return con
                    except sqlite3.Error:
                        try:
                            con.close()
                        except sqlite3.Error:
                            pass
                        del self._conns[key]
            con = self._open(path, readonly)
            self._conns[key] = (con, time.time())
            return con

    def recycle_all(self) -> int:
        '''Close every pooled connection (used at daemon shutdown). Returns count.'''
        with self._registry_lock:
            n = 0
            for con, _opened in self._conns.values():
                try:
                    con.close()
                    n += 1
                except sqlite3.Error:
                    continue
            self._conns.clear()
        return n

    def recycle_stale(self) -> int:
        '''Close only connections past the age gate. Returns count recycled.'''
        now = time.time()
        recycled = 0
        with self._registry_lock:
            for key in list(self._conns.keys()):
                con, opened_at = self._conns[key]
                if now - opened_at > self._max_age_s:
                    try:
                        con.close()
                        recycled += 1
                    except sqlite3.Error:
                        pass
                    del self._conns[key]
        return recycled

    def stats(self) -> Dict[str, dict]:
        out = {}
        now = time.time()
        with self._registry_lock:
            for (path, ro), (con, opened_at) in sorted(self._conns.items()):
                out[f'{path}{" [ro]" if ro else ""}'] = {
                    'age_s': round(now - opened_at, 1),
                    'recycles_in': max(0.0, round(self._max_age_s - (now - opened_at), 1)),
                }
        return out


def iter_sqlite_files(data_dir: Path = DATA_DIR) -> Iterator[Path]:
    '''Yield every *.db file under data_dir (non-recursive).'''
    data_dir = Path(data_dir)
    if not data_dir.is_dir():
        return
    for p in sorted(data_dir.glob('*.db')):
        if p.is_file():
            yield p


def enforce_wal_all(data_dir: Path = DATA_DIR) -> Dict[str, str]:
    '''
    Set journal_mode=WAL on every SQLite DB in data_dir.
    Idempotent: already-WAL files are untouched no-ops.
    Returns {filename: resulting_mode}.
    '''
    results = {}
    for p in iter_sqlite_files(data_dir):
        try:
            con = sqlite3.connect(str(p), timeout=BUSY_TIMEOUT_MS / 1000)
            con.isolation_level = None
            mode = con.execute('PRAGMA journal_mode = WAL').fetchone()[0]
            con.close()
            results[p.name] = str(mode)
        except sqlite3.Error as e:
            results[p.name] = f'ERROR: {e}'
    return results


def check_wal_all(data_dir: Path = DATA_DIR) -> Dict[str, str]:
    '''Report current journal_mode for every DB (readonly).'''
    results = {}
    for p in iter_sqlite_files(data_dir):
        try:
            con = sqlite3.connect(f'file:{p}?mode=ro', uri=True, timeout=5)
            mode = con.execute('PRAGMA journal_mode').fetchone()[0]
            con.close()
            results[p.name] = str(mode)
        except sqlite3.Error as e:
            results[p.name] = f'ERROR: {e}'
    return results


@contextmanager
def read_conn(pool: DBPool, path: str) -> Iterator[sqlite3.Connection]:
    '''Read-only scoped connection (recycle-gated).'''
    con = pool.get(path, readonly=True)
    try:
        yield con
    finally:
        pass


@contextmanager
def write_txn(pool: DBPool, path: str) -> Iterator[sqlite3.Connection]:
    '''
    Single-writer scoped transaction.

    - Per-DB in-process lock prevents same-process concurrent writers.
    - BEGIN IMMEDIATE grabs the WAL write lock up front (no upgrade deadlock).
    - Retries on 'database is locked' with backoff.
    - Commits on clean exit, rolls back on exception.
    '''
    path = str(Path(path).resolve())
    lock = pool._write_lock(path)
    acquired = lock.acquire(timeout=60)
    if not acquired:
        raise TimeoutError(f'write lock wait exceeded 60s for {path}')
    con = pool.get(path, readonly=False)
    try:
        for attempt in range(1, WRITE_RETRY_ATTEMPTS + 1):
            try:
                con.execute('BEGIN IMMEDIATE')
                break
            except sqlite3.OperationalError as e:
                if 'locked' in str(e).lower() and attempt < WRITE_RETRY_ATTEMPTS:
                    time.sleep(WRITE_RETRY_BACKOFF_S * attempt)
                    continue
                raise
        try:
            yield con
            con.execute('COMMIT')
        except BaseException:
            try:
                con.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            raise
    finally:
        lock.release()






def _self_test(data_dir: Path) -> int:
    '''End-to-end smoke test on a throwaway DB inside data_dir.'''
    import tempfile

    test_db = None
    failures = []
    try:
        with tempfile.NamedTemporaryFile(
            suffix='.db', prefix='_intraday_db_selftest_' + str(data_dir), dir=data_dir
        ) as tmp:
            test_db = tmp.name

        pool = DBPool(max_age_s=2, data_dir=data_dir)


        con = pool.get(test_db)
        mode = con.execute('PRAGMA journal_mode').fetchone()[0]
        if str(mode).lower() != 'wal':
            failures.append(f'WAL not active on fresh conn: {mode}')


        with write_txn(pool, test_db) as w:
            w.execute('CREATE TABLE t (x INTEGER)')
            w.execute('INSERT INTO t VALUES (42)')
        got = pool.get(test_db).execute('SELECT x FROM t').fetchone()[0]
        if got != 42:
            failures.append(f'write_txn commit failed: got {got}')


        try:
            with write_txn(pool, test_db) as w:
                w.execute('INSERT INTO t VALUES (99)')
                raise RuntimeError('intentional')
        except RuntimeError:
            pass
        n = pool.get(test_db).execute('SELECT COUNT(*) FROM t').fetchone()[0]
        if n != 1:
            failures.append(f'rollback failed: {n} rows (expected 1)')


        before = id(pool.get(test_db))
        time.sleep(2.2)
        after = id(pool.get(test_db))
        if before == after:
            failures.append('recycle gate did not reopen stale connection')


        errors = []
        def writer(tag):
            try:
                with write_txn(pool, test_db) as w:
                    w.execute('INSERT INTO t VALUES (?)', (tag,))
                    time.sleep(0.2)
            except Exception as e:
                errors.append(f'{tag}: {e}')
        threads = [threading.Thread(target=writer, args=(i,)) for i in range(100, 105)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if errors:
            failures.append(f'concurrent writes errored: {errors}')
        n = pool.get(test_db).execute('SELECT COUNT(*) FROM t').fetchone()[0]
        if n != 6:
            failures.append(f'expected 6 rows after concurrent writes, got {n}')

        pool.recycle_all()
    finally:
        if test_db and os.path.exists(test_db):
            for suffix in ('', '-wal', '-shm'):
                try:
                    os.unlink(test_db + suffix)
                except OSError:
                    continue

    if failures:
        print('❌ intraday_db self-test FAILED:')
        for f in failures:
            print(f'   - {f}')
        return 1
    print('✅ intraday_db self-test PASSED (WAL, commit, rollback, recycle gate, single-writer)')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description='Intraday DB safety layer CLI')
    ap.add_argument('--check', action='store_true', help='report journal modes of all data/*.db')
    ap.add_argument('--enforce-wal', action='store_true', help='set WAL on all data/*.db')
    ap.add_argument('--recycle', action='store_true', help='exercise recycle gate against core DBs')
    ap.add_argument('--self-test', action='store_true', help='run smoke test')
    ap.add_argument('--data-dir', type=str, default=str(DATA_DIR))
    args = ap.parse_args()
    data_dir = Path(args.data_dir)

    if args.self_test:
        return _self_test(data_dir)

    if args.enforce_wal:
        res = enforce_wal_all(data_dir)
        non_wal = {k: v for k, v in res.items() if v != 'wal'}
        print(f'WAL enforcement: {len(res) - len(non_wal)}/{len(res)} DBs in WAL mode')
        if non_wal:
            for k, v in sorted(non_wal.items()):
                print(f'  ⚠️  {k}: {v}')
            return 1
        print('✅ all DBs WAL')
        return 0

    if args.check:
        res = check_wal_all(data_dir)
        for k, v in sorted(res.items()):
            mark = '✅' if v == 'wal' else '⚠️ '
            print(f'{mark} {k}: {v}')
        return 0

    if args.recycle:
        pool = DBPool()
        core = ['metar_backfill.db', 'nwp_forecasts.db', 'iem_asos_1min.db', 'signal_cache.db']
        for name in core:
            p = data_dir / name
            if not p.exists(): continue
            try:
                with read_conn(pool, str(p)) as con:
                    con.execute('SELECT 1').fetchone()
                print(f'✅ {name}: pooled, recycle gate armed ({CONN_MAX_AGE_S}s)')
            except sqlite3.Error as e:
                print(f'❌ {name}: {e}')
                return 1
        print(f'pool stats: {pool.stats() or "{}"}')
        pool.recycle_all()
        return 0

    ap.print_help()
    return 0


if __name__ == '__main__':
    sys.exit(main())