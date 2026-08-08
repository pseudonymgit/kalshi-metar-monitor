#!/usr/bin/env python3
"""
bayesian_cascade.py — Maintains running posterior over temperature buckets.

Architecture per Gray Room spec (2026-08-08):
  Trajectory updates the prior daily (epoch-based analog matching).
  Nowcasting updates intraday when a front is detected (event-driven).
  Each new METAR observation narrows the distribution ("probabilistic walk
  forward" / particle filter mechanics).

  GEFS ensemble fraction → Trajectory update (daily) → Nowcasting update
  (intraday) → Final EV for position sizing.

Interface:
  class BayesianCascade:
    def daily_update(self, station, target_date, gefs_distribution) -> np.array
    def nowcast_update(self, station, nowcast_shift) -> np.array
    def get_final_ev(self, station, payoff_matrix) -> float
    def get_posterior(self, station) -> Optional[np.array]
    def clear_station(self, station) -> None
    def cache_state(self) -> None
    def load_state(self) -> None

B-Mode compliant. No AI/ML.
"""

import json
import logging
import math
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

from core.nowcasting_lane import NowcastingLane, NowcastShift

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CASCADE_DB = os.path.join(REPO_ROOT, "data", "bayesian_cascade.db")

# Default temperature bucket labels (matching Kalshi market structure)
DEFAULT_BUCKET_LABELS = [
    "below_30", "30-35", "35-40",
    "40-45", "45-50", "50-55", "above_55"
]

CASCADE_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS posterior_state (
    station TEXT NOT NULL,
    target_date TEXT NOT NULL,
    posterior_json TEXT NOT NULL,
    prior_source TEXT DEFAULT 'gefs',
    trajectory_updated INTEGER DEFAULT 0,
    nowcast_updated INTEGER DEFAULT 0,
    last_updated TEXT NOT NULL,
    PRIMARY KEY (station, target_date)
);
CREATE INDEX IF NOT EXISTS idx_posterior_date ON posterior_state(target_date);
"""


def get_db(db_path: str = DEFAULT_CASCADE_DB) -> sqlite3.Connection:
    """Get connection with WAL mode."""
    os.makedirs(os.path.dirname(db_path) or '.', exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Initialize schema."""
    conn.executescript(CASCADE_DB_SCHEMA)
    conn.commit()


class BayesianCascade:
    """
    Maintains a running posterior distribution over temperature buckets.

    The posterior evolves through three stages:
      1. GEFS prior (baseline ensemble fraction)
      2. Trajectory update (daily epoch-based analog matching)
      3. Nowcasting update (event-driven intraday)

    Updates are multiplicative in probability space:
      P_post(bucket) ∝ P_prior(bucket) × L_traj(bucket) × L_nowcast(bucket)
    """

    def __init__(self, nowcasting_lane: Optional[NowcastingLane] = None,
                 cascade_db: str = DEFAULT_CASCADE_DB,
                 bucket_labels: Optional[List[str]] = None):
        self._nowcasting_lane = nowcasting_lane or NowcastingLane()
        self._cascade_db = cascade_db
        self._bucket_labels = bucket_labels or DEFAULT_BUCKET_LABELS
        self._conn: Optional[sqlite3.Connection] = None

        # In-memory posterior cache: {station: {target_date: np.array}}
        self._posteriors: Dict[str, Dict[str, np.ndarray]] = {}

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = get_db(self._cascade_db)
            init_db(self._conn)
        return self._conn

    # ── Core Update Methods ──

    def daily_update(self, station: str, target_date: str,
                     prior_distribution: np.ndarray,
                     trajectory_likelihood: Optional[np.ndarray] = None) -> np.ndarray:
        """
        Daily trajectory update.

        Args:
            station: ICAO station code
            target_date: Settlement date YYYY-MM-DD
            prior_distribution: GEFS-based prior over buckets (np.array, len=n_buckets)
                Must sum to 1.0 (or will be normalized).
            trajectory_likelihood: Optional likelihood from trajectory gate.
                If None, posterior = prior (trajectory is neutral).

        Returns:
            Posterior distribution (np.array, sums to 1.0)
        """
        if station not in self._posteriors:
            self._posteriors[station] = {}
        if target_date not in self._posteriors[station]:
            self._posteriors[station][target_date] = None

        # Normalize prior
        prior = prior_distribution.copy().astype(np.float64)
        prior_sum = prior.sum()
        if prior_sum <= 0:
            prior = np.ones(len(prior)) / len(prior)
        else:
            prior = prior / prior_sum

        if trajectory_likelihood is not None:
            traj = trajectory_likelihood.copy().astype(np.float64)
            traj_sum = traj.sum()
            if traj_sum > 0:
                traj = traj / traj_sum
                posterior = prior * traj
            else:
                posterior = prior.copy()
        else:
            posterior = prior.copy()

        # Re-normalize
        post_sum = posterior.sum()
        if post_sum > 0:
            posterior = posterior / post_sum

        self._posteriors[station][target_date] = posterior
        self._persist_posterior(station, target_date, posterior,
                                 source="trajectory" if trajectory_likelihood is not None else "gefs")
        return posterior

    def nowcast_update(self, station: str, target_date: str) -> Optional[np.ndarray]:
        """
        Intraday nowcasting update.

        Checks the nowcasting lane for active front detections at this station.
        If a front is detected, applies the probability-mass shift to the
        current posterior.

        Args:
            station: ICAO station code
            target_date: Settlement date YYYY-MM-DD

        Returns:
            Updated posterior, or None if no active nowcasting
        """
        key = self._get_posterior_key(station, target_date)
        if key is None:
            return None

        posterior = self._posteriors[station][target_date]
        if posterior is None:
            return None

        # Get nowcasting shift
        prob_shift = self._nowcasting_lane.get_prob_shift(station,
            prior_distribution={l: 0.0 for l in self._bucket_labels})

        if not prob_shift:
            return posterior  # No active nowcasting

        # Apply shift: P_post(b) ∝ P_prior(b) × (1 + shift[b])
        shifted = posterior.copy()
        for i, label in enumerate(self._bucket_labels):
            if i >= len(shifted):
                break
            delta = prob_shift.get(label, 0.0)
            shifted[i] *= (1.0 + delta)

        # Re-normalize
        shifted_sum = shifted.sum()
        if shifted_sum > 0:
            shifted = shifted / shifted_sum
        else:
            shifted = posterior.copy()

        self._posteriors[station][target_date] = shifted
        self._persist_posterior(station, target_date, shifted, source="nowcast")

        logger.info(
            "BayesianCascade: nowcast update for %s %s — prior_sum=%.3f shift_sum=%.3f",
            station, target_date, posterior.sum(),
            sum(prob_shift.values()),
        )

        return shifted

    # ── EV / Final Output ──

    def get_final_ev(self, station: str, target_date: str,
                     payoff_matrix: np.ndarray) -> Optional[float]:
        """
        Compute expected value from the posterior.

        EV = Σ P(bucket_i) × payoff_i

        Args:
            station: ICAO station code
            target_date: Settlement date YYYY-MM-DD
            payoff_matrix: Array of payoffs per bucket (same length as posterior)

        Returns:
            Expected value, or None if no posterior exists
        """
        posterior = self.get_posterior(station, target_date)
        if posterior is None:
            return None

        if len(posterior) != len(payoff_matrix):
            logger.error(
                "Mismatch: posterior len=%d, payoff len=%d",
                len(posterior), len(payoff_matrix),
            )
            return None

        return float(np.dot(posterior, payoff_matrix))

    def get_posterior(self, station: str, target_date: str) -> Optional[np.ndarray]:
        """Get the current posterior distribution."""
        if station in self._posteriors and target_date in self._posteriors[station]:
            return self._posteriors[station][target_date]
        return None

    def clear_station(self, station: str, target_date: Optional[str] = None) -> None:
        """Clear posterior for a station, optionally for a specific date."""
        if station not in self._posteriors:
            return
        if target_date:
            self._posteriors[station].pop(target_date, None)
        else:
            self._posteriors[station].clear()
        self._clear_db_posterior(station, target_date)

    # ── Persistence ──

    def cache_state(self) -> None:
        """Persist all in-memory posteriors to DB."""
        conn = self._get_conn()
        for station, dates in self._posteriors.items():
            for target_date, posterior in dates.items():
                if posterior is not None:
                    self._persist_posterior(conn, station, target_date, posterior)
        conn.commit()

    def load_state(self) -> int:
        """Load posteriors from DB into memory. Returns count loaded."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT station, target_date, posterior_json FROM posterior_state"
        ).fetchall()
        count = 0
        for r in rows:
            try:
                arr = np.array(json.loads(r["posterior_json"]), dtype=np.float64)
                station = r["station"]
                date = r["target_date"]
                if station not in self._posteriors:
                    self._posteriors[station] = {}
                self._posteriors[station][date] = arr
                count += 1
            except (json.JSONDecodeError, TypeError):
                pass
        return count

    def close(self) -> None:
        """Flush state and close connections."""
        self.cache_state()
        if self._nowcasting_lane:
            self._nowcasting_lane.close()
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── Internal ──

    def _get_posterior_key(self, station: str, target_date: str) -> Optional[str]:
        if station in self._posteriors and target_date in self._posteriors[station]:
            if self._posteriors[station][target_date] is not None:
                return f"{station}/{target_date}"
        return None

    def _persist_posterior(self, station: str, target_date: str,
                            posterior: np.ndarray, source: str = "nowcast") -> None:
        conn = self._get_conn()
        conn.execute("""
            INSERT OR REPLACE INTO posterior_state
            (station, target_date, posterior_json, prior_source,
             trajectory_updated, nowcast_updated, last_updated)
            VALUES (?,?,?,?,?,?,?)
        """, (
            station,
            target_date,
            json.dumps(posterior.tolist()),
            source,
            1 if source == "trajectory" else 0,
            1 if source == "nowcast" else 0,
            datetime.now(timezone.utc).isoformat(),
        ))
        conn.commit()

    def _clear_db_posterior(self, station: str, target_date: Optional[str] = None) -> None:
        conn = self._get_conn()
        if target_date:
            conn.execute(
                "DELETE FROM posterior_state WHERE station=? AND target_date=?",
                (station, target_date),
            )
        else:
            conn.execute("DELETE FROM posterior_state WHERE station=?", (station,))
        conn.commit()


# ── Test ──

def test_bayesian_cascade():
    print("=" * 72)
    print("  BayesianCascade — Phase 2 Self-Test")
    print("=" * 72)

    cascade = BayesianCascade(cascade_db="/tmp/test_cascade.db")

    # Test daily_update with GEFS prior
    prior = np.array([0.1, 0.2, 0.35, 0.25, 0.08, 0.02, 0.0])
    posterior1 = cascade.daily_update("KNYC", "2026-08-10", prior)
    print(f"  Prior:          {np.round(prior, 3)}")
    print(f"  Posterior (traj): {np.round(posterior1, 3)} (unmodified = prior)")

    # Test daily_update with trajectory likelihood
    traj_likelihood = np.array([0.3, 0.25, 0.2, 0.15, 0.07, 0.02, 0.01])
    posterior2 = cascade.daily_update("KNYC", "2026-08-10", prior, traj_likelihood)
    print(f"  Traj likelihood:  {np.round(traj_likelihood, 3)}")
    print(f"  Posterior (traj):  {np.round(posterior2, 3)}")

    # Test nowcast_update (no active nowcasting — should be no-op)
    posterior3 = cascade.nowcast_update("KNYC", "2026-08-10")
    if posterior3 is not None:
        print(f"  Posterior (nowcast): {np.round(posterior3, 3)} (no shift)")

    # Test get_final_ev
    payoffs = np.array([0.0, 0.3, 0.5, 0.7, 0.9, 1.0, 1.0])
    ev = cascade.get_final_ev("KNYC", "2026-08-10", payoffs)
    if ev is not None:
        print(f"  Expected value: {ev:.4f}")

    # Test cache + load
    cascade.cache_state()
    loaded = cascade.load_state()
    print(f"  State loaded from DB: {loaded} entries")

    # Test clear
    cascade.clear_station("KNYC")
    post_clear = cascade.get_posterior("KNYC", "2026-08-10")
    print(f"  After clear — posterior: {post_clear}")

    cascade.close()

    import os
    try:
        os.remove("/tmp/test_cascade.db")
    except:
        pass

    print("\n  ✅ Phase 2 self-test complete")


if __name__ == "__main__":
    test_bayesian_cascade()