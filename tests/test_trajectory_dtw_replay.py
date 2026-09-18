import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("trajectory_dtw_replay", ROOT / "scripts" / "trajectory_dtw_replay.py")
MOD = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules["trajectory_dtw_replay"] = MOD
SPEC.loader.exec_module(MOD)


def test_dtw_known_path_and_recurrence():
    distance, path = MOD.dtw_distance([[0.0], [1.0]], [[0.0], [0.0], [1.0]], radius=1)
    assert path == [(1, 1), (1, 2), (2, 3)]
    assert distance == pytest.approx(0.0)


def test_dtw_rejects_unreachable_band():
    distance, path = MOD.dtw_distance([[0.0], [1.0], [2.0]], [[0.0]], radius=1)
    assert distance == float("inf")
    assert path == []


def test_causal_cutoff_excludes_future_observation():
    obs = [
        MOD.Observation(1, "KATL", "2025-08-25", "2025-08-25T05:00:00Z", 70.0, 60.0, 1000.0),
        MOD.Observation(2, "KATL", "2025-08-25", "2025-08-25T12:00:00Z", 90.0, 60.0, 1000.0),
    ]
    got = MOD.aggregate_day(obs, "KATL", "2025-08-25", MOD._parse_dt("2025-08-25T06:00:00Z"))
    assert got["temp_max_f"] == 70.0


def test_no_target_day_leakage_in_partial_query():
    obs = [
        MOD.Observation(1, "KATL", "2025-08-21", "2025-08-21T00:00:00Z", 70.0, 60.0, 1000.0),
        MOD.Observation(2, "KATL", "2025-08-22", "2025-08-22T00:00:00Z", 71.0, 60.0, 1000.0),
        MOD.Observation(3, "KATL", "2025-08-23", "2025-08-23T00:00:00Z", 72.0, 60.0, 1000.0),
        MOD.Observation(4, "KATL", "2025-08-24", "2025-08-24T00:00:00Z", 73.0, 60.0, 1000.0),
        MOD.Observation(5, "KATL", "2025-08-25", "2025-08-25T05:00:00Z", 74.0, 60.0, 1000.0),
        MOD.Observation(6, "KATL", "2025-08-25", "2025-08-25T06:01:00Z", 99.0, 60.0, 1000.0),
    ]
    seq = MOD.sequence_for(obs, "KATL", "2025-08-25", MOD._parse_dt("2025-08-25T06:00:00Z"))
    assert seq is not None
    assert seq[1][-1]["temp_max_f"] == 74.0


def test_candidate_eligibility_boundaries_are_frozen():
    # Query window starts 2025-08-21; 2025-08-20 is eligible, 2025-08-21 overlaps.
    query_start = MOD.date.fromisoformat("2025-08-25") - MOD.timedelta(days=4)
    assert MOD.date.fromisoformat("2025-08-20") < query_start
    assert not (MOD.date.fromisoformat("2025-08-21") < query_start)


def test_sequential_idempotence_and_revision_error():
    state = MOD.SequentialReplayState()
    row = {"event_id": "KATL|HIGH|2025-08-25", "cutoff": "06:00:00Z", "feature_set": "temp_only", "x": 1}
    first = state.apply(row)
    second = state.apply(copy.deepcopy(row))
    assert first == second
    assert state.count == 1
    changed = dict(row, x=2)
    with pytest.raises(MOD.ReplayQCError, match="REVISION_ERROR"):
        state.apply(changed)


def test_probability_normalization_and_abstention():
    artifact = MOD.replay_fixture()
    assert artifact["rows"]
    for row in artifact["rows"]:
        if row["abstained"]:
            assert row["bucket_probabilities"] is None
            assert row["abstention_reason"]
        else:
            assert sum(row["bucket_probabilities"].values()) == pytest.approx(1.0)
            assert set(map(int, row["bucket_probabilities"])) == set(MOD.BUCKETS)
    abstained = MOD._abstained_row(("e", "06:00:00Z", "temp_only"), "2025-08-25", MOD._parse_dt("2025-08-25T06:00:00Z"), "temp_only", "ABSTAIN_INSUFFICIENT_ANALOGS")
    assert abstained["abstained"] is True


def test_deterministic_ties():
    a = MOD.Settlement(1, "KATL", "HIGH", "2020-01-01", 70)
    b = MOD.Settlement(2, "KATL", "HIGH", "2020-01-02", 71)
    matches = [{"candidate_id": MOD._candidate_id(b), "distance": 1.0}, {"candidate_id": MOD._candidate_id(a), "distance": 1.0}]
    matches.sort(key=lambda m: (m["distance"], m["candidate_id"]))
    assert [m["candidate_id"] for m in matches] == [MOD._candidate_id(a), MOD._candidate_id(b)]


def test_fixture_rerun_byte_identity(tmp_path):
    a = MOD.replay_fixture()
    b = MOD.replay_fixture()
    first = MOD.canonical_bytes(a) + b"\n"
    second = MOD.canonical_bytes(b) + b"\n"
    assert first == second
    p = tmp_path / "fixture.json"
    digest = MOD.write_artifact(a, p)
    assert digest == hashlib.sha256(first).hexdigest()
    assert p.read_bytes() == first


def test_fixture_has_one_row_per_identity():
    artifact = MOD.replay_fixture()
    keys = [(r["event_id"], r["cutoff"], r["feature_set"]) for r in artifact["rows"]]
    assert len(keys) == len(set(keys))
    assert len(keys) == len(MOD.FIXTURE_TARGET_DATES) * len(MOD.FIXTURE_CUTOFFS) * len(MOD.FEATURE_SETS)
