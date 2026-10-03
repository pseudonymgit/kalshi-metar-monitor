# Dual-Supervisor Consolidation — Design & Decision (R2 close)

**Date:** 2026-10-03 · **Author:** Bertram Gilfoyle (architecture) · **Implementer:** Ray Donovan
**Repo:** `prototypes/weather-engine-source/` · **Roadmap:** R2 (Runtime durability)
**Trigger:** Dan-approved spec via Donna — the WE intraday daemon has two supervisors racing.

---

## 0. TL;DR

1. **Host verification is NOT possible from inside the container** — no `docker` CLI, no
   `systemctl`, no `crontab`, no `journalctl`, no host exec. The host check is **blocked on Dan/the host**
   (exact commands in §4).
2. **The host installer is stale AND buggy.** `scripts/install-daemon-supervisor.sh` restarts the
   daemon in a way that (a) **cannot find numpy/scipy** (uses bare `python3`, not `.venv-gefs/bin/python`)
   and (b) **pre-writes `$!` into the PID file**, which the daemon's `_check_pid()` reads back as
   "already running" and self-exits. If Option A was installed as-is, **the host supervisor is a no-op.**
3. **Therefore the requested plan is inverted.** Gilfoyle's original C3 recommendation (make host
   systemd primary) assumed a *working* host supervisor. It is not working. **Until the installer is
   fixed, the container cron is the only functional supervisor and must stay primary.**
4. **Recommended single chain of record (now):**
   `Docker (unless-stopped) → gateway → WE-daemon-watchdog cron (primary, restart authority) →
   daemon_watchdog.sh → daemon`, with a **demoted host systemd timer** as cold-start-only backup
   *after* the installer is fixed.

---

## 1. Problem statement

Two supervisors target the same daemon concurrently:

| Supervisor | Where | Trigger | Restart authority | Restart pattern |
|---|---|---|---|---|
| `WE-daemon-watchdog` cron (`5ac14c0d`) | Container (gateway) | `*/5 min` | **Yes** | `daemon_watchdog.sh` — `.venv-gefs/bin/python`, no PID pre-write, rate-limited |
| `weather-engine-daemon-watchdog` | Host (systemd timer *or* host cron) | 1–5 min | **Yes** (intended) | inline `docker exec` — bare `python3`, pre-writes `$!` |

Both race on every restart; the loser no-ops. The design assumption "one chain is the authority"
is broken. This doc closes R2 by naming one.

---

## 2. Host verification — BLOCKED from container (evidence)

Attempted, all unavailable inside `82f8309e34ce`:

| Probe | Result |
|---|---|
| `which docker` | **not found** (`/var/run/docker.sock` exists but `srw-rw---- root:985` — not the container's gid) |
| `which systemctl` / `journalctl` | **not found** |
| `crontab -l` | **not found** |
| `/var/spool/cron` | **does not exist** |
| `/etc/systemd/system/` | no weather unit visible |
| `/host/openclaw-next` | = the OpenClaw **source repo**, not the host root — no host exec surface |

**Conclusion:** verification of Option A vs Option B must be run **on the host** (§4). Only Dan
(or a host-capable node) can produce that receipt. This is not a blocker to the decision below —
the decision is robust either way (§3).

---

## 3. Decision

### 3.1 Supervisor of record: **container cron `WE-daemon-watchdog` (primary), for now**

Reason — it is the **only supervisor with a correct restart path**:

| Property | Container cron (`daemon_watchdog.sh`) | Host installer |
|---|---|---|
| Interpreter | `.venv-gefs/bin/python` (has numpy+scipy) | bare `python3` (**lacks numpy/scipy**) |
| PID pre-write | **No** (WS-N fix 2026-09-27) — daemon owns its PID | **Yes** — `echo $! > pid` → daemon self-exits "already running" |
| Rate limit | Yes (`6/3600s` → STUCK, exit 1) | None |
| Failure surfacing | `failureAlert` can be attached (cron) | systemd journal / host MAILTO only |
| Blast radius | dies with gateway (container SPOF) | survives container death (real SPOF) |

**The host supervisor wins on SPOF coverage; the container cron wins on correctness.** Correctness
wins today: a supervisor that cannot restart the daemon is worse than none, because it masks the
gap (it looks supervised but isn't).

### 3.2 Target end-state (after Ray fixes the installer)

`Docker (unless-stopped) → weather-engine-daemon-watchdog.timer (host, primary) →
docker exec → daemon_watchdog.sh (in container) → daemon`

i.e. move the host systemd timer to primary **only** once the host unit calls the **container's
own `daemon_watchdog.sh`** (which carries the correct venv + PID handling + rate limiter), rather
than inlining a broken restart. Then the container cron is **demoted** to a pure liveness/observability
check with **no restart authority**.

### 3.3 Demotion mechanics (pick one)

- **(D1) Remove** `WE-daemon-watchdog` cron. Simplest; loses in-container liveness signal.
- **(D2) Convert to pure liveness check** — runs `daemon_watchdog.sh --check-only` (new flag) that
  reports OK/DEAD + emits a failureAlert but never restarts. **Recommended** — preserves the
  in-container signal and the alert path without a second authority.

### 3.4 `failureAlert` wiring (required)

Attach to whichever cron survives:
- If **container cron stays primary** → attach `failureAlert` to `WE-daemon-watchdog` now (its
  `delivery.mode` is currently `none`; STUCK currently goes only to `logs/daemon_watchdog.log`).
- If **host systemd becomes primary** → systemd can't alert to Discord; add a thin host→container
  notifier, **or** keep the demoted (D2) container liveness cron as the alert carrier. The D2 option
  is cleaner: **the container cron is the alert surface, the host timer is the restart authority.**

---

## 4. Host verification — exact commands (Dan / host node)

```bash
# Option A (systemd) ?
systemctl status weather-engine-daemon-watchdog.timer --no-pager
systemctl is-enabled weather-engine-daemon-watchdog.timer
journalctl -u weather-engine-daemon-watchdog.service --since '24 hours ago' --no-pager | tail -20

# Option B (host cron) ?
crontab -l | grep -i intraday_daemon

# Either way — is the host supervisor actually restarting the daemon, or no-oping?
docker exec openclaw-next-runtime-openclaw-gateway-1 sh -lc \
  'tail -5 /home/node/.openclaw/workspace/prototypes/weather-engine-source/logs/daemon_watchdog.log; \
   cat /tmp/intraday-daemon.pid; ps -p $(cat /tmp/intraday-daemon.pid) -o pid,lstart,cmd'
```

Return: which option exists, its last-fire, and whether the daemon PID it claims matches the live
`WE-daemon-watchdog` restart. **If nothing is installed → the race already doesn't exist; the
container cron is unopposed and §3.1 stands.**

---

## 5. Live evidence supporting this decision (2026-10-03)

- Container cron `WE-daemon-watchdog`: `lastRunStatus=ok`, `consecutiveErrors=0`, ran 10:40:00 UTC.
- `logs/daemon_watchdog.log`: every 5 min `[OK] Daemon running (pid=831493)` — **single PID, no fight**.
- Daemon PID 831493 started **2026-10-03 06:19:59 UTC**, PPID=7 (gateway tini).
- Last real `[RESTART]` = 2026-10-03 06:20 by the container cron; prior `[STUCK]` = 2026-09-28 13:57
  (rate limiter did its job). **No evidence of a competing restart source.**
- Interpretation: the container cron has been the de-facto sole supervisor. If a host supervisor
  exists, it is (today) a no-op — consistent with the installer bug in §3.1, not with "racing".

---

## 6. Deliverables / handoff

| # | Owner | Action |
|---|---|---|
| 1 | **Dan / host** | Run §4 commands; return which option is installed (or none). |
| 2 | **Ray** | Fix `install-daemon-supervisor.sh`: use `.venv-gefs/bin/python`; **do not** pre-write the PID; delegate to `daemon_watchdog.sh`; add restart rate-limit. |
| 3 | **Ray** | Add `--check-only` flag to `daemon_watchdog.sh` (D2 demotion support). |
| 4 | **Ray** | Attach `failureAlert` to the surviving cron (currently `WE-daemon-watchdog`, `delivery.mode=none`). |
| 5 | **Ray/Gilfoyle** | After #2–#4 verified: promote host timer to primary, convert container cron to (D2) liveness-only. Flip §3.2 chain on. |
| 6 | **Gilfoyle** | Update R2 roadmap row + commit this doc. |

**R2 status: NOT closeable yet** — closing requires the §4 host receipt (which supervisor is
installed) + the §3.4 alert wiring. This doc delivers the decision, the target chain, and the
exact host commands; Ray implements #2–#5.

---

## 7. Stop conditions

- No host mutation from the container (none possible / none attempted).
- No cron change made by this doc (design only).
- No daemon/service restart performed.
