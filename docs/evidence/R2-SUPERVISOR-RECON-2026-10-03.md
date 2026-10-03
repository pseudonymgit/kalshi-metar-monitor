# R2 — Dual-supervisor consolidation: RECON (pre-implementation)

**Date:** 2026-10-03 (Ray Donovan) · **Status:** recon only — implementation HELD
**Repo:** prototypes/weather-engine-source/

## Why held
Donna's spec is conditional: "Gilfoyle is designing the decision. Once he delivers,
implement." As of 2026-10-03 10:40 UTC Gilfoyle had **not** filed a decision doc; he is
mid-investigation in session `agent:gilfoyle:dashboard:7e8296b6-…` and his draft position
is **blocked on a host check that cannot be resolved from inside the container**. Demoting
the container cron before confirming a working host supervisor risks a daemon outage.
No implementation performed.

## Race confirmed (E4 evidence)
06:16:57 daemon start self-exited: `Daemon already running (pid 830885 …) — exiting`.
Current daemon (831493) has **PPID 7** (container init) → launched via the gateway/cron
path. `scripts/daemon_watchdog.sh` does NOT pre-write the PID file (WS-N fix) and uses
`.venv-gefs`. So the 06:16:57 foreign start did not come from the container watchdog.

## Supervisor inventory
| Supervisor | Location | Cadence | Restart-capable? | failureAlert |
|---|---|---|---|---|
| `WE-daemon-watchdog` (`5ac14c0d`) | container cron | `*/5` (drift) | yes (`daemon_watchdog.sh`) | **none configured** (lastDeliveryStatus `not-requested`) |
| `install-daemon-supervisor.sh` | HOST (systemd timer or host cron) | 1–5 min via `docker exec` | yes | n/a |

## The installer's host unit has two defects (would fight the container watchdog)
`scripts/install-daemon-supervisor.sh` systemd `ExecStart`:
1. **pre-writes `echo $! > /tmp/intraday-daemon.pid`** — exactly what WS-N (2026-09-27)
   removed from the container watchdog, because it makes the daemon's own `_check_pid()`
   see itself and `sys.exit(1)` ("already running"). Root-cause of the E4 race.
2. **bare `python3 scripts/intraday_daemon.py`** — not `.venv-gefs/bin/python`; bare
   python3 lacks numpy/scipy, a known silent-restart failure.

## Host reachability from container
- No docker CLI, no SSH keys, no host filesystem (`/host/openclaw-next` = OpenClaw source repo only).
- Docker socket reachable via `curl --unix-socket /var/run/docker.sock` (Engine 29.3.0) — could
  inspect/exec containers, but host **systemd/crontab** remains unverifiable from here.
- `/etc/cron.d/` inside container = base image only (no WE entry visible).

## Recommendation (matches Gilfoyle's draft direction)
Keep **host systemd timer as supervisor of record** ONLY IF host verification confirms it is
installed AND the unit is fixed (remove PID pre-write; use `.venv-gefs`). Then demote the
container cron to read-only liveness (exit 0 alive / exit 1 dead, no restart) and attach
`failureAlert` to the surviving supervisor. Pending host owner check:
`systemctl status weather-engine-daemon-watchdog.timer` (host).
