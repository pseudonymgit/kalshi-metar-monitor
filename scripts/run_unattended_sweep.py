#!/usr/bin/env python3
"""Deterministic, unattended gate around the canonical weather sweep.

This wrapper is deliberately boring: without --execute it only performs gates.
It never edits source/configuration, calls AI, or performs network I/O.
"""
from __future__ import annotations
import argparse, hashlib, json, os, platform, shlex, signal, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CANONICAL = [sys.executable, "scripts/run_sweep.py", "--folds", "4", "--n-configs", "20"]
QC_COMMANDS = [
    [sys.executable, "-m", "py_compile", "scripts/run_unattended_sweep.py", "scripts/run_sweep.py", "scripts/sweep/config.py"],
    [sys.executable, "-m", "pytest", "-q", "tests/test_pre_sweep_build.py", "tests/test_qc_check.py"],
    [sys.executable, "-m", "pytest", "-q"],
]
HASH_ROOTS = ("app.py", "core", "scripts", "config")
# Graphify generation/inspection is an independent artifact workflow. It is
# deliberately outside this runner's pre-sweep readiness contract: do not
# hash, dirty-gate, or QC Graphify paths here, and do not delete or mutate
# their outputs. The Graphify cron remains untouched.
PREFLIGHT_EXCLUSION = {
    "workflow": "graphify-generation-and-inspection",
    "reason": "independent Graphify workflow is not a sweep prerequisite",
    "match": "any path component containing graphify (case-insensitive)",
}
# Only these immutable evaluator inputs belong in the execution manifest.  The
# data directory also contains live SQLite sidecars, WALs, logs, and generated
# research artifacts; walking it made preflight both nondeterministic and
# needlessly expensive.
HASH_DATA_FILES = ("data/kalshi_settlements.db", "data/metar_backfill.db")
VOLATILE_SUFFIXES = {".db-shm", ".db-wal", ".pyc"}
EXIT_PASS, EXIT_BLOCKED, EXIT_QC, EXIT_TIMEOUT = 0, 2, 3, 4


def utcnow(): return datetime.now(timezone.utc).isoformat()
def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""): h.update(chunk)
    return h.hexdigest()

def git(*args):
    return subprocess.run(["git", *args], cwd=REPO_ROOT, text=True, capture_output=True, check=False)

def is_graphify_path(path):
    """Return true for independent Graphify generation/inspection artifacts."""
    return any("graphify" in part.lower() for part in Path(path).parts)


def status_lines(ignore=()):
    ignored = {str(Path(p).resolve()) for p in ignore}
    lines = []
    for line in git("status", "--porcelain=v1", "--untracked-files=all").stdout.splitlines():
        path = line[3:].split(" -> ")[-1]
        if is_graphify_path(path):
            continue
        if any(str((REPO_ROOT / path).resolve()).startswith(p + os.sep) for p in ignored):
            continue
        lines.append(line)
    return lines

def allowed_dirty(lines, allow):
    prefixes = tuple(str(Path(x)) for x in allow)
    return [x for x in lines
            if not is_graphify_path(x[3:].split(" -> ")[-1])
            and not any(x[3:].startswith(p) for p in prefixes)]

def source_manifest():
    pairs = []
    for root in HASH_ROOTS:
        base = REPO_ROOT / root
        paths = [base] if base.is_file() else [
            p for p in base.rglob("*")
            if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts
            and not is_graphify_path(p.relative_to(REPO_ROOT))
            and not any(p.name.endswith(suffix) for suffix in VOLATILE_SUFFIXES)
        ]
        pairs.extend((str(p.relative_to(REPO_ROOT)), sha256_file(p)) for p in paths)
    for relative in HASH_DATA_FILES:
        path = REPO_ROOT / relative
        if path.is_file():
            pairs.append((relative, sha256_file(path)))
    return dict(sorted(pairs))

def run_command(cmd, timeout, out=None, err=None):
    started = time.monotonic()
    try:
        cp = subprocess.run(cmd, cwd=REPO_ROOT, stdin=subprocess.DEVNULL, stdout=out, stderr=err, timeout=timeout, check=False, env=os.environ.copy())
        return {"command": cmd, "exit_code": cp.returncode, "timed_out": False, "seconds": round(time.monotonic()-started, 3)}
    except subprocess.TimeoutExpired:
        return {"command": cmd, "exit_code": None, "timed_out": True, "seconds": round(time.monotonic()-started, 3)}

def preflight(args, run_dir):
    before = status_lines()
    checks = {"repo_root": str(REPO_ROOT), "git_head": git("rev-parse", "HEAD").stdout.strip(), "dirty_before": before,
              "dirty_unallowlisted": allowed_dirty(before, args.allow_dirty_path), "source_hashes": source_manifest(),
              "python": sys.version, "platform": platform.platform(), "canonical_command": CANONICAL,
              "network": "not used by runner; child must be independently approved",
              "preflight_exclusions": [PREFLIGHT_EXCLUSION]}
    checks["clean_gate"] = not checks["dirty_unallowlisted"]
    checks["canonical_exists"] = (REPO_ROOT / "scripts/run_sweep.py").is_file()
    checks["config_exists"] = (REPO_ROOT / "config/sweep-optimal.json").is_file()
    checks["data_exists"] = all((REPO_ROOT / p).is_file() for p in ("data/kalshi_settlements.db", "data/metar_backfill.db"))
    try:
        sys.path.insert(0, str(REPO_ROOT))
        from scripts.sweep.config import STATIONS
        from core.station_registry import get_all_stations
        checks["stations"] = {"configured": list(STATIONS), "registry_count": len(get_all_stations()), "nonempty": bool(STATIONS)}
        checks["station_gate"] = bool(STATIONS) and set(STATIONS).issubset(set(get_all_stations()))
    except Exception as e:
        checks["station_gate"] = False; checks["station_error"] = repr(e)
    qc = []
    if checks["clean_gate"] and checks["canonical_exists"] and checks["config_exists"] and checks["data_exists"] and checks.get("station_gate"):
        for command in QC_COMMANDS:
            q = run_command(command, args.qc_timeout)
            qc.append(q)
            if q["exit_code"] != 0 or q["timed_out"]: break
    else:
        qc.append({"skipped": True, "reason": "pre-QC gate failed"})
    checks["qc"] = qc
    checks["qc_gate"] = bool(qc) and qc[-1].get("exit_code") == 0 and not qc[-1].get("timed_out") and len(qc) == len(QC_COMMANDS)
    checks["disposition"] = "PASS" if checks["clean_gate"] and checks["qc_gate"] else "BLOCKED"
    return checks

def write_json(path, obj): path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")

def main():
    ap = argparse.ArgumentParser(description="Preflight or execute the canonical sweep; execution requires --execute.")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--run-root", default=str(REPO_ROOT / ".meta" / "unattended-sweep-runs"))
    ap.add_argument("--qc-timeout", type=int, default=1800)
    ap.add_argument("--sweep-timeout", type=int, default=86400)
    ap.add_argument("--allow-dirty-path", action="append", default=[], help="Explicit path prefix allowed to be dirty; all other changes block.")
    args = ap.parse_args()
    run_dir = Path(args.run_root) / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=False)
    started = utcnow(); pre = preflight(args, run_dir)
    write_json(run_dir / "preflight.json", pre)
    manifest = {"created_at": started, "git_head": pre["git_head"], "canonical_command": CANONICAL, "source_hashes": pre["source_hashes"]}
    write_json(run_dir / "manifest.json", manifest)
    (run_dir / "stdout.log").write_text(""); (run_dir / "stderr.log").write_text("")
    disposition = pre["disposition"]
    result = {"started_at": started, "execute_requested": args.execute, "disposition": disposition, "exit_code": EXIT_BLOCKED if disposition != "PASS" else 0}
    if args.execute and disposition == "PASS":
        with open(run_dir / "stdout.log", "w") as out, open(run_dir / "stderr.log", "w") as err:
            child = run_command(CANONICAL, args.sweep_timeout, out, err)
        after = status_lines(ignore=[run_dir]); result.update(child, finished_at=utcnow(), dirty_after=after, source_changed=(after != pre["dirty_before"]))
        if child["timed_out"]: result.update(disposition="BLOCKED", exit_code=EXIT_TIMEOUT)
        elif child["exit_code"] != 0 or result["source_changed"]: result.update(disposition="BLOCKED", exit_code=EXIT_QC)
        else: result.update(disposition="PASS", exit_code=EXIT_PASS)
    elif not args.execute and disposition == "PASS":
        result["disposition"] = "PARK"; result["exit_code"] = EXIT_PASS
    write_json(run_dir / "result.json", result)
    hashes = {p.name: sha256_file(p) for p in run_dir.iterdir() if p.is_file() and p.name != "sha256-manifest.json"}
    write_json(run_dir / "sha256-manifest.json", hashes)
    receipt = {"run_dir": str(run_dir), "disposition": result["disposition"], "real_sweep_executed": bool(args.execute and pre["disposition"] == "PASS"), "continuity": "source/config/data hashes and pre/post status recorded", "requires_dan_go_ahead": True}
    write_json(run_dir / "continuity-receipt.json", receipt)
    print(json.dumps({"run_dir": str(run_dir), "disposition": result["disposition"], "exit_code": result["exit_code"]}, sort_keys=True))
    return result["exit_code"]

if __name__ == "__main__": raise SystemExit(main())
