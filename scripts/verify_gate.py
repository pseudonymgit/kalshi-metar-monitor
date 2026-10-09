#!/usr/bin/env python3
"""
verify_gate.py — A-series structural gate (AST-based, deterministic).

Locks in the invariants the A1-A4 remediation established so a later edit
cannot silently reintroduce them. Parses Python (never greps), so comments,
docstrings, and dead strings cannot fool it.

Checks
------
C1 fee    the trade-write / P&L / sizing path uses the canonical,
          price-dependent Kalshi fee from core.fees, and the flat 0.0205 fee
          literal is gone from that path.
C2 prov   the trade-write path stamps a provenance field (price_source) on the
          rows it writes, with a column-ensure migration.
C3 axis   the eval + calibration axis is strict strike-crossing, never
          day-over-day (bucket > prev_bucket).

Exit codes (deterministic)
--------------------------
0  PASS
1  usage error
2  fail-closed (a gated file is missing/unparseable, or a check is ambiguous)
3  P0-FAIL (an A-series invariant is violated in source)
4  PROVISIONAL (non-P0 divergence surfaced; the gate cannot fully certify)

Standalone + importable
-----------------------
    python3 scripts/verify_gate.py [--repo PATH] [--json]
    from verify_gate import run_checks, evaluate

Precedence: P0 (3) > fail-closed (2) > PROVISIONAL (4) > PASS (0).
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_DEFAULT = Path(__file__).resolve().parents[1]

EXIT_PASS = 0
EXIT_USAGE = 1
EXIT_FAIL_CLOSED = 2
EXIT_P0 = 3
EXIT_PROVISIONAL = 4

# C1: the flat per-contract fee that A3 removed. Any float literal equal to this
# anywhere on the trade-write / fee / P&L / sizing path is a P0 regression.
FLAT_FEE_LITERALS = (0.0205,)

# C1: modules on that path. A flat-fee float literal here is a P0.
FEE_PATH_MODULES = (
    "core/fees.py",
    "core/market_cost_model.py",
    "core/pnl_tracking.py",
    "core/paper_trading_engine.py",
    "core/position_sizing.py",
    "core/position_sizer.py",
    "core/fee_aware_kelly_position_sizing.py",
    "core/kelly_position_sizer.py",
    "core/trade_execution.py",
    "core/instance_config.py",
    "scripts/phase1_paper_trading_cron.py",
    "scripts/sbox_intraday_ev_bridge.py",
)

# C1: modules that must import the canonical fee from core.fees (the trade-write
# and P&L read paths). Missing import => P0.
CANONICAL_FEE_CONSUMERS = (
    "core/pnl_tracking.py",
    "core/paper_trading_engine.py",
)
CANONICAL_FEE_NAMES = frozenset({
    "fee_per_contract", "kalshi_fee", "taker_fee_rate",
    "round_trip_fee", "fee_adjusted_edge",
})

# C2: write paths whose trades INSERT must carry provenance.
PROVENANCE_WRITE_PATHS = ("scripts/sbox_intraday_ev_bridge.py",)
PROVENANCE_FIELD = "price_source"
PROVENANCE_TABLE = "trades"

# C3: eval/calibration modules that must declare the strike-crossing axis.
AXIS_MODULES = (
    "core/unified_backtest.py",
    "core/calibrate_all_signals.py",
    "core/intraday/calibration.py",
    "scripts/run_calibration_pipeline.py",
)
AXIS_VALUE = "strike_crossing"
AXIS_CONST = "LABEL_AXIS"

_ORDERING = (ast.Gt, ast.Lt, ast.GtE, ast.LtE)


@dataclass
class Check:
    name: str
    status: str  # PASS | P0 | FAIL-CLOSED | PROVISIONAL
    evidence: list[str] = field(default_factory=list)


def _load_ast(repo: Path, rel: str):
    """Return (tree, None) or (None, error-string). Never raises."""
    path = repo / rel
    if not path.exists():
        return None, f"{rel}: MISSING"
    try:
        return ast.parse(path.read_text(encoding="utf-8", errors="replace")), None
    except SyntaxError as exc:
        return None, f"{rel}: SYNTAX ERROR at line {exc.lineno}: {exc.msg}"
    except OSError as exc:
        return None, f"{rel}: UNREADABLE: {exc}"


def _float_literals(tree, targets):
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, float):
            if any(node.value == t for t in targets):
                hits.append((node.lineno, node.value))
    return hits


def _imports_canonical_fee(tree) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = (node.module or "")
            if mod.split(".")[-1] == "fees" or mod.endswith("core.fees"):
                if any(a.name in CANONICAL_FEE_NAMES for a in node.names):
                    return True
        elif isinstance(node, ast.Import):
            if any(a.name.endswith("core.fees") or a.name == "core.fees" for a in node.names):
                return True
    return False


def _bucket_compare_violations(tree) -> list[str]:
    """Find day-over-day comparisons (operands are both bucket-like, or one is
    the literal prev_bucket name). A `bucket > strike` strike-crossing compare
    is NOT a violation."""
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not any(isinstance(op, _ORDERING) for op in node.ops):
            continue
        names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        if "prev_bucket" in names:
            bad.append(f"line {node.lineno}: day-over-day compare references prev_bucket")
            continue
        bucket_names = {n for n in names if "bucket" in n.lower()}
        if len(bucket_names) >= 2:
            bad.append(f"line {node.lineno}: day-over-day compare {sorted(bucket_names)}")
    return bad


def check_fee_path(repo: Path) -> Check:
    ev: list[str] = []
    status = "PASS"
    for rel in FEE_PATH_MODULES:
        tree, err = _load_ast(repo, rel)
        if err:
            ev.append(err)
            status = "FAIL-CLOSED"
            continue
        for lineno, val in _float_literals(tree, FLAT_FEE_LITERALS):
            status = "P0"
            ev.append(f"{rel}:{lineno}: flat fee float literal {val!r} on the fee/P&L path")
    for rel in CANONICAL_FEE_CONSUMERS:
        tree, err = _load_ast(repo, rel)
        if err:
            ev.append(err)
            status = "FAIL-CLOSED"
            continue
        if not _imports_canonical_fee(tree):
            status = "P0"
            ev.append(f"{rel}: does not import the canonical fee from core.fees")
    if status == "PASS":
        ev.append(f"{len(FEE_PATH_MODULES)} modules clean; "
                  f"{len(CANONICAL_FEE_CONSUMERS)} canonical-fee consumers import core.fees")
    return Check("C1-fee", status, ev)


def check_provenance(repo: Path) -> Check:
    ev: list[str] = []
    status = "PASS"
    for rel in PROVENANCE_WRITE_PATHS:
        tree, err = _load_ast(repo, rel)
        if err:
            return Check("C2-provenance", "FAIL-CLOSED", [err])

        has_param = any(
            isinstance(n, ast.FunctionDef)
            and any(a.arg == PROVENANCE_FIELD for a in n.args.args)
            for n in ast.walk(tree)
        )
        strings = [n.value for n in ast.walk(tree)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        has_insert = any(
            f"INSERT INTO {PROVENANCE_TABLE}" in s and PROVENANCE_FIELD in s
            for s in strings
        )
        has_migration = any(
            f"ALTER TABLE {PROVENANCE_TABLE}" in s and PROVENANCE_FIELD in s
            for s in strings
        )
        if not (has_param and has_insert and has_migration):
            status = "P0"
            missing = [
                k for k, ok in (("param", has_param), ("insert", has_insert),
                                ("migration", has_migration)) if not ok
            ]
            ev.append(f"{rel}: provenance wiring incomplete (missing: {', '.join(missing)})")
        else:
            ev.append(f"{rel}: price_source param + INSERT + column migration present")
    return Check("C2-provenance", status, ev)


def check_axis(repo: Path) -> Check:
    ev: list[str] = []
    status = "PASS"
    for rel in AXIS_MODULES:
        tree, err = _load_ast(repo, rel)
        if err:
            ev.append(err)
            status = "FAIL-CLOSED"
            continue
        declared = [
            n.value.value
            for n in ast.walk(tree)
            if isinstance(n, ast.Assign)
            and isinstance(n.value, ast.Constant)
            for t in n.targets
            if isinstance(t, ast.Name) and t.id == AXIS_CONST
        ]
        if AXIS_VALUE not in declared:
            status = "P0"
            ev.append(f"{rel}: {AXIS_CONST} != {AXIS_VALUE!r} (found {declared or 'none'})")
        for v in _bucket_compare_violations(tree):
            status = "P0"
            ev.append(f"{rel}:{v}")
    if status == "PASS":
        ev.append(f"{len(AXIS_MODULES)} modules declare {AXIS_CONST}={AXIS_VALUE!r}; no day-over-day compare")
    return Check("C3-axis", status, ev)


def run_checks(repo: Path) -> list[Check]:
    repo = Path(repo)
    return [check_fee_path(repo), check_provenance(repo), check_axis(repo)]


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
    ap = argparse.ArgumentParser(description="A-series structural gate (AST-based).")
    ap.add_argument("--repo", default=str(REPO_DEFAULT),
                    help="repository root (default: this script's repo)")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    try:
        args = ap.parse_args(argv)
    except SystemExit:
        return EXIT_USAGE

    repo = Path(args.repo)
    if not repo.exists():
        print(f"verify_gate: repo not found: {repo}", file=sys.stderr)
        return EXIT_FAIL_CLOSED

    checks = run_checks(repo)
    code = _exit_for(checks)

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
        print(f"verify_gate: exit {code} "
              f"({'PASS' if code == 0 else 'P0-FAIL' if code == 3 else 'FAIL-CLOSED' if code == 2 else 'PROVISIONAL' if code == 4 else 'USAGE'})")
    return code


if __name__ == "__main__":
    sys.exit(main())
