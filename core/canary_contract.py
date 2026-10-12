#!/usr/bin/env python3
"""
canary_contract.py — F-3: freeze the code that actually runs the look-200 battery.

The GR5 prereg artifact (`artifacts/preregistration/GR5-WE-001.json`) freezes
`analysis_code.modules` — the calibration/backtest path
(`core/unified_backtest.py`, `core/calibrate_all_signals.py`,
`core/intraday/calibration.py`, `scripts/run_calibration_pipeline.py`). That hash
does NOT cover the modules that produce the look-200 and canary read-outs:

    core/canary_harness.py
    scripts/run_canaries.py
    scripts/look200_measurement.py

so those modules were mutable after the freeze — the compliance question ("does
the code match the frozen hash?") passed while the substantive question ("is the
measured analysis frozen?") did not (look-200 finding F-3).

This module freezes those bytes in a sidecar the look gate verifies, without
touching the already-frozen prereg artifact (amending the artifact would change
its hash and invalidate the current freeze). The sidecar carries its own SHA-256
in a `.sha256` companion, exactly like the prereg artifact.

CLI:
    python3 -m core.canary_contract freeze GR5-WE-001
    python3 -m core.canary_contract verify GR5-WE-001
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_DEFAULT = Path(__file__).resolve().parents[1]
PREREG_DIR = "artifacts/preregistration"
SCHEMA_VERSION = 1
CONTRACT_SUFFIX = "-canary-contract.json"

# The look-200 / canary measurement path (F-3). Sorted for determinism.
MEASUREMENT_MODULES: Tuple[str, ...] = (
    "core/canary_harness.py",
    "scripts/run_canaries.py",
    "scripts/look200_measurement.py",
)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def measurement_code_hash(repo: Path,
                          modules: Tuple[str, ...] = MEASUREMENT_MODULES
                          ) -> Dict[str, Any]:
    """Deterministic combined hash over the measurement modules (sorted)."""
    entries = []
    for rel in sorted(modules):
        p = repo / rel
        entries.append([rel, _file_sha256(p) if p.exists() else "MISSING"])
    blob = json.dumps(entries, sort_keys=True, separators=(",", ":"))
    return {
        "modules": dict(entries),
        "combined_sha256": hashlib.sha256(blob.encode("utf-8")).hexdigest(),
    }


def contract_path(repo: Path, experiment_id: str) -> Path:
    return repo / PREREG_DIR / f"{experiment_id}{CONTRACT_SUFFIX}"


def _sidecar_path(json_path: Path) -> Path:
    return json_path.with_suffix(json_path.suffix + ".sha256")


def freeze_measurement_contract(repo: Path = REPO_DEFAULT,
                                experiment_id: str = "GR5-WE-001",
                                *, force: bool = False,
                                freeze_ts: Optional[str] = None) -> Dict[str, Any]:
    """Write the measurement-contract sidecar + its SHA-256 companion."""
    repo = Path(repo)
    path = contract_path(repo, experiment_id)
    if path.exists() and not force:
        raise FileExistsError(
            f"{path} already frozen; use force=True for a deliberate amendment")
    path.parent.mkdir(parents=True, exist_ok=True)
    art = {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": experiment_id,
        "frozen_at_utc": freeze_ts or datetime.now(timezone.utc).isoformat(),
        "note": ("F-3: measurement-path freeze. The prereg artifact's "
                 "analysis_code hash does not cover the canary harness/runners; "
                 "this sidecar does, so a post-freeze edit invalidates it."),
        "measurement_code": measurement_code_hash(repo),
    }
    path.write_text(json.dumps(art, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    digest = _file_sha256(path)
    _sidecar_path(path).write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    return {"path": str(path), "sha256": digest, "artifact": art}


def verify_measurement_contract(repo: Path = REPO_DEFAULT,
                                experiment_id: str = "GR5-WE-001"
                                ) -> Dict[str, Any]:
    """Verify the sidecar against its own hash AND the live measurement bytes."""
    repo = Path(repo)
    path = contract_path(repo, experiment_id)
    side = _sidecar_path(path)
    if not path.exists():
        return {"ok": False, "reason": f"canary contract missing: {path}"}
    if not side.exists():
        return {"ok": False, "reason": f"canary contract sidecar missing: {side}"}
    digest = _file_sha256(path)
    try:
        want = side.read_text(encoding="utf-8").split()[0]
    except (OSError, IndexError):
        return {"ok": False, "reason": f"canary contract sidecar unreadable: {side}"}
    if digest != want:
        return {"ok": False, "sha256": digest,
                "reason": f"canary contract hash mismatch {digest} != {want}"}
    art = json.loads(path.read_text(encoding="utf-8"))
    frozen = art.get("measurement_code", {})
    live = measurement_code_hash(repo)
    if live["combined_sha256"] != frozen.get("combined_sha256"):
        return {"ok": False, "sha256": digest,
                "reason": ("measurement code drifted from the frozen contract: "
                           f"{live['combined_sha256']} != "
                           f"{frozen.get('combined_sha256')}"),
                "live": live["combined_sha256"],
                "frozen": frozen.get("combined_sha256")}
    return {"ok": True, "sha256": digest,
            "combined_sha256": frozen.get("combined_sha256"),
            "frozen_at_utc": art.get("frozen_at_utc"),
            "modules": live["modules"]}


def _main(argv: List[str]) -> int:
    if len(argv) < 2:
        print("usage: python3 -m core.canary_contract {freeze|verify} <experiment_id>",
              file=sys.stderr)
        return 1
    action, exp = argv[1], argv[2] if len(argv) > 2 else "GR5-WE-001"
    if action == "freeze":
        r = freeze_measurement_contract(REPO_DEFAULT, exp, force="--force" in argv)
        print(f"frozen {r['path']}\nsha256 {r['sha256']}")
    elif action == "verify":
        r = verify_measurement_contract(REPO_DEFAULT, exp)
        print(json.dumps(r, indent=2, sort_keys=True))
        return 0 if r["ok"] else 2
    else:
        print(f"unknown action {action!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
