#!/usr/bin/env python3
"""
phantom_watch.py — R6 phantom-success prevention, Layer 4 (divergence alert).

Why: a data-write job can exit 0 while doing zero real work (the R6
phantom-success vector). Layer 1 (fail-closed exit codes) makes the job's own
receipt trustworthy, but a job that *reports* success can still be lying — or a
wrapper/scheduler can lie on its behalf. Layer 4 is the independent cross-check:
it compares each command-kind cron job's self-report (exit 0) against an
INDEPENDENT oracle (the data actually in the DB), never trusting the exit code.

Rule:
    phantom :=  job_exit == 0
            AND oracle.max_date did not advance vs baseline
            AND oracle.row_count_delta <= 0

    partial (WARN) : max_date advanced but row_count_delta <= 0
                     (possible overwrite/rollup, not a full phantom)
    OK             : max_date advanced and rows grew (real work)

Alerting (Step 3/4): one alert per source per change-of-state. A source that
stays phantom does not re-alert every cycle; a recovery clears its signature.
The watcher exits 2 to trigger the existing cron failureAlert path. When a
source is phantom for N consecutive checks (default 3, Step 5 / L5), the halt
file is written so the pipeline stops instead of silently logging.

This is an OBSERVER. It does not modify any L1 script, exit code, or writer.
It reuses the freshness monitor's SOURCES registry (same oracles) and the
existing deadman/halt gate.

Exit codes:
  0 = no new phantom alert (healthy, or phantom already reported this state)
  1 = WARN (partial divergence) — reported, not a failure
  2 = PHANTOM-SUCCESS (new/changed state) -> cron failureAlert; and/or L5 halt
"""

import argparse
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Reuse the existing oracle registry + parsers (Step 2: "reuse, don't fork").
from scripts.data_freshness_monitor import SOURCES, _max_raw, _parse_ts  # noqa: E402

DATA_DIR = REPO_ROOT / "data"
BASELINE_FILE = DATA_DIR / ".phantom_baseline.json"
CONSEC_FILE = DATA_DIR / ".phantom_consecutive.json"
SIG_FILE = DATA_DIR / ".phantom_last_sig"
HALT_FILE = DATA_DIR / ".halt"

DEFAULT_N = 3

# Command-kind cron job name -> freshness SOURCES key.
# Only jobs that (a) are command-kind, (b) carry L1 exit semantics, and
# (c) have a registered oracle row. Match is on a case-insensitive substring
# of the cron job name, so a rename with the same stem still maps.
JOB_SOURCE_MAP = [
    ("METAR ingestion", "metar"),
    ("z5-asos-1min", "iem_asos_1min"),
    ("NWP Forecast Collection", "nwp_core"),
    ("Forecast Disagreement", "forecast_disagreement"),
    ("WE-GEFS-daily-backfill", "gefs_archive"),
    ("ecmwf-tigge-daily-update", "tigge_archive"),
    ("WE-era5-archive-weekly", "era5_archive"),
    ("kalshi-settlement-upkeep", "kalshi_settlements"),
    ("WE-GHCN-daily-backfill", "ghcn_settlements"),
    ("WE-weatherapi-daily", "weatherapi"),
]

_SOURCE_BY_KEY = {s["key"]: s for s in SOURCES}


# ─── Oracle read (independent of the job's self-report) ─────────────────────


def oracle_state(src: dict, data_dir: Path = DATA_DIR) -> dict:
    """Read the independent oracle state for one source.

    Returns {max_date, row_count, ok, error}. `ok` is True only when a real
    max_date was read. row_count is best-effort (None if the COUNT fails).
    """
    res = {"max_date": None, "row_count": None, "ok": False, "error": None}
    db = Path(data_dir) / src["db"]
    if not db.exists():
        res["error"] = "MISSING"
        return res
    raw = _max_raw(db, src["table"], src["date_col"], src.get("model"))
    if raw is None:
        res["error"] = "EMPTY"
        return res
    if isinstance(raw, str) and raw.startswith("ERROR:"):
        res["error"] = raw
        return res
    res["max_date"] = str(raw)
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=15)
        where, params = "1=1", []
        if src.get("model"):
            where, params = "model = ?", [src["model"]]
        res["row_count"] = conn.execute(
            f'SELECT COUNT(*) FROM "{src["table"]}" WHERE {where}', params
        ).fetchone()[0]
        conn.close()
    except Exception:  # noqa: BLE001 — row_count is optional
        res["row_count"] = None
    res["ok"] = True
    return res


# ─── Divergence rule (pure) ─────────────────────────────────────────────────


def _advanced(cur, base) -> bool:
    """True iff cur is strictly newer than base."""
    dc, dbb = _parse_ts(cur), _parse_ts(base)
    if isinstance(dc, str) or isinstance(dbb, str):
        return str(cur) > str(base)
    return dc > dbb


def _age_hours(max_date, now) -> float | None:
    ts = _parse_ts(max_date)
    if isinstance(ts, str):
        return None
    return (now - ts).total_seconds() / 3600.0


def evaluate(job_exit_zero, baseline: dict | None, current: dict,
             threshold_h: float | None = None, now=None) -> str:
    """Apply the Layer 4 rule. Returns OK | WARN | PHANTOM | UNKNOWN.

    Cadence-aware: a job that exits 0 while its oracle has not advanced is only
    a *phantom* when the source is also DUE per its freshness threshold
    (cadence_h * (1 + slack)) — i.e. real work should have happened by now.
    A source that is still inside its cadence window (a 6h/24h job checked
    hourly) is OK: no work was due. This reuses the L3 oracle's threshold
    exactly, which is what makes "exit 0 + no advance" a trustworthy signal
    instead of a per-cycle false alarm.
    """
    if not current.get("ok"):
        return "UNKNOWN"
    if not job_exit_zero:
        # The job admitted failure (non-zero exit). That is L1's signal, not
        # a phantom; L4 only catches *false* success.
        return "OK"
    if not baseline:
        # First observation: no anchor to compare against. Establish baseline.
        return "OK"

    base_md = baseline.get("max_date")
    base_rc = baseline.get("row_count")
    cur_rc = current.get("row_count")

    rows_delta = None
    if cur_rc is not None and base_rc is not None:
        rows_delta = cur_rc - base_rc

    if _advanced(current.get("max_date"), base_md):
        # max_date moved. But if rows did not grow, suspect overwrite/rollup.
        if rows_delta is not None and rows_delta <= 0:
            return "WARN"
        return "OK"

    # max_date did NOT advance. If rows grew, real work happened. Otherwise the
    # source is frozen — but only a phantom if it is also past its cadence
    # threshold (i.e. it SHOULD have advanced by now).
    if rows_delta is not None and rows_delta > 0:
        return "OK"
    if threshold_h is not None and now is not None:
        age = _age_hours(current.get("max_date"), now)
        if age is not None and age <= threshold_h:
            return "OK"  # within cadence; no work due yet
    return "PHANTOM"


# ─── Cron job introspection ─────────────────────────────────────────────────


def load_jobs_from_cron() -> list[dict]:
    """Load command-kind cron jobs via `openclaw cron list --json`."""
    try:
        out = subprocess.run(
            ["openclaw", "cron", "list", "--json"],
            capture_output=True, text=True, timeout=60,
        )
        data = json.loads(out.stdout)
    except Exception:  # noqa: BLE001
        return []
    jobs = data if isinstance(data, list) else data.get("jobs", data.get("crons", []))
    return jobs


def job_report(job: dict) -> dict:
    """Self-report for a command job.

    Returns {exit_zero, last_run_ms}. `exit_zero` is None when undeterminable.
    `last_run_ms` is the scheduler's record of when the job last ran (None if
    unknown). The run timestamp is what lets L4 only compare the oracle when
    the job actually executed — otherwise a 6h/24h-cadence job checked hourly
    would look "frozen" on every idle cycle and false-alarm.
    """
    st = job.get("state") or {}
    lrs = st.get("lastRunStatus")
    if lrs is not None:
        exit_zero = lrs == "ok"
    else:
        exit_zero = None
        for e in (st.get("lastDiagnostics") or {}).get("entries", []):
            if "exitCode" in e:
                exit_zero = e["exitCode"] == 0
                break
    return {"exit_zero": exit_zero, "last_run_ms": st.get("lastRunAtMs")}


def map_job_to_source(job_name: str, source_map=None) -> str | None:
    if not job_name:
        return None
    low = job_name.lower()
    for pattern, key in (source_map or JOB_SOURCE_MAP):
        if pattern.lower() in low:
            return key
    return None


# ─── Sidecar state ──────────────────────────────────────────────────────────


def _read_json(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except Exception:  # noqa: BLE001
        return {}


def _write_json(path: Path, obj: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, indent=2, sort_keys=True))


# ─── Orchestration (pure-ish: all paths injectable for tests) ───────────────


def run(sources, jobs, *, data_dir=DATA_DIR, baseline_path=None,
        consec_path=None, sig_path=None, halt_path=None, n=DEFAULT_N,
        now=None, write=True):
    """Evaluate every mapped job/source and return (exit_code, report)."""
    data_dir = Path(data_dir)
    baseline_path = Path(baseline_path or (data_dir / ".phantom_baseline.json"))
    consec_path = Path(consec_path or (data_dir / ".phantom_consecutive.json"))
    sig_path = Path(sig_path or (data_dir / ".phantom_last_sig"))
    halt_path = Path(halt_path or (data_dir / ".halt"))

    baseline = _read_json(baseline_path)
    consec = _read_json(consec_path)
    now = now or datetime.now(timezone.utc)

    # Collapse jobs -> source key. Track the newest run timestamp and whether
    # any job for that source self-reported exit 0.
    exit_by_source: dict[str, bool] = {}
    run_by_source: dict[str, int | None] = {}
    for job in jobs:
        key = map_job_to_source(job.get("name", ""))
        if not key:
            continue
        rep = job_report(job)
        if rep["exit_zero"] is None:
            continue
        # A source is only "claimed success" if at least one of its jobs says 0.
        exit_by_source[key] = exit_by_source.get(key, False) or bool(rep["exit_zero"])
        lr = rep["last_run_ms"]
        if lr is not None:
            run_by_source[key] = max(run_by_source.get(key, 0), lr)

    results = []
    for src in sources:
        key = src["key"]
        if key not in exit_by_source:
            continue
        base = baseline.get(key)
        prev_run = (base or {}).get("last_run_ms")
        cur_run = run_by_source.get(key)
        # Cadence gate: only compare the oracle when the job actually ran since
        # the last observation (or when run times are unknown, e.g. fixtures).
        if base is not None and prev_run is not None and cur_run is not None:
            if cur_run <= prev_run:
                continue
        cur = oracle_state(src, data_dir)
        threshold_h = src["cadence_h"] * (1 + src["slack"])
        status = evaluate(exit_by_source[key], base, cur, threshold_h, now)
        results.append({"key": key, "status": status,
                        "max_date": cur.get("max_date"),
                        "prev_max_date": (base or {}).get("max_date"),
                        "row_count": cur.get("row_count"),
                        "prev_row_count": (base or {}).get("row_count"),
                        "last_run_ms": cur_run,
                        "ok": cur.get("ok"), "error": cur.get("error")})

    # L5: consecutive phantom counter.
    phantoms = [r["key"] for r in results if r["status"] == "PHANTOM"]
    warns = [r["key"] for r in results if r["status"] == "WARN"]
    for r in results:
        if r["status"] == "PHANTOM":
            consec[r["key"]] = int(consec.get(r["key"], 0)) + 1
        else:
            consec[r["key"]] = 0

    # Rate-limit: one alert per source per change-of-state.
    sig = ",".join(sorted(phantoms))
    last_sig = ""
    if sig_path.exists():
        last_sig = sig_path.read_text().strip()
    changed = sig != last_sig

    exit_code = 0
    lines = []
    if phantoms:
        if changed:
            lines.append("PHANTOM-SUCCESS detected:")
            for r in results:
                if r["status"] == "PHANTOM":
                    lines.append(
                        f"  {r['key']} exited 0 but oracle shows no advance "
                        f"(max_date {r['prev_max_date']}->{r['max_date']}, "
                        f"rows {r['prev_row_count']}->{r['row_count']})")
            exit_code = 2
        # else: already reported this exact state -> suppressed
    elif last_sig:
        # recovered: clear signature (report once so the recovery is visible)
        lines.append("PHANTOM-SUCCESS cleared (all previously-phantom sources advanced).")
    if warns:
        lines.append("WARN (partial divergence — max_date advanced, rows flat): "
                     + ", ".join(warns))
        if exit_code == 0:
            exit_code = 1

    # L5 halt gate: any source phantom N consecutive checks -> write halt.
    halted = []
    for r in results:
        if r["status"] == "PHANTOM" and consec.get(r["key"], 0) >= n:
            halted.append((r["key"], consec[r["key"]]))
    if halted and write:
        reason = "; ".join(f"{k} phantom {c}x" for k, c in halted)
        _write_halt(halt_path, reason)

    report = {"checked_at": now.isoformat(), "results": results,
              "phantoms": phantoms, "warns": warns, "halted": halted,
              "exit_code": exit_code, "lines": lines}

    if write:
        # Persist baseline only for sources whose oracle read succeeded.
        for r in results:
            if r["ok"]:
                baseline[r["key"]] = {"max_date": r["max_date"],
                                      "row_count": r["row_count"],
                                      "last_run_ms": r.get("last_run_ms"),
                                      "checked_at": now.isoformat()}
        _write_json(baseline_path, baseline)
        _write_json(consec_path, consec)
        if phantoms:
            sig_path.write_text(sig)
        elif last_sig:
            try:
                sig_path.unlink()
            except FileNotFoundError:
                pass

    return exit_code, report


def _write_halt(halt_path: Path, reason: str) -> None:
    try:
        Path(halt_path).parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        Path(halt_path).write_text(f"HALTED\nTimestamp: {ts}\nReason: {reason}\n")
    except Exception:  # noqa: BLE001
        pass


def main():
    ap = argparse.ArgumentParser(description="R6 phantom-success divergence watch (L4)")
    ap.add_argument("--json", action="store_true", help="emit JSON report")
    ap.add_argument("--now", help="override 'now' (ISO-8601) for testing")
    ap.add_argument("--data-dir", help="override data dir (frozen-fixture tests)")
    ap.add_argument("--n", type=int, default=DEFAULT_N, help="L5 consecutive threshold")
    ap.add_argument("--dry-run", action="store_true", help="evaluate without writing state")
    args = ap.parse_args()

    data_dir = Path(args.data_dir).resolve() if args.data_dir else DATA_DIR
    now = _parse_ts(args.now) if args.now else datetime.now(timezone.utc)
    if isinstance(now, str):
        print(f"bad --now: {args.now}", file=sys.stderr)
        return 2

    jobs = load_jobs_from_cron()
    exit_code, report = run(SOURCES, jobs, data_dir=data_dir, n=args.n,
                            now=now, write=not args.dry_run)

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        if report["lines"]:
            print("\n".join(report["lines"]))
        else:
            print("NO_REPLY")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
