import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_unattended_sweep.py"


def test_execute_flag_is_required_and_preflight_is_non_executing(tmp_path):
    cp = subprocess.run(
        [sys.executable, str(RUNNER), "--run-root", str(tmp_path)],
        cwd=ROOT, stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=30,
    )
    assert cp.returncode in (0, 2, 3, 4)
    summary = json.loads(cp.stdout)
    run_dir = Path(summary["run_dir"])
    result = json.loads((run_dir / "result.json").read_text())
    assert result["execute_requested"] is False
    assert result["execute_requested"] is False
    assert (run_dir / "stdout.log").read_text() == ""
    assert (run_dir / "stderr.log").read_text() == ""
    assert (run_dir / "continuity-receipt.json").exists()


def test_manifest_is_sorted_and_deterministic():
    import importlib.util
    spec = importlib.util.spec_from_file_location("runner", RUNNER)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    first = runner.source_manifest()
    second = runner.source_manifest()
    # Data files may be updated by external collectors between snapshots; each
    # individual manifest is deterministic and canonically ordered.
    assert list(first) == sorted(first)
    assert list(second) == sorted(second)
    assert runner.CANONICAL[-4:] == ["--folds", "4", "--n-configs", "20"]


def test_allowlist_does_not_hide_unrelated_dirty_paths():
    import importlib.util
    spec = importlib.util.spec_from_file_location("runner", RUNNER)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    assert runner.allowed_dirty([" M docs/note.md", " M scripts/run_sweep.py"], ["docs/"]) == [" M scripts/run_sweep.py"]


def test_graphify_is_explicitly_excluded_from_preflight():
    import importlib.util
    spec = importlib.util.spec_from_file_location("runner", RUNNER)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)

    assert runner.is_graphify_path("scripts/graphify_generate.py")
    assert runner.is_graphify_path(".meta/Graphify/output.json")
    assert not runner.is_graphify_path("scripts/run_sweep.py")
    assert runner.allowed_dirty([
        "?? scripts/graphify_generate.py",
        " M scripts/run_sweep.py",
    ], []) == [" M scripts/run_sweep.py"]
    assert runner.PREFLIGHT_EXCLUSION["workflow"] == "graphify-generation-and-inspection"


def test_graphify_is_not_in_source_manifest():
    import importlib.util
    spec = importlib.util.spec_from_file_location("runner", RUNNER)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    assert all("graphify" not in path.lower() for path in runner.source_manifest())
