#!/usr/bin/env python3
"""Standalone, causal sequential DTW trajectory replay evaluator.

This module deliberately contains no live, trading, network, model, k-NN, or
Euclidean fallback path.  It is a small deterministic research fixture runner.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = REPO_ROOT / "data" / "metar_backfill.db"
DEFAULT_OUTPUT = REPO_ROOT / "data" / "trajectory_dtw_replay_fixture_20260815.json"
FEATURE_SETS: tuple[str, ...] = ("temp_only", "temp_humidity", "temp_humidity_pressure")
FEATURES: Mapping[str, tuple[str, ...]] = {
    "temp_only": ("temp_max_f",),
    "temp_humidity": ("temp_max_f", "rh_pct"),
    "temp_humidity_pressure": ("temp_max_f", "rh_pct", "pressure_mb"),
}
FEATURE_WEIGHTS = {name: tuple(1.0 / len(cols) for _ in cols) for name, cols in FEATURES.items()}
BUCKETS = tuple(range(-50, 131))
SEQUENCE_LENGTH = 5
DTW_RADIUS = 1
MIN_ANALOGS = 3
MAX_ANALOGS = 20
FIXTURE_STATION = "KATL"
FIXTURE_MARKET = "HIGH"
FIXTURE_TARGET_DATES = ("2025-08-25", "2025-08-26", "2025-08-27")
FIXTURE_CUTOFFS = ("06:00:00Z", "12:00:00Z", "18:00:00Z", "23:00:00Z")


class ReplayQCError(ValueError):
    """Fail-closed QC error."""


@dataclass(frozen=True)
class Observation:
    row_id: int
    station: str
    day: str
    timestamp: str
    temp_f: float | None
    dewpoint_f: float | None
    pressure_mb: float | None


@dataclass(frozen=True)
class Settlement:
    row_id: int
    station: str
    market_type: str
    target_date: str
    bucket: int


def canonical_bytes(value: Any) -> bytes:
    """Canonical JSON bytes: sorted, compact UTF-8, finite six-place floats."""
    return json.dumps(_canonicalize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _canonicalize(value: Any) -> Any:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ReplayQCError("non-finite value in canonical artifact")
        return float(f"{value:.6f}")
    if isinstance(value, Mapping):
        return {str(k): _canonicalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonicalize(v) for v in value]
    return value


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _day_end(day: str) -> datetime:
    return _parse_dt(f"{day}T23:59:59Z")


def _rh(temp_f: float, dewpoint_f: float) -> float:
    # Fixed Magnus relation, with Fahrenheit converted to Celsius.
    t = (temp_f - 32.0) * 5.0 / 9.0
    d = (dewpoint_f - 32.0) * 5.0 / 9.0
    a, b = 17.625, 243.04
    gamma_t = a * t / (b + t)
    gamma_d = a * d / (b + d)
    return max(0.0, min(100.0, 100.0 * math.exp(gamma_d - gamma_t)))


def load_source(db_path: Path, station: str, market_type: str) -> tuple[list[Observation], list[Settlement]]:
    """Load immutable source rows and reject duplicate identities."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        observations = [
            Observation(int(r["id"]), r["station"], r["date_utc"], r["timestamp_utc"], r["temp_f"], r["dewpoint_f"], r["pressure_mb"])
            for r in conn.execute(
                """SELECT id, station, date_utc, timestamp_utc, temp_f, dewpoint_f, pressure_mb
                   FROM metar_observations WHERE station=? ORDER BY date_utc, timestamp_utc, id""", (station,)
            )
        ]
        settlements = [
            Settlement(int(r["id"]), r["station"], r["market_type"], r["local_trading_date"], int(r["settlement_bucket"]))
            for r in conn.execute(
                """SELECT id, station, market_type, local_trading_date, settlement_bucket
                   FROM settlement_epochs WHERE station=? AND market_type=? AND epoch_status='closed'
                     AND terminal_state_reached=1 ORDER BY local_trading_date, id""", (station, market_type)
            )
        ]
    finally:
        conn.close()
    _assert_unique(((o.station, o.day, o.timestamp) for o in observations), "observation identity")
    _assert_unique(((s.station, s.market_type, s.target_date) for s in settlements), "settlement identity")
    return observations, settlements


def _assert_unique(keys: Iterable[tuple[Any, ...]], label: str) -> None:
    seen: set[tuple[Any, ...]] = set()
    for key in keys:
        if key in seen:
            raise ReplayQCError(f"DUPLICATE_IDENTITY: {label}: {key}")
        seen.add(key)


def _eligible_observations(observations: Sequence[Observation] | Mapping[str, Sequence[Observation]], station: str, day: str, cutoff: datetime) -> list[Observation]:
    source = observations.get(day, ()) if isinstance(observations, Mapping) else observations
    return [o for o in source if o.station == station and _parse_dt(o.timestamp) <= cutoff]


def aggregate_day(observations: Sequence[Observation] | Mapping[str, Sequence[Observation]], station: str, day: str, cutoff: datetime) -> dict[str, float] | None:
    rows = _eligible_observations(observations, station, day, cutoff)
    valid_temp = [float(o.temp_f) for o in rows if o.temp_f is not None and -50 <= float(o.temp_f) <= 130]
    if not valid_temp:
        return None
    valid_rh = [_rh(float(o.temp_f), float(o.dewpoint_f)) for o in rows if o.temp_f is not None and o.dewpoint_f is not None and -50 <= float(o.temp_f) <= 130 and -50 <= float(o.dewpoint_f) <= 130]
    valid_pressure = [float(o.pressure_mb) for o in rows if o.pressure_mb is not None and 800 <= float(o.pressure_mb) <= 1100]
    out: dict[str, float] = {"temp_max_f": max(valid_temp)}
    if valid_rh:
        out["rh_pct"] = sum(valid_rh) / len(valid_rh)
    if valid_pressure:
        out["pressure_mb"] = sum(valid_pressure) / len(valid_pressure)
    return out


def sequence_for(observations: Sequence[Observation] | Mapping[str, Sequence[Observation]], station: str, end_day: str, cutoff: datetime) -> tuple[list[str], list[dict[str, float]]] | None:
    end = date.fromisoformat(end_day)
    days = [(end - timedelta(days=i)).isoformat() for i in range(SEQUENCE_LENGTH - 1, -1, -1)]
    rows = [aggregate_day(observations, station, day, cutoff) for day in days]
    if any(row is None for row in rows):
        return None
    return days, [row for row in rows if row is not None]


def _scaled(sequence: Sequence[Mapping[str, float]], stats: Mapping[str, tuple[float, float]], feature_set: str) -> list[list[float]]:
    return [[(float(row[name]) - stats[name][0]) / stats[name][1] for name in FEATURES[feature_set]] for row in sequence]


def fit_stats(sequences: Sequence[Sequence[Mapping[str, float]]], feature_set: str) -> dict[str, tuple[float, float]]:
    stats: dict[str, tuple[float, float]] = {}
    for name in FEATURES[feature_set]:
        values = [float(row[name]) for seq in sequences for row in seq]
        if not values:
            raise ReplayQCError(f"no scaling values for {feature_set}/{name}")
        mean = sum(values) / len(values)
        variance = sum((v - mean) ** 2 for v in values) / len(values)
        stats[name] = (mean, math.sqrt(variance) or 1.0)
    return stats


def _local_cost(a: Sequence[float], b: Sequence[float], weights: Sequence[float]) -> float:
    return math.sqrt(sum(w * (x - y) ** 2 for x, y, w in zip(a, b, weights)))


def dtw_distance(query: Sequence[Sequence[float]], candidate: Sequence[Sequence[float]], radius: int = DTW_RADIUS) -> tuple[float, list[tuple[int, int]]]:
    """Exact banded DTW, normalized by selected path length.

    Predecessor ties are resolved diagonal, up, left. The returned path uses
    one-based matrix coordinates for auditability.
    """
    n, m = len(query), len(candidate)
    if not n or not m or radius < abs(n - m):
        return math.inf, []
    weights = tuple(1.0 / len(query[0]) for _ in query[0])
    inf = float("inf")
    costs = [[inf] * (m + 1) for _ in range(n + 1)]
    lengths = [[0] * (m + 1) for _ in range(n + 1)]
    paths: list[list[list[tuple[int, int]] | None]] = [[None] * (m + 1) for _ in range(n + 1)]
    costs[0][0] = 0.0
    paths[0][0] = []
    for i in range(1, n + 1):
        for j in range(max(1, i - radius), min(m, i + radius) + 1):
            predecessors = ((i - 1, j - 1), (i - 1, j), (i, j - 1))
            best: tuple[float, int, int, int, list[tuple[int, int]]] | None = None
            for priority, (pi, pj) in enumerate(predecessors):
                if paths[pi][pj] is None:
                    continue
                candidate_value = (costs[pi][pj], priority, pi, pj, paths[pi][pj])
                if best is None or candidate_value[:4] < best[:4]:
                    best = candidate_value
            if best is None:
                continue
            costs[i][j] = _local_cost(query[i - 1], candidate[j - 1], weights) + best[0]
            lengths[i][j] = len(best[4]) + 1
            paths[i][j] = best[4] + [(i, j)]
    if paths[n][m] is None:
        return math.inf, []
    return costs[n][m] / lengths[n][m], paths[n][m]  # type: ignore[arg-type]


def _candidate_id(s: Settlement) -> str:
    return f"{s.station}|{s.market_type}|{s.target_date}"


def _cutoff(target_date: str, clock: str) -> datetime:
    return _parse_dt(f"{target_date}T{clock}")


def _target_bucket(settlements: Sequence[Settlement], target_date: str) -> int | None:
    matches = [s.bucket for s in settlements if s.target_date == target_date]
    if len(matches) > 1:
        raise ReplayQCError(f"DUPLICATE_TARGET_SETTLEMENT: {target_date}")
    return matches[0] if matches else None


def replay_fixture(db_path: Path = DEFAULT_DB) -> dict[str, Any]:
    observations, settlements = load_source(db_path, FIXTURE_STATION, FIXTURE_MARKET)
    observations_by_day: dict[str, list[Observation]] = {}
    for observation in observations:
        observations_by_day.setdefault(observation.day, []).append(observation)
    rows: list[dict[str, Any]] = []
    candidate_sequence_cache: dict[str, tuple[list[str], list[dict[str, float]]] | None] = {}
    rejection_counts: Counter[str] = Counter()
    seen_keys: set[tuple[str, str, str]] = set()
    for target_date in FIXTURE_TARGET_DATES:
        cutoff_end = _cutoff(target_date, FIXTURE_CUTOFFS[-1])
        query_built: dict[str, tuple[list[str], list[dict[str, float]]]] = {}
        for clock in FIXTURE_CUTOFFS:
            cutoff = _cutoff(target_date, clock)
            for feature_set in FEATURE_SETS:
                key = (f"{FIXTURE_STATION}|{FIXTURE_MARKET}|{target_date}", clock, feature_set)
                if key in seen_keys:
                    raise ReplayQCError(f"DUPLICATE_OUTPUT_KEY: {key}")
                seen_keys.add(key)
                q = sequence_for(observations_by_day, FIXTURE_STATION, target_date, cutoff)
                if q is None:
                    rejection_counts["ABSTAIN_MISSING_QUERY_EPOCH"] += 1
                    rows.append(_abstained_row(key, target_date, cutoff, feature_set, "ABSTAIN_MISSING_QUERY_EPOCH"))
                    continue
                query_days, query_seq = q
                query_built[feature_set] = q
                candidate_payloads: list[tuple[Settlement, list[str], list[dict[str, float]]]] = []
                query_start = date.fromisoformat(target_date) - timedelta(days=4)
                for settlement in settlements:
                    candidate_date = date.fromisoformat(settlement.target_date)
                    if settlement.target_date >= target_date:
                        rejection_counts["FUTURE_TARGET_SETTLEMENT"] += 1
                        continue
                    if candidate_date >= query_start:
                        rejection_counts["FUTURE_OR_OVERLAPPING_CANDIDATE"] += 1
                        continue
                    candidate_cutoff = _day_end(settlement.target_date)
                    if settlement.target_date not in candidate_sequence_cache:
                        candidate_sequence_cache[settlement.target_date] = sequence_for(observations_by_day, settlement.station, settlement.target_date, candidate_cutoff)
                    candidate_seq = candidate_sequence_cache[settlement.target_date]
                    if candidate_seq is None:
                        rejection_counts["INCOMPLETE_CANDIDATE_SEQUENCE"] += 1
                        continue
                    if any(name not in feature_row for feature_row in candidate_seq[1] for name in FEATURES[feature_set]):
                        rejection_counts["MISSING_CANDIDATE_FEATURE"] += 1
                        continue
                    candidate_payloads.append((settlement, candidate_seq[0], candidate_seq[1]))
                if len(candidate_payloads) < MIN_ANALOGS:
                    rejection_counts["ABSTAIN_INSUFFICIENT_ANALOGS"] += 1
                    rows.append(_abstained_row(key, target_date, cutoff, feature_set, "ABSTAIN_INSUFFICIENT_ANALOGS", sequence_days=query_days, target_bucket=_target_bucket(settlements, target_date)))
                    continue
                all_sequences = [query_seq] + [payload[2] for payload in candidate_payloads]
                stats = fit_stats(all_sequences, feature_set)
                scaled_query = _scaled(query_seq, stats, feature_set)
                matches = []
                for settlement, candidate_days, candidate_seq in candidate_payloads:
                    scaled_candidate = _scaled(candidate_seq, stats, feature_set)
                    distance, path = dtw_distance(scaled_query, scaled_candidate, DTW_RADIUS)
                    if not math.isfinite(distance):
                        raise ReplayQCError(f"non-finite DTW distance for {_candidate_id(settlement)}")
                    matches.append({"candidate_id": _candidate_id(settlement), "station": settlement.station, "target_date": settlement.target_date, "bucket": settlement.bucket, "distance": distance, "path_length": len(path), "path": path})
                matches.sort(key=lambda m: (m["distance"], m["candidate_id"]))
                selected = matches[:MAX_ANALOGS]
                counts = Counter(int(m["bucket"]) for m in selected)
                probabilities = {str(bucket): (counts.get(bucket, 0) / len(selected)) for bucket in BUCKETS}
                ordered_probabilities = dict(sorted(probabilities.items(), key=lambda pair: int(pair[0])))
                top_prob = max(ordered_probabilities.values())
                top_buckets = [int(bucket) for bucket, probability in ordered_probabilities.items() if probability == top_prob]
                analog_buckets = sorted(int(m["bucket"]) for m in selected)
                row = {
                    "event_id": key[0], "station": FIXTURE_STATION, "market_type": FIXTURE_MARKET,
                    "target_date": target_date, "as_of_epoch": cutoff.isoformat().replace("+00:00", "Z"), "cutoff": clock,
                    "feature_set": feature_set, "sequence_dates": query_days,
                    "bucket_probabilities": ordered_probabilities,
                    "top_buckets": top_buckets,
                    "expected_range": [analog_buckets[max(0, math.floor(0.10 * (len(analog_buckets) - 1)))], analog_buckets[min(len(analog_buckets) - 1, math.ceil(0.90 * (len(analog_buckets) - 1)))]],
                    "confidence": top_prob,
                    "abstained": False, "abstention_reason": None,
                    "analog_ids": [m["candidate_id"] for m in selected],
                    "analog_distances": [m["distance"] for m in selected],
                    "analog_count": len(selected), "eligible_candidate_count": len(candidate_payloads),
                    "actual_target_bucket": _target_bucket(settlements, target_date),
                    "updated_at": cutoff.isoformat().replace("+00:00", "Z"),
                    "algorithm": {"name": "exact_multivariate_dtw", "sequence_length": SEQUENCE_LENGTH, "radius": DTW_RADIUS, "path_normalization": "selected_path_length", "feature_weights": list(FEATURE_WEIGHTS[feature_set]), "tie_break": ["distance", "candidate_id"]},
                }
                rows.append(row)
    rows.sort(key=lambda r: (r["target_date"], r["cutoff"], FEATURE_SETS.index(r["feature_set"])))
    payload = {
        "schema_version": "trajectory-dtw-replay-v1", "spec": "TRAJECTORY-DTW-REPLAY-BUILD-SPEC-2026-08-15",
        "fixture": {"station": FIXTURE_STATION, "market_type": FIXTURE_MARKET, "target_dates": list(FIXTURE_TARGET_DATES), "cutoffs": list(FIXTURE_CUTOFFS), "database": str(db_path.relative_to(REPO_ROOT) if db_path.is_relative_to(REPO_ROOT) else db_path.name)},
        "configuration": {"feature_sets": list(FEATURE_SETS), "buckets": list(BUCKETS), "min_analogs": MIN_ANALOGS, "max_analogs": MAX_ANALOGS, "candidate_embargo_days": 5, "source_tables": ["metar_observations", "settlement_epochs"], "network_calls": False, "fallbacks": []},
        "counts": {"observation_rows": len(observations), "settlement_rows": len(settlements), "prediction_rows": len(rows), "abstained_rows": sum(1 for r in rows if r["abstained"]), "rejections": dict(sorted(rejection_counts.items()))},
        "rows": rows,
    }
    payload_bytes = canonical_bytes(payload)
    artifact = dict(payload)
    artifact["payload_sha256"] = sha256_bytes(payload_bytes)
    return _canonicalize(artifact)


def _abstained_row(key: tuple[str, str, str], target_date: str, cutoff: datetime, feature_set: str, reason: str, sequence_days: list[str] | None = None, target_bucket: int | None = None) -> dict[str, Any]:
    return {"event_id": key[0], "station": FIXTURE_STATION, "market_type": FIXTURE_MARKET, "target_date": target_date, "as_of_epoch": cutoff.isoformat().replace("+00:00", "Z"), "cutoff": key[1], "feature_set": feature_set, "sequence_dates": sequence_days or [], "bucket_probabilities": None, "top_buckets": [], "expected_range": None, "confidence": None, "abstained": True, "abstention_reason": reason, "analog_ids": [], "analog_distances": [], "analog_count": 0, "eligible_candidate_count": 0, "actual_target_bucket": target_bucket, "updated_at": cutoff.isoformat().replace("+00:00", "Z"), "algorithm": {"name": "exact_multivariate_dtw", "sequence_length": SEQUENCE_LENGTH, "radius": DTW_RADIUS, "feature_weights": list(FEATURE_WEIGHTS[feature_set])}}


class SequentialReplayState:
    """Append-only identity store proving replay update idempotence."""
    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, str], bytes] = {}

    def apply(self, row: Mapping[str, Any]) -> str:
        key = (str(row["event_id"]), str(row["cutoff"]), str(row["feature_set"]))
        data = canonical_bytes(row)
        old = self._rows.get(key)
        if old is not None and old != data:
            raise ReplayQCError(f"REVISION_ERROR: {key}")
        self._rows[key] = data
        return sha256_bytes(data)

    @property
    def count(self) -> int:
        return len(self._rows)


def write_artifact(artifact: Mapping[str, Any], output: Path) -> str:
    data = canonical_bytes(artifact) + b"\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(data)
    return sha256_bytes(data)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    artifact = replay_fixture(args.db)
    digest = write_artifact(artifact, args.output)
    state = SequentialReplayState()
    for row in artifact["rows"]:
        state.apply(row)
    print(json.dumps({"output": str(args.output), "sha256": digest, "rows": len(artifact["rows"]), "state_rows": state.count}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
