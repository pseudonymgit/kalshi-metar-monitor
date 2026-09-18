"""
test_consensus_scoring.py — Unit tests for consensus_bonus_score, compute_pareto_front,
jaccard_convergence, and compute_jaccard_similarity.

Run with: pytest tests/test_consensus_scoring.py -v
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from core.consensus_scoring import (
    BASE_SCORE,
    BONUS_POOL,
    TOTAL_METRICS,
    compare_scoring_methods,
    compute_jaccard_similarity,
    compute_pareto_front,
    consensus_bonus_score,
    jaccard_convergence,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def all_pass_metrics() -> dict:
    """All five metrics comfortably above their thresholds."""
    return {
        "accuracy": 0.60,
        "sharpe": 0.8,
        "total_pnl": 100.0,
        "brier": 0.20,
        "max_drawdown": 0.3,
    }


@pytest.fixture
def all_fail_metrics() -> dict:
    """All five metrics below their thresholds."""
    return {
        "accuracy": 0.40,
        "sharpe": 0.2,
        "total_pnl": -50.0,
        "brier": 0.30,
        "max_drawdown": 0.6,
    }


@pytest.fixture
def mixed_metrics() -> dict:
    """Three pass (accuracy, total_pnl, max_drawdown), two fail (sharpe, brier)."""
    return {
        "accuracy": 0.58,  # pass (>= 0.57)
        "sharpe": 0.3,  # fail (< 0.5)
        "total_pnl": 10.0,  # pass (> 0)
        "brier": 0.28,  # fail (> 0.25, strict <)
        "max_drawdown": 0.4,  # pass (<= 0.5)
    }


@pytest.fixture
def sample_data_json() -> str:
    """Write a small set of configs to a temp JSON file and return the path."""
    configs = [
        {"id": "cfg_a", "accuracy": 0.60, "sharpe": 0.8, "total_pnl": 100.0, "brier": 0.20, "max_drawdown": 0.3},
        {"id": "cfg_b", "accuracy": 0.40, "sharpe": 0.2, "total_pnl": -50.0, "brier": 0.30, "max_drawdown": 0.6},
        {"id": "cfg_c", "accuracy": 0.58, "sharpe": 0.3, "total_pnl": 10.0, "brier": 0.28, "max_drawdown": 0.4},
    ]
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(configs, f)
    f.close()
    return f.name


# ── consensus_bonus_score tests ─────────────────────────────────────────────


class TestConsensusBonusScore:
    def test_all_metrics_pass(self, all_pass_metrics):
        """All five thresholds met → score = 0.5 + 0.5 * (5/5) = 1.0."""
        score = consensus_bonus_score(all_pass_metrics)
        expected = BASE_SCORE + BONUS_POOL * (5.0 / TOTAL_METRICS)
        assert score == pytest.approx(expected)
        assert score == 1.0

    def test_all_metrics_fail(self, all_fail_metrics):
        """None pass → score = 0.5 + 0.5 * (0/5) = 0.5."""
        score = consensus_bonus_score(all_fail_metrics)
        expected = BASE_SCORE + BONUS_POOL * (0.0 / TOTAL_METRICS)
        assert score == pytest.approx(expected)
        assert score == 0.5

    def test_mixed_metrics(self, mixed_metrics):
        """3 pass out of 5 → score = 0.5 + 0.5 * (3/5) = 0.8."""
        score = consensus_bonus_score(mixed_metrics)
        expected = BASE_SCORE + BONUS_POOL * (3.0 / TOTAL_METRICS)
        assert score == pytest.approx(expected)
        assert score == pytest.approx(0.8)

    def test_missing_metric_is_fail(self):
        """Missing key counts as fail (does not crash)."""
        metrics = {"accuracy": 0.60, "sharpe": 0.8, "total_pnl": 100.0, "brier": 0.20}
        score = consensus_bonus_score(metrics)  # missing max_drawdown → fail
        expected = BASE_SCORE + BONUS_POOL * (4.0 / TOTAL_METRICS)  # 4 of 5 pass
        assert score == pytest.approx(expected)

    def test_empty_metrics(self):
        """Empty dict → 0 of 5 pass → 0.5."""
        score = consensus_bonus_score({})
        assert score == pytest.approx(BASE_SCORE)

    def test_boundary_values(self):
        """Exactly at threshold — strict operators: total_pnl>0 fails at 0.0, brier<0.25 fails at 0.25."""
        metrics = {
            "accuracy": 0.57,        # >= 0.57 → pass
            "sharpe": 0.5,            # >= 0.5  → pass
            "total_pnl": 0.0,         # > 0     → fail (strict)
            "brier": 0.25,            # < 0.25  → fail (strict)
            "max_drawdown": 0.5,      # <= 0.5  → pass
        }
        # 3 pass → score = 0.5 + 0.5 * (3/5) = 0.8
        score = consensus_bonus_score(metrics)
        expected = BASE_SCORE + BONUS_POOL * (3.0 / TOTAL_METRICS)
        assert score == pytest.approx(expected)
        assert score == pytest.approx(0.8)

    def test_custom_thresholds(self):
        """Custom thresholds override defaults."""
        metrics = {"accuracy": 0.50, "sharpe": 0.4}
        custom = {"accuracy": 0.50, "sharpe": 0.4}  # both exactly at threshold
        score = consensus_bonus_score(metrics, thresholds=custom)
        # accuracy passes (>= 0.50), sharpe passes (>= 0.40)
        # total_pnl, brier, max_drawdown missing → fail
        expected = BASE_SCORE + BONUS_POOL * (2.0 / TOTAL_METRICS)
        assert score == pytest.approx(expected)


# ── compute_pareto_front tests ──────────────────────────────────────────────


class TestComputeParetoFront:
    def test_simple_dominance(self):
        """A dominates B, C is incomparable with A."""
        configs = [
            {"id": "A", "accuracy": 0.60, "sharpe": 1.0},
            {"id": "B", "accuracy": 0.40, "sharpe": 0.5},  # dominated by A
            {"id": "C", "accuracy": 0.55, "sharpe": 0.9},  # dominated by A (0.60>0.55, 1.0>0.9)
        ]
        # A dominates both B and C
        front = compute_pareto_front(configs, ["accuracy", "sharpe"])
        assert len(front) == 1
        assert front[0]["id"] == "A"

    def test_a_dominates_b_c_incomparable(self):
        """A dominates B. C is better than A on sharpe but worse on accuracy."""
        configs = [
            {"id": "A", "accuracy": 0.60, "sharpe": 0.8},
            {"id": "B", "accuracy": 0.40, "sharpe": 0.5},   # dominated by A
            {"id": "C", "accuracy": 0.55, "sharpe": 0.9},   # incomparable (A better acc, C better sharpe)
        ]
        front = compute_pareto_front(configs, ["accuracy", "sharpe"])
        front_ids = [c["id"] for c in front]
        assert "A" in front_ids
        assert "C" in front_ids
        assert "B" not in front_ids

    def test_all_incomparable(self):
        """No config dominates any other (trade-offs on different metrics)."""
        configs = [
            {"id": "X", "accuracy": 0.60, "sharpe": 0.5},
            {"id": "Y", "accuracy": 0.50, "sharpe": 0.8},
        ]
        front = compute_pareto_front(configs, ["accuracy", "sharpe"])
        assert len(front) == 2

    def test_all_dominated_by_one(self):
        """One config dominates all others."""
        configs = [
            {"id": "best", "accuracy": 0.90, "sharpe": 1.5},
            {"id": "mid",  "accuracy": 0.60, "sharpe": 0.8},
            {"id": "worst","accuracy": 0.30, "sharpe": 0.2},
        ]
        front = compute_pareto_front(configs, ["accuracy", "sharpe"])
        assert len(front) == 1
        assert front[0]["id"] == "best"

    def test_identical_configs(self):
        """Identical configs — neither dominates (no strictly better)."""
        configs = [
            {"id": "A", "accuracy": 0.60, "sharpe": 1.0},
            {"id": "B", "accuracy": 0.60, "sharpe": 1.0},
        ]
        front = compute_pareto_front(configs, ["accuracy", "sharpe"])
        assert len(front) == 2

    def test_single_config(self):
        """Single config is always on the front."""
        front = compute_pareto_front([{"id": "only", "acc": 0.5}], ["acc"])
        assert len(front) == 1

    def test_empty_configs(self):
        """Empty list returns empty list."""
        assert compute_pareto_front([], ["acc"]) == []

    def test_three_way_chain(self):
        """A > B > C, A dominates all."""
        configs = [
            {"id": "A", "accuracy": 0.60, "sharpe": 0.8, "total_pnl": 100},
            {"id": "B", "accuracy": 0.50, "sharpe": 0.6, "total_pnl": 50},
            {"id": "C", "accuracy": 0.40, "sharpe": 0.4, "total_pnl": 0},
        ]
        front = compute_pareto_front(configs, ["accuracy", "sharpe", "total_pnl"])
        assert len(front) == 1
        assert front[0]["id"] == "A"


# ── compute_jaccard_similarity tests ─────────────────────────────────────────


class TestComputeJaccardSimilarity:
    def test_full_overlap(self):
        assert compute_jaccard_similarity({"a", "b", "c"}, {"a", "b", "c"}) == pytest.approx(1.0)

    def test_no_overlap(self):
        assert compute_jaccard_similarity({"a", "b", "c"}, {"d", "e", "f"}) == pytest.approx(0.0)

    def test_partial_overlap(self):
        # {"a","b","c"} ∩ {"b","c","d"} = {"b","c"} → 2/4 = 0.5
        sim = compute_jaccard_similarity({"a", "b", "c"}, {"b", "c", "d"})
        assert sim == pytest.approx(0.5)

    def test_one_empty(self):
        assert compute_jaccard_similarity(set(), {"a", "b"}) == pytest.approx(0.0)

    def test_both_empty(self):
        assert compute_jaccard_similarity(set(), set()) == pytest.approx(1.0)

    def test_subset(self):
        # {"a","b"} ∩ {"a","b","c"} = {"a","b"} / {"a","b","c"} = 2/3
        sim = compute_jaccard_similarity({"a", "b"}, {"a", "b", "c"})
        assert sim == pytest.approx(2.0 / 3.0)


# ── jaccard_convergence tests ────────────────────────────────────────────────


class TestJaccardConvergence:
    def test_full_overlap_converges(self):
        """3 identical top-5 sets → J(0,1) = 1.0, J(1,2) = 1.0 → converged."""
        history = [
            ["a", "b", "c", "d", "e"],
            ["a", "b", "c", "d", "e"],
            ["a", "b", "c", "d", "e"],
        ]
        converged, jaccard = jaccard_convergence(
            history, jaccard_threshold=0.80, consecutive_windows=2
        )
        assert converged is True
        assert jaccard == pytest.approx(1.0)

    def test_no_overlap_does_not_converge(self):
        """Completely disjoint sets → J = 0.0."""
        history = [
            ["a", "b", "c", "d", "e"],
            ["f", "g", "h", "i", "j"],
        ]
        converged, jaccard = jaccard_convergence(
            history, jaccard_threshold=0.80, consecutive_windows=2
        )
        assert converged is False
        assert jaccard == pytest.approx(0.0)

    def test_partial_overlap_not_enough(self):
        """3 consecutive pair J=0.6 < 0.80 threshold → not converged."""
        history = [
            ["a", "b", "c", "d", "e"],
            ["a", "b", "c", "f", "g"],
            ["a", "b", "c", "h", "i"],
        ]
        converged, _ = jaccard_convergence(
            history, jaccard_threshold=0.80, consecutive_windows=2
        )
        assert converged is False

    def test_partial_overlap_meets_threshold(self):
        """Overlap of 4/5 elements between two 5-element sets = 4/6 = 0.667."""
        history = [
            ["a", "b", "c", "d", "e"],
            ["a", "b", "c", "d", "f"],  # 4 shared / 6 union = 0.667
        ]
        converged, jaccard = jaccard_convergence(
            history, jaccard_threshold=0.60, consecutive_windows=1
        )
        assert converged is True
        assert jaccard == pytest.approx(4.0 / 6.0)

    def test_single_window_no_convergence(self):
        """Only one window → can't compute overlap → no convergence."""
        converged, jaccard = jaccard_convergence(
            [["a", "b", "c", "d", "e"]], jaccard_threshold=0.80, consecutive_windows=1
        )
        assert converged is False
        assert jaccard == pytest.approx(1.0)

    def test_consecutive_windows_requirement(self):
        """Need 3 consecutive high-overlap pairs; streak broken → not converged."""
        history = [
            ["a", "b", "c", "d", "e"],
            ["a", "b", "c", "d", "e"],
            ["a", "b", "c", "d", "e"],
            ["x", "y", "z", "w", "v"],  # disrupts the streak
        ]
        converged, last_jaccard = jaccard_convergence(
            history, jaccard_threshold=0.80, consecutive_windows=3
        )
        # Pairs: (0,1)=1.0, (1,2)=1.0, (2,3)=0.0 → trailing streak = 0
        assert converged is False
        assert last_jaccard == pytest.approx(0.0)

    def test_strictly_more_than_consecutive(self):
        """More than enough consecutive windows → still converges."""
        history = [
            ["a", "b", "c", "d", "e"],
            ["a", "b", "c", "d", "e"],
            ["a", "b", "c", "d", "e"],
            ["a", "b", "c", "d", "e"],
        ]
        converged, _ = jaccard_convergence(
            history, jaccard_threshold=0.80, consecutive_windows=3
        )
        # 4 windows → 3 pairs, all 1.0
        assert converged is True

    def test_window_fewer_than_5_ids(self):
        """Windows with fewer than 5 IDs should still work."""
        history = [
            ["a", "b", "c"],  # top-3
            ["a", "b", "c"],
            ["a", "b", "c"],
        ]
        converged, _ = jaccard_convergence(
            history, jaccard_threshold=0.80, consecutive_windows=2
        )
        assert converged is True


# ── compare_scoring_methods test ─────────────────────────────────────────────


class TestCompareScoringMethods:
    def test_writes_output_file(self, sample_data_json):
        """compare_scoring_methods should produce a valid JSON output file."""
        out_fd, out_path = tempfile.mkstemp(suffix=".json")
        Path(out_path).unlink(missing_ok=True)
        try:
            compare_scoring_methods(sample_data_json, out_path)
            with open(out_path, "r") as f:
                report = json.load(f)
            assert "consensus_bonus" in report
            assert "scalarised_weighted_sum" in report
            assert "pareto_front_ids" in report
            assert "analysis" in report
            assert report["consensus_bonus"]["top_config_id"] == "cfg_a"
        finally:
            Path(sample_data_json).unlink(missing_ok=True)
            Path(out_path).unlink(missing_ok=True)

    def test_empty_data(self):
        """Empty data produces an error report, not a crash."""
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump([], f)
        f.close()
        out_path = tempfile.mktemp(suffix=".json")
        try:
            compare_scoring_methods(f.name, out_path)
            with open(out_path, "r") as r:
                report = json.load(r)
            assert "error" in report
        finally:
            Path(f.name).unlink(missing_ok=True)
            Path(out_path).unlink(missing_ok=True)


# ── Integration / Smoke test ─────────────────────────────────────────────────


def test_end_to_end_smoke():
    """
    Smoke test: run consensus_bonus_score, compute_pareto_front,
    and jaccard_convergence together with realistic-looking data.
    """
    configs = [
        {"id": "v1", "accuracy": 0.62, "sharpe": 0.9, "total_pnl": 200, "brier": 0.18, "max_drawdown": 0.25},
        {"id": "v2", "accuracy": 0.55, "sharpe": 0.4, "total_pnl": 80,  "brier": 0.30, "max_drawdown": 0.35},
        {"id": "v3", "accuracy": 0.58, "sharpe": 0.6, "total_pnl": 150, "brier": 0.22, "max_drawdown": 0.45},
        {"id": "v4", "accuracy": 0.48, "sharpe": 0.3, "total_pnl": -10, "brier": 0.27, "max_drawdown": 0.55},
        {"id": "v5", "accuracy": 0.60, "sharpe": 0.7, "total_pnl": 120, "brier": 0.24, "max_drawdown": 0.30},
    ]

    # Consensus bonus scores
    scores = [consensus_bonus_score(c) for c in configs]
    top_idx = max(range(len(scores)), key=lambda i: scores[i])
    # v1 should rank highest (all 5 pass)
    assert configs[top_idx]["id"] == "v1"

    # Pareto front — negate lower-is-better metrics for dominance check
    pareto_keys = ["accuracy", "sharpe", "total_pnl", "brier", "max_drawdown"]
    neg_configs = [
        {"id": c["id"], **{k: v for k, v in c.items() if k != "id"},
         "brier": -c["brier"], "max_drawdown": -c["max_drawdown"]}
        for c in configs
    ]
    front = compute_pareto_front(neg_configs, pareto_keys)
    assert len(front) == 1
    assert front[0]["id"] == "v1"

    # Jaccard convergence — stable top-5 across windows
    stable_history = [
        ["v1", "v3", "v5", "v2", "v4"],
        ["v1", "v3", "v5", "v2", "v4"],
        ["v1", "v3", "v5", "v2", "v4"],
    ]
    converged, jac = jaccard_convergence(stable_history, jaccard_threshold=0.80, consecutive_windows=2)
    assert converged is True
    assert jac == pytest.approx(1.0)

    # Unstable — no convergence
    unstable = [
        ["v1", "v3", "v5", "v2", "v4"],
        ["v8", "v9", "v1", "v3", "v5"],
        ["v4", "v2"],
    ]
    converged, _ = jaccard_convergence(unstable, jaccard_threshold=0.80, consecutive_windows=2)
    assert converged is False