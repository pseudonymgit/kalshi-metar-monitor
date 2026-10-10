#!/usr/bin/env python3
"""
stopping_record.py — machine-checkable governance stopping record (Workstream F).

The weather engine runs under a pre-registered experiment (GR5-WE-001). This
module is the single off-switch that decides whether ANY producer run is
permitted, and — when it is not — records the enforcement action.

    is_project_stopped := A1 ∨ A2 ∨ A3 ∨ A4

      A1 Falsified        falsification-certificate.json with verdict=="FALSIFIED"
      A2 Unmeasurable     repair budget exhausted AND
                          (canonical ledger undesignated OR unexplained_rows > 0)
      A3 Hurdle failed    market-relative upper 95% bound on net-of-cost EV <= 0
                          over >= 200 events
      A4 Governance breach SBOX->DEV merge / unapproved live exposure /
                          fee-or-settlement-gate bypass

State enum: {ACTIVE, ACTIVE-BOUNDED, PARKED, STOPPED, STOPPED-PERMANENT,
             UNDETERMINED-STOP}

Only ACTIVE-BOUNDED (an unexpired, hash-verified pre-registration) permits a
run. UNDETERMINED-STOP fails closed and is treated as STOPPED.

Root sunset (F2): 2026-12-31. If no pre-registered test PASSES by then the
project auto-terminates. Extensions require an explicit dated written decision
recorded in stopping-record.json (never a silent edit).

Deterministic. No AI/ML.

CLI:
    python3 scripts/stopping_record.py --check          # 0=proceed 1=stopped 2=fail-closed
    python3 scripts/stopping_record.py --write          # emit stopping-record.json
    python3 scripts/stopping_record.py --halt "reason"  # force stop + halt artifact
    python3 scripts/stopping_record.py --status         # human-readable state

Optional:
    --repo DIR        repo root (default: this file's parent's parent)
    --cert-dir DIR    where falsification-certificate.json may live (repeatable)
    --json            machine-readable output on stdout
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ─── Constants ───────────────────────────────────────────────────────────────
SCHEMA_VERSION = 1
RECORD_NAME = "stopping-record.json"
CERT_NAME = "falsification-certificate.json"

# Root sunset (F2). Extensions must be recorded here AND in the JSON.
ROOT_SUNSET = "2026-12-31"

# The experiment this off-switch governs (pre-registration freeze, GR5 D2).
GOVERNED_EXPERIMENT = "GR5-WE-001"
PREREG_DIR = "artifacts/preregistration"

# A3 threshold: need at least this many events to judge the hurdle.
HURDLE_MIN_EVENTS = 200

# Where a falsification certificate may legitimately be dropped.
DEFAULT_CERT_DIRS = ("", "artifacts/governance")

# Producer crons that must be disabled on STOP (recorded; disabled only when
# OPENCLAW_CRON_DISABLE=1 so a check never mutates the scheduler by surprise).
PRODUCER_CRONS = [
    "weather-engine-paper-trading-prod",
    "weather-engine-paper-trading-sbox",
    "weather-engine-collector",
    "weather-engine-settlement-upkeep",
]

STATES = ("ACTIVE", "ACTIVE-BOUNDED", "PARKED", "STOPPED",
          "STOPPED-PERMANENT", "UNDETERMINED-STOP")

EXIT_PROCEED = 0
EXIT_STOPPED = 1
EXIT_FAIL_CLOSED = 2


# ─── Helpers ─────────────────────────────────────────────────────────────────
def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def record_path(repo: Path) -> Path:
    return repo / RECORD_NAME


def _load_record(repo: Path) -> Dict[str, Any]:
    return _read_json(record_path(repo)) or {}


def _search_cert(dirs: List[str], repo: Path) -> Optional[Dict[str, Any]]:
    """First falsification certificate found wins."""
    for d in dirs:
        p = (repo / d / CERT_NAME) if d else (repo / CERT_NAME)
        data = _read_json(p)
        if data is not None:
            data.setdefault("_path", str(p))
            return data
    return None


def _verify_prereg(repo: Path, experiment: str) -> Dict[str, Any]:
    """Hash-verify the frozen pre-registration against its sidecar."""
    base = repo / PREREG_DIR
    jp = base / f"{experiment}.json"
    sp = base / f"{experiment}.json.sha256"
    if not jp.exists():
        return {"ok": False, "reason": f"prereg missing: {jp}"}
    if not sp.exists():
        return {"ok": False, "reason": f"prereg sidecar missing: {sp}"}
    import hashlib
    digest = hashlib.sha256(jp.read_bytes()).hexdigest()
    try:
        want = sp.read_text(encoding="utf-8").split()[0]
    except (OSError, IndexError):
        return {"ok": False, "reason": f"prereg sidecar unreadable: {sp}"}
    if digest != want:
        return {"ok": False, "reason": f"prereg hash mismatch {digest} != {want}"}
    art = _read_json(jp) or {}
    return {"ok": True, "sha256": digest, "frozen_at_utc": art.get("frozen_at_utc")}


def _sunset_passed(sunset: str) -> bool:
    try:
        y, m, d = (int(x) for x in sunset.split("-"))
        return date.today() > date(y, m, d)
    except (ValueError, AttributeError):
        # Unparseable sunset fails closed: treat as passed.
        return True


# ─── Evaluation ──────────────────────────────────────────────────────────────
def evaluate(repo: Path, cert_dirs: Optional[List[str]] = None) -> Dict[str, Any]:
    """Compute the stopping state + which A-conditions fired."""
    repo = Path(repo)
    dirs = list(cert_dirs) if cert_dirs else list(DEFAULT_CERT_DIRS)
    env_dir = os.environ.get("STOPPING_RECORD_CERT_DIR")
    if env_dir:
        dirs = [env_dir] + dirs

    rec = _load_record(repo)
    fails: List[str] = []

    # ── A1 Falsified ──
    cert = _search_cert(dirs, repo)
    a1 = bool(cert) and str(cert.get("verdict", "")).upper() == "FALSIFIED"
    if a1:
        fails.append("A1_FALSIFIED")

    # ── A2 Unmeasurable ──
    rb = rec.get("repair_budget", {}) or {}
    cl = rec.get("canonical_ledger", {}) or {}
    budget_exhausted = bool(rb.get("exhausted", False))
    undesignated = not bool(cl.get("designated", True))
    try:
        unexplained = int(cl.get("unexplained_rows", 0) or 0)
    except (TypeError, ValueError):
        unexplained = 0
    a2 = budget_exhausted and (undesignated or unexplained > 0)
    if a2:
        fails.append("A2_UNMEASURABLE")

    # ── A3 Hurdle failed ──
    hurdle = rec.get("hurdle", {}) or {}
    try:
        events = int(hurdle.get("events", 0) or 0)
    except (TypeError, ValueError):
        events = 0
    upper = hurdle.get("upper_95_bound_net_ev")
    a3 = False
    if events >= HURDLE_MIN_EVENTS and upper is not None:
        try:
            a3 = float(upper) <= 0.0
        except (TypeError, ValueError):
            a3 = False
    if a3:
        fails.append("A3_HURDLE_FAILED")

    # ── A4 Governance breach ──
    gov = rec.get("governance", {}) or {}
    gov_events = gov.get("events", []) or []
    gov_breach_flags = [
        "sbox_to_dev_merge", "unapproved_live_exposure",
        "fee_gate_bypass", "settlement_gate_bypass",
    ]
    a4 = bool(gov_events) or any(bool(gov.get(k, False)) for k in gov_breach_flags)
    if a4:
        fails.append("A4_GOVERNANCE_BREACH")

    # ── Boundedness (only ACTIVE-BOUNDED permits a run) ──
    prereg = _verify_prereg(repo, GOVERNED_EXPERIMENT)
    sunset = rec.get("root_sunset", ROOT_SUNSET)
    sunset_passed = _sunset_passed(sunset)
    # An explicit dated extension decision recorded in the JSON lifts sunset.
    ext = rec.get("extension_decision") or {}
    if ext.get("decided_at_utc") and ext.get("new_sunset"):
        sunset = ext["new_sunset"]
        sunset_passed = _sunset_passed(sunset)

    is_stopped = bool(fails)
    forced = str(rec.get("forced_state", "")).upper()
    if forced == "STOPPED-PERMANENT":
        is_stopped = True
        if "FORCED_PERMANENT" not in fails:
            fails.append("FORCED_PERMANENT")

    # ── State resolution ──
    declared = str(rec.get("declared_state", "")).upper()
    if not is_stopped:
        if not prereg.get("ok"):
            # Cannot verify the frozen pre-registration -> fail closed.
            state = "UNDETERMINED-STOP"
            is_stopped = True
            fails.append("PREREG_UNVERIFIED")
        elif not rec:
            # The committed record is the off-switch; its absence fails closed.
            state = "UNDETERMINED-STOP"
            is_stopped = True
            fails.append("RECORD_MISSING")
        elif sunset_passed:
            # No passing pre-registered test by sunset -> auto-terminate.
            state = "STOPPED"
            is_stopped = True
            fails.append("SUNSET_EXPIRED")
        elif declared in ("ACTIVE", "PARKED"):
            state = declared
        else:
            state = "ACTIVE-BOUNDED"
    else:
        if forced == "STOPPED-PERMANENT" or a4:
            state = "STOPPED-PERMANENT"
        else:
            state = "STOPPED"

    return {
        "state": state,
        "is_project_stopped": bool(is_stopped),
        "conditions": fails,
        "a1_falsified": a1,
        "a2_unmeasurable": a2,
        "a3_hurdle_failed": a3,
        "a4_governance_breach": a4,
        "sunset": sunset,
        "sunset_passed": sunset_passed,
        "preregistration": prereg,
        "certificate": (cert.get("_path") if a1 else None),
        "evaluated_at_utc": _utc_now(),
    }


# ─── Enforcement ─────────────────────────────────────────────────────────────
def enforce_stop(repo: Path, ev: Dict[str, Any], reason: str) -> Dict[str, Any]:
    """Bind the stop to concrete artifacts (crons, ledger, writes, halt file)."""
    repo = Path(repo)
    gov_dir = repo / "artifacts" / "governance"
    gov_dir.mkdir(parents=True, exist_ok=True)
    now = _utc_now()

    # 1) Record (and optionally disable) producer crons.
    crons_requested = list(PRODUCER_CRONS)
    crons_disabled: List[str] = []
    if os.environ.get("OPENCLAW_CRON_DISABLE") == "1":
        for job in crons_requested:
            try:
                subprocess.run(["openclaw", "cron", "disable", job],
                               capture_output=True, text=True, timeout=30)
                crons_disabled.append(job)
            except (OSError, subprocess.SubprocessError):
                pass

    # 2) Freeze the canonical ledger + block new trade/settlement writes.
    (gov_dir / "canonical-ledger-frozen.flag").write_text(
        f"frozen=true\nat={now}\nreason={reason}\n", encoding="utf-8")
    (gov_dir / "trading-blocked.flag").write_text(
        f"blocked=true\nscope=trades,settlements\nat={now}\n"
        f"reason={reason}\nstate={ev['state']}\nconditions={','.join(ev['conditions'])}\n",
        encoding="utf-8")

    # 3) Halt artifact into continuity (runtime; .meta/ is gitignored).
    halt_dir = repo / ".meta" / "continuity" / "weather-engine"
    halt_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.replace(":", "").replace("-", "")[:15] + "Z"
    halt_path = halt_dir / f"HALT-stopping-record-{stamp}.md"
    halt_path.write_text(
        "\n".join([
            "# WEATHER ENGINE — GOVERNANCE HALT",
            "",
            f"- emitted_at_utc: {now}",
            f"- state: {ev['state']}",
            f"- is_project_stopped: {ev['is_project_stopped']}",
            f"- conditions: {', '.join(ev['conditions']) or 'none'}",
            f"- reason: {reason}",
            f"- sunset: {ev['sunset']}",
            f"- preregistration_ok: {ev['preregistration'].get('ok')}",
            "",
            "## Enforcement binding",
            f"- producer_crons_disabled: {crons_disabled or crons_requested}",
            f"- cron_disable_applied: {os.environ.get('OPENCLAW_CRON_DISABLE') == '1'}",
            "- canonical_ledger frozen: artifacts/governance/canonical-ledger-frozen.flag",
            "- trade/settlement writes blocked: artifacts/governance/trading-blocked.flag",
            "",
            "Resume requires an explicit dated written decision recorded in",
            "stopping-record.json (extension_decision) — never a silent edit.",
            "",
        ]), encoding="utf-8")

    return {
        "enforced_at_utc": now,
        "producer_crons": crons_requested,
        "crons_disabled": crons_disabled,
        "cron_disable_applied": os.environ.get("OPENCLAW_CRON_DISABLE") == "1",
        "canonical_ledger_frozen": True,
        "trades_blocked": True,
        "settlements_blocked": True,
        "halt_artifact": str(halt_path.relative_to(repo)),
    }


# ─── Default committed record ────────────────────────────────────────────────
def default_record(repo: Path) -> Dict[str, Any]:
    prereg = _verify_prereg(repo, GOVERNED_EXPERIMENT)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": _utc_now(),
        "governed_experiment": GOVERNED_EXPERIMENT,
        "root_sunset": ROOT_SUNSET,
        "extension_decision": None,
        "sunset_policy": (
            "If no pre-registered test PASSES by root_sunset the project "
            "auto-terminates. Extensions require an explicit dated written "
            "decision recorded in extension_decision {decided_at_utc, "
            "new_sunset, authority, rationale}."
        ),
        "forced_state": None,
        "preregistration": {
            "experiment_id": GOVERNED_EXPERIMENT,
            "artifact": f"{PREREG_DIR}/{GOVERNED_EXPERIMENT}.json",
            "ok": prereg.get("ok", False),
            "sha256": prereg.get("sha256"),
        },
        "repair_budget": {"exhausted": False, "note": "set exhausted=true when repair budget runs out"},
        "canonical_ledger": {
            "designated": True,
            "unexplained_rows": 0,
            "path": "data/kalshi_settlements.db",
            "frozen": False,
        },
        "hurdle": {
            "metric": "net_ev_per_accepted_trade",
            "market_relative": True,
            "min_events": HURDLE_MIN_EVENTS,
            "events": 0,
            "upper_95_bound_net_ev": None,
        },
        "governance": {
            "events": [],
            "sbox_to_dev_merge": False,
            "unapproved_live_exposure": False,
            "fee_gate_bypass": False,
            "settlement_gate_bypass": False,
        },
        "enforcement": {
            "producer_crons": list(PRODUCER_CRONS),
            "crons_disabled": [],
            "canonical_ledger_frozen": False,
            "trades_blocked": False,
            "settlements_blocked": False,
            "halt_artifact": None,
        },
        "notes": (
            "Machine-checkable stopping record (Workstream F). CI/cron must run "
            "`scripts/ci_stop_check.sh` (wraps --check) before any producer run."
        ),
    }


def write_record(repo: Path, ev: Optional[Dict[str, Any]] = None) -> Path:
    repo = Path(repo)
    rec = default_record(repo)
    if ev and ev.get("is_project_stopped"):
        rec["enforcement"] = enforce_stop(repo, ev, "state=" + ev["state"])
        rec["canonical_ledger"]["frozen"] = True
    out = record_path(repo)
    out.write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    return out


# ─── CLI ─────────────────────────────────────────────────────────────────────
def _repo_default() -> Path:
    return Path(__file__).resolve().parents[1]


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Governance stopping record (Workstream F)")
    ap.add_argument("--check", action="store_true", help="0=proceed 1=stopped 2=fail-closed")
    ap.add_argument("--write", action="store_true", help="emit stopping-record.json")
    ap.add_argument("--halt", metavar="REASON", default=None,
                    help="force STOPPED-PERMANENT + write halt artifact")
    ap.add_argument("--status", action="store_true", help="print state")
    ap.add_argument("--repo", default=None)
    ap.add_argument("--cert-dir", action="append", default=None,
                    help="directory that may hold falsification-certificate.json")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    repo = Path(args.repo).resolve() if args.repo else _repo_default()

    if args.halt is not None:
        ev = evaluate(repo, args.cert_dir)
        ev["state"] = "STOPPED-PERMANENT"
        ev["is_project_stopped"] = True
        if "FORCED_HALT" not in ev["conditions"]:
            ev["conditions"].append("FORCED_HALT")
        enf = enforce_stop(repo, ev, args.halt)
        ev["enforcement"] = enf
        write_record(repo, ev)
        payload = {"state": ev["state"], "enforcement": enf}
        print(json.dumps(payload, indent=2) if args.json
              else f"HALTED: {ev['state']} -> {enf['halt_artifact']}")
        return EXIT_STOPPED

    ev = evaluate(repo, args.cert_dir)

    if args.write:
        p = write_record(repo, ev)
        print(json.dumps({"wrote": str(p), "state": ev["state"]}) if args.json
              else f"wrote {p} (state={ev['state']})")
        return EXIT_PROCEED

    if args.status or args.json or not args.check:
        if args.json:
            print(json.dumps(ev, indent=2, sort_keys=True))
        else:
            print(f"state={ev['state']} stopped={ev['is_project_stopped']} "
                  f"conditions={ev['conditions'] or ['none']} sunset={ev['sunset']}")
        if not args.check:
            return EXIT_PROCEED

    # --check (or --check combined with other flags)
    if ev["state"] == "UNDETERMINED-STOP":
        print(f"FAIL-CLOSED (UNDETERMINED-STOP) conditions={ev['conditions']}",
              file=sys.stderr)
        return EXIT_FAIL_CLOSED
    if ev["is_project_stopped"]:
        print(f"STOPPED state={ev['state']} conditions={ev['conditions']}",
              file=sys.stderr)
        return EXIT_STOPPED
    if ev["state"] == "ACTIVE-BOUNDED":
        print(f"PROCEED state={ev['state']} prereg={ev['preregistration'].get('sha256')}")
        return EXIT_PROCEED
    # Any other non-bounded state cannot run (only ACTIVE-BOUNDED permits a run).
    print(f"STOPPED state={ev['state']} (not ACTIVE-BOUNDED)", file=sys.stderr)
    return EXIT_STOPPED


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
