#!/usr/bin/env python3
"""
substrate_gate.py — independent substrate recompute + phantom-watch wiring gate.

Two concerns:

1. Fee-independent trade recompute. For every CLOSED trade it recomputes the
   settled P&L from first principles — (settlement contract value vs. the
   realized strike, minus the fill price, times quantity) — with NO fee applied
   at any point. If the stored `realized_pnl` diverges from the recompute, the
   substrate disagrees with what was actually booked and the gate HALTS (P0).
   A trade whose strike is unreadable is not silently skipped: it is
   UNVERIFIABLE and forces PROVISIONAL (the gate will not certify what it
   cannot check).

2. Phantom-watch wiring. observe-and-report only: it asserts that
   `scripts/phantom_watch.py` is referenced by a cron job. After A2 removed the
   silent settlement fall-through, the phantom-success observer is what
   catches a writer that exits 0 while doing no work. If it is not wired into
   cron, the layer is inert: exit 4 (PROVISIONAL) — surfaced, not fatal.

Exit codes (deterministic)
--------------------------
0  PASS
1  usage error
2  fail-closed (a source DB is missing/unreadable, or cron cannot be queried)
3  P0-FAIL (a stored settled P&L diverges from the fee-independent recompute)
4  PROVISIONAL (unverifiable trades, and/or phantom_watch not referenced by cron)

Standalone + importable
-----------------------
    python3 scripts/substrate_gate.py [--repo PATH] [--json]
    from substrate_gate import run_checks, evaluate

Precedence: P0 (3) > fail-closed (2) > PROVISIONAL (4) > PASS (0).
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_DEFAULT = Path(__file__).resolve().parents[1]

EXIT_PASS = 0
EXIT_USAGE = 1
EXIT_FAIL_CLOSED = 2
EXIT_P0 = 3
EXIT_PROVISIONAL = 4

# Trade DBs to reconcile (relative to repo root).
TRADE_DBS = (
    "data/paper_trading_sbox.db",
    "data/paper_trading_dev.db",
    "data/paper_trading_prod.db",
    "data/paper_trading.db",
)

# Plausible °F band for a daily max/min settlement. Outside this band the
# settled_value is not a real temperature and the row cannot be recomputed.
TEMP_MIN_F = -60.0
TEMP_MAX_F = 150.0

PHANTOM_SCRIPT = "scripts/phantom_watch.py"

# Trade types and their settlement payoff (contract value v in {0,1}, fill p):
#   buy_yes : (v     - p    ) * q
#   buy_no  : ((1-v) - (1-p)) * q
#   sell_yes: (p     - v    ) * q
#   sell_no : ((1-p) - (1-v)) * q
_PAYOFFS = {
    "buy_yes": lambda v, p: v - p,
    "buy_no": lambda v, p: (1.0 - v) - (1.0 - p),
    "sell_yes": lambda v, p: p - v,
    "sell_no": lambda v, p: (1.0 - p) - (1.0 - v),
}

_NOTE_STRIKE = re.compile(r"@\s*(-?\d+(?:\.\d+)?)\s*F")
_NOTE_TICKER_STRIKE = re.compile(r"-T(-?\d+(?:\.\d+)?)")

# Tolerances: exact arithmetic allows a tiny epsilon; the divergence threshold
# is one cent (sub-cent differences are float noise, not a substrate break).
_MATCH_EPS = 1e-6
_DIVERGE_TOL = 0.005


@dataclass
class Check:
    name: str
    status: str  # PASS | P0 | FAIL-CLOSED | PROVISIONAL
    evidence: list[str] = field(default_factory=list)


def _strike_from_notes(notes: str):
    if not notes:
        return None
    m = _NOTE_STRIKE.search(notes) or _NOTE_TICKER_STRIKE.search(notes)
    return float(m.group(1)) if m else None


def _recompute(row) -> tuple[str, float | None, str]:
    """Return (verdict, recomputed_pnl, detail).

    verdict in {OK, DIVERGE, UNVERIFIABLE, SKIP}.
    """
    trade_type, qty, fill, settled, booked, market_type, notes = row
    tt = (trade_type or "").lower()
    if tt not in _PAYOFFS:
        return "SKIP", None, f"unknown trade_type={trade_type!r}"
    if qty is None or fill is None or booked is None:
        return "UNVERIFIABLE", None, "null quantity/fill/realized_pnl"
    if settled is None or not (TEMP_MIN_F <= float(settled) <= TEMP_MAX_F):
        return "UNVERIFIABLE", None, f"settled_value={settled!r} not a plausible temp"
    strike = _strike_from_notes(notes)
    if strike is None:
        return "UNVERIFIABLE", None, "no strike parseable from notes"
    mt = (market_type or "").upper()
    if mt == "HIGH":
        v = 1.0 if float(settled) > strike else 0.0
    elif mt == "LOW":
        v = 1.0 if float(settled) < strike else 0.0
    else:
        return "UNVERIFIABLE", None, f"unknown market_type={market_type!r}"

    # Fee-independent: contract payoff only, NO fee term anywhere.
    recomputed = _PAYOFFS[tt](v, float(fill)) * float(qty)
    delta = abs(recomputed - float(booked))
    if delta <= _MATCH_EPS:
        return "OK", recomputed, f"match (Δ={delta:.2e})"
    if delta <= _DIVERGE_TOL:
        return "OK", recomputed, f"within tolerance (Δ={delta:.2e})"
    return "DIVERGE", recomputed, f"booked={booked:.4f} recomputed={recomputed:.4f} Δ={delta:.4f}"


def check_recompute(repo: Path) -> Check:
    ev: list[str] = []
    present = [rel for rel in TRADE_DBS if (repo / rel).exists()]
    if not present:
        return Check("R1-recompute", "FAIL-CLOSED",
                     [f"no trade DB found among {list(TRADE_DBS)}"])

    total = ok = diverge = unverifiable = 0
    unver_samples: list[str] = []
    for rel in present:
        db = repo / rel
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=15)
        except sqlite3.Error as exc:
            return Check("R1-recompute", "FAIL-CLOSED", [f"{rel}: cannot open: {exc}"])
        try:
            cols = {d[1] for d in conn.execute("PRAGMA table_info(trades)")}
            need = {"trade_type", "quantity", "trade_price", "settled_value",
                    "realized_pnl", "market_type", "notes"}
            if not need.issubset(cols):
                missing = sorted(need - cols)
                conn.close()
                return Check("R1-recompute", "FAIL-CLOSED",
                             [f"{rel}: trades table missing columns {missing}"])
            rows = conn.execute(
                "SELECT trade_type, quantity, trade_price, settled_value, "
                "realized_pnl, market_type, notes "
                "FROM trades WHERE status='closed'"
            ).fetchall()
        except sqlite3.Error as exc:
            conn.close()
            return Check("R1-recompute", "FAIL-CLOSED", [f"{rel}: query failed: {exc}"])
        conn.close()

        for i, row in enumerate(rows):
            total += 1
            verdict, recomputed, detail = _recompute(row)
            if verdict == "OK":
                ok += 1
            elif verdict == "DIVERGE":
                diverge += 1
                ev.append(f"{rel}:{i}: DIVERGENCE {detail}")
            elif verdict == "UNVERIFIABLE":
                unverifiable += 1
                if len(unver_samples) < 5:
                    unver_samples.append(f"{rel}:{i}: UNVERIFIABLE {detail}")
            # SKIP is silent

    status = "PASS"
    if diverge:
        status = "P0"
    elif unverifiable:
        status = "PROVISIONAL"
    ev.extend(unver_samples)
    if unverifiable > len(unver_samples):
        ev.append(f"... {unverifiable - len(unver_samples)} more unverifiable row(s)")
    ev.append(f"{len(present)} DB(s), {total} closed trade(s): "
              f"{ok} match, {diverge} diverge, {unverifiable} unverifiable")
    return Check("R1-recompute", status, ev)


def _cron_jobs() -> tuple[list[dict] | None, str]:
    """Load cron jobs from the OpenClaw CLI. Returns (jobs, error)."""
    import subprocess
    try:
        proc = subprocess.run(
            ["openclaw", "cron", "list", "--json"],
            capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError:
        return None, "openclaw CLI not found on PATH"
    except subprocess.SubprocessError as exc:
        return None, f"openclaw cron list failed: {exc}"
    if proc.returncode != 0:
        return None, f"openclaw cron list exit {proc.returncode}: {proc.stderr.strip()[:200]}"
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return None, f"cron list not JSON: {exc}"
    jobs = data if isinstance(data, list) else data.get("jobs", data.get("crons", data.get("items", [])))
    return jobs, ""


def _job_references(job: dict, needle: str) -> bool:
    payload = job.get("payload") or {}
    argv = payload.get("argv") or []
    blob = " ".join(str(a) for a in argv)
    if needle in blob:
        return True
    for key in ("command", "script", "name", "description"):
        val = payload.get(key) or job.get(key)
        if isinstance(val, str) and needle in val:
            return True
    return False


def check_phantom_wiring(repo: Path) -> Check:
    script = repo / PHANTOM_SCRIPT
    if not script.exists():
        return Check("R2-phantom-wiring", "FAIL-CLOSED",
                     [f"{PHANTOM_SCRIPT} does not exist; cannot wire a missing observer"])

    jobs, err = _cron_jobs()
    if jobs is None:
        return Check("R2-phantom-wiring", "FAIL-CLOSED", [err])

    hits = [j for j in jobs if _job_references(j, "phantom_watch.py")]
    if not hits:
        return Check("R2-phantom-wiring", "PROVISIONAL",
                     [f"{PHANTOM_SCRIPT} is not referenced by any of {len(jobs)} cron jobs "
                      f"— phantom-success layer is inert"])
    names = ", ".join(str(j.get("name", j.get("id", "?"))) for j in hits)
    return Check("R2-phantom-wiring", "PASS",
                 [f"phantom_watch.py wired into cron: {names}"])


def run_checks(repo: Path) -> list[Check]:
    repo = Path(repo)
    return [check_recompute(repo), check_phantom_wiring(repo)]


def _exit_for(checks: list[Check]) -> int:
    statuses = {c.status for c in checks}
    if "P0" in statuses:
        return EXIT_P0
    if "FAIL-CLOSED" in statuses:
        return EXIT_FAIL_CLOSED
    if "PROVISIONAL" in statuses:
        return EXIT_PROVISIONAL
    return EXIT_PASS


def evaluate(repo: Path) -> int:
    return _exit_for(run_checks(repo))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Substrate recompute + phantom-watch wiring gate.")
    ap.add_argument("--repo", default=str(REPO_DEFAULT),
                    help="repository root (default: this script's repo)")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    try:
        args = ap.parse_args(argv)
    except SystemExit:
        return EXIT_USAGE

    repo = Path(args.repo)
    if not repo.exists():
        print(f"substrate_gate: repo not found: {repo}", file=sys.stderr)
        return EXIT_FAIL_CLOSED

    checks = run_checks(repo)
    code = _exit_for(checks)
    label = {0: "PASS", 2: "FAIL-CLOSED", 3: "P0-FAIL", 4: "PROVISIONAL"}.get(code, "USAGE")

    if args.json:
        print(json.dumps({
            "repo": str(repo),
            "exit_code": code,
            "checks": [
                {"name": c.name, "status": c.status, "evidence": c.evidence}
                for c in checks
            ],
        }, indent=2))
    else:
        for c in checks:
            print(f"[{c.status}] {c.name}")
            for line in c.evidence:
                print(f"    - {line}")
        print(f"substrate_gate: exit {code} ({label})")
    return code


if __name__ == "__main__":
    sys.exit(main())
