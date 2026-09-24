'''
Intraday Daemon Watchdog (v1.0 — 2026-09-04)

L0 infrastructure watchdog (Alpha workstream A.2). Designed for a 5-minute cron.

Modes:
  watch (default)   — if the daemon is dead and not halt-suppressed, restart it.
  restart --graceful — graceful daily restart: SIGTERM the daemon, wait for a
                       clean exit, then start it again. Used by the 6AM ET cron.

Restart order (first available wins):
  1. systemctl start intraday-daemon        (prod host, systemd init)
  2. supervisorctl start intraday-daemon    (container with supervisord)
  3. nohup spawn + PID file                 (raw container fallback)

Restart budget: maximum 3 restarts per 10-minute window, tracked in
/tmp/intraday-daemon-restarts.jsonl (applies to paths 2 and 3; systemd enforces
its own StartLimitBurst=3 / StartLimitIntervalSec=600 in the unit file).

Suppression:
  - /tmp/intraday-halt-marker.json present → operator-ordered stop, never restart.
  - /tmp/intraday-halt present → daemon shutting down or refused start; do not
    resurrect until the halt file is cleared (daemon clears it on clean exit).

No trading logic. No AI/ML. Cron/infra only.

Usage:
    python3 scripts/intraday_watchdog.py                  # watch mode (5-min cron)
    python3 scripts/intraday_watchdog.py restart --graceful
    python3 scripts/intraday_watchdog.py status

'''

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
PYTHON = sys.executable or '/usr/bin/python3'

PID_FILE = Path('/tmp/intraday-daemon.pid')
HALT_FILE = Path('/tmp/intraday-halt')
HALT_MARKER = Path('/tmp/intraday-halt-marker.json')
RESTART_LEDGER = Path('/tmp/intraday-daemon-restarts.jsonl')
DAEMON_LOG = REPO_ROOT / 'logs' / 'intraday_daemon.log'

DAEMON_SCRIPT = SCRIPT_DIR / 'intraday_daemon.py'
SYSTEMD_UNIT = 'intraday-daemon'
SUPERVISOR_PROGRAM = 'intraday-daemon'

RESTART_WINDOW_S = 600
RESTART_MAX_IN_WINDOW = 3
GRACEFUL_STOP_TIMEOUT_S = 180


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    print(f'{ts} [watchdog] {msg}', flush=True)



def read_pid() -> Optional[int]:
    try:
        return int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None


def pid_alive_and_ours(pid: Optional[int]) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        cmdline = (Path('/proc') / str(pid) / 'cmdline').read_bytes().decode(errors='replace')
        return 'intraday_daemon' in cmdline
    except OSError:
        return False


def daemon_running() -> Tuple[bool, Optional[int]]:
    pid = read_pid()
    if pid_alive_and_ours(pid):
        return (True, pid)

    # fallback: pgrep sweep (covers stale/missing PID file)
    try:
        out = subprocess.run(
            ['pgrep', '-f', 'intraday_daemon.py'],
            capture_output=True, text=True, timeout=10)

        pids = [int(p) for p in out.stdout.split() if p.strip().isdigit()]
        pids = [p for p in pids if p != os.getpid()]
        if pids:
            return (True, pids[0])
    except (subprocess.SubprocessError, ValueError, OSError):
        pass
    return (False, None)



def _prune_and_count_restarts() -> int:
    '''Count restarts within the window; prune older ledger lines.'''
    now = time.time()
    kept = []
    try:
        for line in RESTART_LEDGER.read_text().splitlines():
            try:
                ts = float(json.loads(line)['ts'])
                if now - ts <= RESTART_WINDOW_S:
                    kept.append(line)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
    except OSError:
        pass
    try:
        RESTART_LEDGER.write_text(''.join(l + '\n' for l in kept))
    except OSError:
        pass
    return len(kept)


def _record_restart(method: str) -> None:
    try:
        with RESTART_LEDGER.open('a') as f:
            f.write(json.dumps({'ts': time.time(),
                                'method': method,
                                'at': datetime.now(timezone.utc).isoformat()}) + '\n')
    except OSError:
        pass


def restart_budget_exhausted() -> bool:
    n = _prune_and_count_restarts()
    if n >= RESTART_MAX_IN_WINDOW:
        log(f'🚨 ALERT: restart budget exhausted ({n}/{RESTART_MAX_IN_WINDOW} '
            f'in last {RESTART_WINDOW_S // 60}min) — NOT restarting; manual intervention required')
        return True
    return False



def _try_systemd_start() -> bool:
    '''systemctl start if systemd is functional in this environment.'''
    if not Path('/run/systemd/system').exists() and not Path('/proc/1/comm').exists():
        try:
            if 'systemd' not in Path('/proc/1/comm').read_text():
                return False
        except OSError:
            return False
    try:
        r = subprocess.run(['systemctl', 'start', SYSTEMD_UNIT],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            log(f'daemon started via systemctl start {SYSTEMD_UNIT}')
            return True
        log(f'systemctl start failed rc={r.returncode}: {r.stderr.strip()[:200]}')
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        log(f'systemctl unavailable: {e}')
    return False


def _try_supervisor_start() -> bool:
    try:
        r = subprocess.run(['supervisorctl', 'start', SUPERVISOR_PROGRAM],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            log(f'daemon started via supervisorctl start {SUPERVISOR_PROGRAM}')
            return True
        log(f'supervisorctl start failed rc={r.returncode}: {r.stderr.strip()[:200]}')
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        log(f'supervisorctl unavailable: {e}')
    return False


def _spawn_direct() -> bool:
    '''Raw fallback: nohup-style detached spawn with the daemon managing its own PID file.'''
    try:
        DAEMON_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DAEMON_LOG.open('a') as log_fh:
            subprocess.Popen(
                [PYTHON, str(DAEMON_SCRIPT), '--interval-min', '60'],
                cwd=str(REPO_ROOT),
                             stdout=log_fh,
                             stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL,
                             start_new_session=True,
                             close_fds=True)

        log_fh.close()

        for _ in range(10):
            time.sleep(1)
            running, pid = daemon_running()
            if running:
                log(f'daemon spawned directly (pid {pid}) — raw-container fallback')
                return True
        log('🚨 ALERT: direct spawn did not produce a running daemon within 10s')
        return False
    except OSError as e:
        log(f'🚨 ALERT: direct spawn failed: {e}')
        return False


def start_daemon() -> bool:
    for method in (_try_systemd_start, _try_supervisor_start, _spawn_direct):
        if method():
            return True
    log('🚨 ALERT: all start mechanisms failed')
    return False



def graceful_stop(pid: int) -> bool:
    log(f'sending SIGTERM to daemon pid {pid} (graceful stop)')
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        log(f'SIGTERM failed: {e}')
        return False
    deadline = time.time() + GRACEFUL_STOP_TIMEOUT_S
    while time.time() < deadline:
        if not pid_alive_and_ours(pid):
            log(f'daemon exited cleanly within {GRACEFUL_STOP_TIMEOUT_S}s')
            return True
        time.sleep(5)
    log(f'🚨 ALERT: daemon pid {pid} did not exit within {GRACEFUL_STOP_TIMEOUT_S}s of SIGTERM')
    return False



def mode_watch() -> int:
    running, pid = daemon_running()


    if HALT_MARKER.exists():
        log(f'halt marker present — operator-ordered stop; not restarting (daemon running: '
            f'{running})')
        return 0
    if HALT_FILE.exists():
        log('halt file present — daemon stopping or start-refused; not resurrecting')
        return 0

    if running:
        log(f'daemon healthy (pid {pid})')
        return 0

    log(f'daemon DOWN (pid file: {read_pid()})')
    if restart_budget_exhausted():
        return 1
    if start_daemon():
        _record_restart('watchdog')
        return 0
    return 1


def mode_restart() -> int:
    '''Graceful daily restart (6AM ET cron). Acts only during the 06:00 ET hour
    so the double-fired UTC cron (10:00 & 11:00 for EDT/EST) hits exactly once.'''
    try:
        from zoneinfo import ZoneInfo
        et_now = datetime.now(ZoneInfo('America/New_York'))
    except Exception:
        et_now = None

    if et_now is not None:
        if et_now.hour != 6:
            log(f"ET local time is {et_now.strftime('%H:%M')} — not the 6AM ET restart window, no-op")
            return 0
        log(f"daily restart at {et_now.strftime('%Y-%m-%d %H:%M %Z')}")

    running, pid = daemon_running()
    if running and pid:
        if not graceful_stop(pid):

            return 1
        _record_restart('daily-restart-stop')

    log('daemon not running — proceeding to start')

    if restart_budget_exhausted():
        return 1
    if start_daemon():
        _record_restart('daily-restart')
        log('daily restart complete')
        return 0
    return 1


def mode_status() -> int:
    running, pid = daemon_running()
    budget_used = _prune_and_count_restarts()
    print(f'daemon running : {running} (pid {pid})')
    print(f'halt file      : {HALT_FILE.exists()}')
    print(f'halt marker    : {HALT_MARKER.exists()}')
    print(f'restart budget : {budget_used}/{RESTART_MAX_IN_WINDOW} in last {RESTART_WINDOW_S // 60}min')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description='Intraday daemon watchdog / restart control')
    sub = ap.add_subparsers(dest='mode')
    sub.add_parser('watch', help='check & restart if dead (default)')
    p_restart = sub.add_parser('restart', help='graceful restart (6AM ET daily job)')
    p_restart.add_argument('--graceful', action='store_true', help='SIGTERM before restart')
    sub.add_parser('status', help='print status')
    args = ap.parse_args()

    if args.mode == 'restart':
        return mode_restart()
    if args.mode == 'status':
        return mode_status()
    return mode_watch()


if __name__ == '__main__':
    sys.exit(main())