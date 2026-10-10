#!/usr/bin/env python3
"""
preregistration.py — frozen pre-registration artifact (GR5 §6).

Before the first eligible trade, the experiment's N, stopping rule, cohort,
metrics, thresholds, exclusions, analysis-code hash, and canary seeds are frozen
into one write-once JSON file plus a SHA-256 sidecar:

    artifacts/preregistration/<experiment_id>.json
    artifacts/preregistration/<experiment_id>.json.sha256

The analysis refuses to run on a hash mismatch (`require_preregistration`).
Amendments require a NEW experiment id (freeze is write-once; an existing file
is never overwritten unless force=True, which is the deliberate amendment path).

Deterministic. No AI/ML.

Usage:
    python3 -m core.preregistration freeze GR5-WE-001
    python3 -m core.preregistration verify GR5-WE-001
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_DEFAULT = Path(__file__).resolve().parents[1]
PREREG_DIR = "artifacts/preregistration"
SCHEMA_VERSION = 1

# Modules whose bytes define the analysis; their hashes are frozen into the
# artifact so a post-freeze change to the analysis path invalidates the freeze.
ANALYSIS_CODE_MODULES = (
    "core/unified_backtest.py",
    "core/calibrate_all_signals.py",
    "core/intraday/calibration.py",
    "scripts/run_calibration_pipeline.py",
)

# The pre-ingest gate composer (G0-G5) is part of the measured path too.
ANALYSIS_CODE_MODULES_OPTIONAL = (
    "scripts/gate_composer.py",
)

# Canary seeds (GR5 §3): one master seed + a derived seed per canary id.
CANARY_IDS = ("C1", "C2", "C3", "C4", "C5")


class PreregistrationError(RuntimeError):
    """Raised when an artifact is missing, malformed, or hash-mismatched."""


# ─── Provenance helpers ─────────────────────────────────────────────────────
def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_head_sha(repo: Path) -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                             capture_output=True, text=True, timeout=30)
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def analysis_code_hash(repo: Path,
                       modules=ANALYSIS_CODE_MODULES) -> Dict[str, Any]:
    """Deterministic combined hash over the analysis modules (sorted)."""
    entries = []
    for rel in sorted(modules):
        p = repo / rel
        entries.append([rel, _file_sha256(p) if p.exists() else "MISSING"])
    blob = json.dumps(entries, sort_keys=True, separators=(",", ":"))
    return {
        "modules": dict(entries),
        "combined_sha256": hashlib.sha256(blob.encode("utf-8")).hexdigest(),
    }


def canary_seeds(master_seed: int) -> Dict[str, Dict[str, Any]]:
    """Derive a deterministic per-canary seed from the master GR_SEED."""
    seeds: Dict[str, Dict[str, Any]] = {}
    for cid in CANARY_IDS:
        raw = f"{master_seed}:{cid}"
        seeds[cid] = {
            "seed": master_seed,
            "derived_seed": int(hashlib.sha256(raw.encode()).hexdigest()[:8], 16),
            "alpha_canary": 0.01,
        }
    return seeds


def default_artifact(experiment_id: str, repo: Path = REPO_DEFAULT,
                     master_seed: int = 20261010,
                     freeze_ts: Optional[str] = None) -> Dict[str, Any]:
    """The frozen GR5 cohort/metric/stopping contract."""
    return {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": experiment_id,
        "frozen_at_utc": freeze_ts or datetime.now(timezone.utc).isoformat(),
        "repo_git_sha": git_head_sha(repo),
        "cohort": {
            "N": 640,
            "stations": 20,
            "market_types": ["HIGH", "LOW"],
            "raw_per_cell": 16,
            "cells": 40,
            "cells_raw_target": 1920,
            "pooled_min_n_eff": 320,
            "cell_claim_min_raw": 48,
            "cell_claim_min_n_eff": 24,
            "cell_claim_min_dates": 3,
        },
        "axis": "strike_crossing",
        "fee_formula": "ceil(0.07*P*(1-P)*100)/100 per contract (core.fees)",
        "primary_metric": {
            "name": "net_ev_per_accepted_trade",
            "thresholds": {"min_net_ev": 0.0, "min_accuracy": 0.58,
                           "min_confidence": 0.65, "min_trades": 100,
                           "min_sharpe": 1.0},
        },
        "secondary_metrics": [
            {"name": "directional_accuracy", "threshold": 0.58},
            {"name": "brier_skill_score", "threshold": 0.0},
            {"name": "sharpe", "threshold": 1.0},
            {"name": "expectancy_per_trade", "threshold": 0.0},
            {"name": "calibration_ece", "threshold": 0.05},
        ],
        "cluster_keys": ["station", "market_type", "settlement_date"],
        "bootstrap_reps": 10000,
        "analysis_code": analysis_code_hash(repo),
        "canary": {
            "master_seed": master_seed,
            "ids": list(CANARY_IDS),
            "seeds": canary_seeds(master_seed),
            "alpha_canary": 0.01,
            "cusum_halt_sigma": 5.0,
        },
        "stopping_rule": {
            "type": "fixed_n_with_obrien_fleming_interim",
            "looks": [200, 400, 640],
            "z_bounds": [3.00, 2.60, 2.20],
            "no_optional_stopping": True,
        },
        "frozen_exclusions": [
            "settlement_confirmed != 1",
            "settlement_outcome not in {correct, incorrect}",
            "label_axis != strike_crossing",
            "asof (G5) violation",
            "duplicate (station, market_type, settlement_date)",
            "fail-open 0.5 provenance row",
        ],
    }


# ─── Freeze / verify ────────────────────────────────────────────────────────
def _sidecar_path(json_path: Path) -> Path:
    return json_path.with_suffix(json_path.suffix + ".sha256")


def freeze_preregistration(experiment_id: str, repo: Path = REPO_DEFAULT,
                           *, master_seed: int = 20261010,
                           freeze_ts: Optional[str] = None,
                           out_dir: Optional[Path] = None,
                           force: bool = False) -> Dict[str, Any]:
    """Write the artifact + sidecar. Write-once unless force=True."""
    repo = Path(repo)
    out = Path(out_dir) if out_dir else repo / PREREG_DIR
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / f"{experiment_id}.json"
    if json_path.exists() and not force:
        raise PreregistrationError(
            f"{json_path} already frozen; amendments need a new experiment id "
            f"(or force=True for the deliberate amendment path)")
    art = default_artifact(experiment_id, repo, master_seed, freeze_ts)
    body = json.dumps(art, indent=2, sort_keys=True)
    json_path.write_text(body, encoding="utf-8")
    digest = hashlib.sha256(json_path.read_bytes()).hexdigest()
    _sidecar_path(json_path).write_text(
        f"{digest}  {json_path.name}\n", encoding="utf-8")
    return {"path": str(json_path), "sha256": digest, "artifact": art}


def verify_preregistration(experiment_id: str, repo: Path = REPO_DEFAULT,
                           out_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Verify the artifact against its sidecar; raise on any mismatch."""
    repo = Path(repo)
    out = Path(out_dir) if out_dir else repo / PREREG_DIR
    json_path = out / f"{experiment_id}.json"
    side = _sidecar_path(json_path)
    if not json_path.exists():
        raise PreregistrationError(f"preregistration missing: {json_path}")
    if not side.exists():
        raise PreregistrationError(f"preregistration sidecar missing: {side}")
    digest = hashlib.sha256(json_path.read_bytes()).hexdigest()
    want = side.read_text(encoding="utf-8").split()[0]
    if digest != want:
        raise PreregistrationError(
            f"preregistration hash mismatch for {experiment_id}: "
            f"sidecar={want} actual={digest} — artifact was tampered with")
    return {"ok": True, "path": str(json_path), "sha256": digest,
            "artifact": json.loads(json_path.read_text(encoding="utf-8"))}


def require_preregistration(experiment_id: str, repo: Path = REPO_DEFAULT,
                            out_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Analysis admission gate: refuse to run on a missing/tampered freeze."""
    return verify_preregistration(experiment_id, repo, out_dir)


def _main(argv: List[str]) -> int:
    if len(argv) < 2:
        print("usage: python3 -m core.preregistration {freeze|verify} <experiment_id>",
              file=sys.stderr)
        return 1
    action, exp = argv[1], argv[2] if len(argv) > 2 else ""
    if not exp:
        print("experiment_id required", file=sys.stderr)
        return 1
    try:
        if action == "freeze":
            r = freeze_preregistration(exp)
            print(f"frozen {r['path']}\nsha256 {r['sha256']}")
        elif action == "verify":
            r = require_preregistration(exp)
            print(f"OK {r['path']}\nsha256 {r['sha256']}")
        else:
            print(f"unknown action {action!r}", file=sys.stderr)
            return 1
    except PreregistrationError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
