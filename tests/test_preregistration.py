#!/usr/bin/env python3
"""
D2 — pre-registration artifact tests.

Locks GR5 §6 invariants:
  - a frozen artifact + SHA-256 sidecar are written;
  - freeze is write-once (a second freeze needs a new id or force);
  - tampering the JSON makes verify() fail (hash mismatch);
  - require_preregistration() refuses analysis on mismatch;
  - the frozen contract carries N=640, axis=strike_crossing, canary seeds,
    stopping rule, analysis-code hash, and frozen exclusions.
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from core.preregistration import (  # noqa: E402
    CANARY_IDS, PreregistrationError, default_artifact,
    freeze_preregistration, require_preregistration, verify_preregistration,
)


def test_freeze_writes_artifact_and_sidecar(tmp_path):
    r = freeze_preregistration("EXP-1", REPO, out_dir=tmp_path)
    assert (tmp_path / "EXP-1.json").exists()
    assert (tmp_path / "EXP-1.json.sha256").exists()
    assert len(r["sha256"]) == 64


def test_frozen_contract_fields(tmp_path):
    freeze_preregistration("EXP-2", REPO, out_dir=tmp_path)
    art = json.loads((tmp_path / "EXP-2.json").read_text())
    assert art["cohort"]["N"] == 640
    assert art["cohort"]["stations"] == 20
    assert art["cohort"]["raw_per_cell"] == 16
    assert art["axis"] == "strike_crossing"
    assert art["bootstrap_reps"] == 10000
    assert art["stopping_rule"]["no_optional_stopping"] is True
    assert art["stopping_rule"]["looks"] == [200, 400, 640]
    assert art["analysis_code"]["combined_sha256"]
    assert set(art["canary"]["seeds"]) == set(CANARY_IDS)
    assert art["canary"]["alpha_canary"] == 0.01
    assert art["frozen_exclusions"]


def test_freeze_is_write_once(tmp_path):
    freeze_preregistration("EXP-3", REPO, out_dir=tmp_path)
    with pytest.raises(PreregistrationError):
        freeze_preregistration("EXP-3", REPO, out_dir=tmp_path)


def test_verify_ok(tmp_path):
    freeze_preregistration("EXP-4", REPO, out_dir=tmp_path)
    r = verify_preregistration("EXP-4", REPO, out_dir=tmp_path)
    assert r["ok"] is True


def test_tamper_fails_hash_check(tmp_path):
    freeze_preregistration("EXP-5", REPO, out_dir=tmp_path)
    p = tmp_path / "EXP-5.json"
    doc = json.loads(p.read_text())
    doc["cohort"]["N"] = 100000  # move the goalposts after the freeze
    p.write_text(json.dumps(doc, indent=2, sort_keys=True))
    with pytest.raises(PreregistrationError) as exc:
        verify_preregistration("EXP-5", REPO, out_dir=tmp_path)
    assert "hash mismatch" in str(exc.value)
    # analysis refuses to run on mismatch
    with pytest.raises(PreregistrationError):
        require_preregistration("EXP-5", REPO, out_dir=tmp_path)


def test_missing_artifact_refused(tmp_path):
    with pytest.raises(PreregistrationError):
        require_preregistration("DOES-NOT-EXIST", REPO, out_dir=tmp_path)


def test_default_artifact_axis_and_seed():
    art = default_artifact("X", REPO, master_seed=42)
    assert art["axis"] == "strike_crossing"
    assert art["canary"]["master_seed"] == 42
    # derived seeds deterministic
    a = default_artifact("X", REPO, master_seed=42)["canary"]["seeds"]
    b = default_artifact("X", REPO, master_seed=42)["canary"]["seeds"]
    assert a == b
