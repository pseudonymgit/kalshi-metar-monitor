from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("srrs_opt", ROOT / "scripts/backfill_srrs_cli_v2_optimized.py")
assert SPEC and SPEC.loader
opt = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(opt)

SOURCE = ROOT / "data/raw/srrs_cli_backfill/2026-08-15-srrs-cli-backfill-final"
START = opt.date(2025, 1, 15)
END = opt.date(2026, 2, 28)


def _selective_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a small source fixture from the approved root, without its cache."""
    source = tmp_path / "source"
    (source / "raw").mkdir(parents=True)
    shutil.copy2(SOURCE / "checkpoint.json", source / "checkpoint.json")
    shutil.copy2(SOURCE / "manifest.jsonl", source / "manifest.jsonl")
    rows = [json.loads(x) for x in (SOURCE / "manifest.jsonl").read_text().splitlines() if x.strip()]
    refs = {r["raw_path"] for row in rows for r in (row.get("accepted_reports", []) + row.get("failures", [])) if r.get("raw_path")}
    for rel in refs:
        dst = source / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SOURCE / rel, dst)
    # The approved source checkpoint names sibling reviewed v3 artifacts.  The
    # lineage verifier is covered by the production path; this fixture isolates
    # import/checkpoint mechanics without duplicating the large v3 tree.
    monkeypatch.setattr(opt, "_verify_lineage_artifacts", lambda *_: {})
    return source


def test_import_initializes_exact_terminal_count_and_remaining_worklist(tmp_path, monkeypatch):
    source = _selective_source(tmp_path, monkeypatch)
    dest = tmp_path / "optimized"
    out, state, rows, remaining = opt.import_checkpoint(
        opt.load_fetcher(), source, dest, START, END, 4, .30, 60, 5
    )
    assert out == dest
    assert len(rows) == 318
    assert state["import"]["imported_key_count"] == 318
    assert len(remaining) == 8200 - 318
    assert len(json.loads((dest / "checkpoint.json").read_text())["completed"]) == 318
    assert len((dest / "manifest.jsonl").read_text().splitlines()) == 318
    assert len(list((dest / "raw").glob("*.raw"))) == state["import"]["raw_reference_count"]
    assert not (source / "cache").exists()


def test_import_rejects_incompatible_worklist_and_policy(tmp_path, monkeypatch):
    source = _selective_source(tmp_path, monkeypatch)
    checkpoint = source / "checkpoint.json"
    original = json.loads(checkpoint.read_text())
    for field, value in (("policy", "scripts/other_policy.py"), ("worklist_sha256", "0" * 64)):
        changed = dict(original, **{field: value})
        checkpoint.write_text(json.dumps(changed))
        with pytest.raises(RuntimeError, match="incompatible source"):
            opt.import_checkpoint(opt.load_fetcher(), source, tmp_path / field, START, END, 1, .3, 60, 1)
        checkpoint.write_text(json.dumps(original))


def test_import_rejects_source_raw_mutation(tmp_path, monkeypatch):
    source = _selective_source(tmp_path, monkeypatch)
    row = next(json.loads(x) for x in (source / "manifest.jsonl").read_text().splitlines() if x.strip())
    raw = source / row["accepted_reports"][0]["raw_path"]
    raw.write_bytes(raw.read_bytes() + b"mutation")
    with pytest.raises(RuntimeError, match="source raw reference hash/size mismatch"):
        opt.import_checkpoint(opt.load_fetcher(), source, tmp_path / "optimized", START, END, 1, .3, 60, 1)


def test_import_initialization_is_deterministic(tmp_path, monkeypatch):
    source = _selective_source(tmp_path, monkeypatch)
    _, state1, _, _ = opt.import_checkpoint(opt.load_fetcher(), source, tmp_path / "a", START, END, 4, .3, 60, 1)
    _, state2, _, _ = opt.import_checkpoint(opt.load_fetcher(), source, tmp_path / "b", START, END, 4, .3, 60, 1)
    assert state1 == state2
    assert (tmp_path / "a/manifest.jsonl").read_bytes() == (tmp_path / "b/manifest.jsonl").read_bytes()
    assert (tmp_path / "a/checkpoint.json").read_bytes() == (tmp_path / "b/checkpoint.json").read_bytes()
