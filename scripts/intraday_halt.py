'''
Intraday Halt Control (v1.0 — 2026-09-04)

Operator CLI for the intraday daemon halt file (Alpha workstream A.4).

Contract:
  - `--halt REASON`  → create /tmp/intraday-halt. The daemon finishes its
    current cycle/tick, shuts down gracefully, clears the halt file, and
    writes /tmp/intraday-halt-marker.json so the watchdog does not
    resurrect it.
  - `--status`       → report halt file / marker / daemon PID state.
  - `--resume`       → remove halt file AND the marker; daemon may start again
    (next watchdog pass or `systemctl start intraday-daemon`).

Emergency stop WITHOUT kill -9. The daemon polls the halt file every tick.

Note: this is the *daemon* halt file (/tmp/intraday-halt), separate from the
legacy maintenance gate (/tmp/halt_weather_engine) used by check_halt.py.

Usage:
    python3 scripts/intraday_halt.py --halt "deploying calibration update"
    python3 scripts/intraday_halt.py --status
    python3 scripts/intraday_halt.py --resume
'''


import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

HALT_FILE = Path('/tmp/intraday-halt')
HALT_MARKER = Path('/tmp/intraday-halt-marker.json')
PID_FILE = Path('/tmp/intraday-daemon.pid')


def _daemon_pid():
    try:
        return int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return 0


def _daemon_alive(pid):
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return 'intraday_daemon' in (Path('/proc') / str(pid) / 'cmdline').read_bytes().decode(errors='replace')
    except (OSError, ValueError):
        return False


def halt(reason):
    if HALT_FILE.exists():
        print(f'ℹ️  Halt file already present: {HALT_FILE}')
        try:
            print(f'   previous reason: {HALT_FILE.read_text().strip()!r}')
        except OSError:
            pass
    try:
        HALT_FILE.write_text(
            'intraday_daemon halt\n'
            f'reason: {reason}\n'
            f'halted_at: {datetime.now(timezone.utc).isoformat()}\n'
            f'by_pid: {os.getpid()}\n'
        )
    except OSError as e:
        print(f'❌ Could not write halt file: {e}')
        return 2
    print(f'⛔ HALT requested: {reason}')
    print(f"   daemon ({_daemon_pid() or 'not running'}) will finish its current cycle and stop gracefully")
    print(f'   watchdog is suppressed by {HALT_MARKER} until --resume')
    return 0


def resume():
    removed = []
    for f in (HALT_FILE, HALT_MARKER):
        try:
            if f.exists():
                f.unlink()
                removed.append(f.name)
        except OSError as e:
            print(f'❌ Could not remove {f}: {e}')
            return 2
    if removed:
        print(f"✅ RESUMED — cleared: {', '.join(removed)}")
        print('   daemon can start again (watchdog pass or `systemctl start intraday-daemon`)')
    else:
        print('ℹ️  Nothing to clear — no halt file or marker present')
    return 0


def status():
    halted = HALT_FILE.exists()
    pid = _daemon_pid()
    alive = _daemon_alive(pid)
    marker = None
    if HALT_MARKER.exists():
        try:
            marker = json.loads(HALT_MARKER.read_text())
        except (OSError, json.JSONDecodeError):
            marker = {'raw': 'unreadable'}

    print('── Intraday daemon halt status ──────────────────────────────')
    print(f"  halt file   : {'⛔ PRESENT' if halted else '✅ absent'}"
          + (f" ({HALT_FILE.read_text().strip().splitlines()[1] if halted else ''})" if halted else ''))
    print(f"  daemon pid  : {pid if pid else '—'} ({'alive' if alive else 'not running'})")
    print(f"  halt marker : {json.dumps(marker, indent=2) if marker else '✅ absent'}")
    if halted and alive:
        print('  → daemon will stop after its current cycle; use --status again in a minute')
    elif halted and not alive:
        print('  → daemon is down and watchdog-suppressed; --resume to allow start')
    elif not halted and alive:
        print('  → daemon running normally')
    else:
        print('  → daemon fully down; start via systemd unit or watchdog')
    return 0


def main():
    ap = argparse.ArgumentParser(description='Intraday daemon halt control (emergency stop without kill -9)')
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument('--halt', metavar='REASON', help='create halt file (daemon stops gracefully)')
    group.add_argument('--resume', action='store_true', help='remove halt file + watchdog marker')
    group.add_argument('--status', action='store_true', help='show halt/daemon status')
    args = ap.parse_args()

    if args.halt is not None:
        return halt(args.halt)
    if args.resume:
        return resume()
    return status()


if __name__ == '__main__':
    sys.exit(main())