#!/usr/bin/env python3
"""Frozen, read-only GEFS-only direct temperature evaluator.

This evaluator is deliberately fail-closed.  The current archive does not
carry enough lineage to turn a cycle label into a dated as-of forecast, so the
2026-08-15 run emits an eligibility ledger and a BLOCKED score artifact rather
than guessing issue/valid semantics or scoring a proxy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Sequence

VERSION = "gefs-direct-temperature-v1.0-2026-08-15"
RUN_DATE = "2026-08-15"
FIT_START, FIT_END = "2021-08-19", "2025-12-31"
CAL_START, CAL_END = "2026-01-01", "2026-02-28"
SHADOW_START, SHADOW_END = "2026-03-01", "2026-08-13"
CUTOFF_HOUR_UTC = 12
STEP_HOURS = 24
DIRECTION_EPSILON_F = 0.5
STATIONS = (
    "KATL", "KAUS", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU", "KLAS", "KLAX", "KMDW",
    "KMIA", "KMSP", "KMSY", "KNYC", "KOKC", "KPHL", "KPHX", "KSAT", "KSEA", "KSFO",
)
EVENT_TYPES = ("HIGH", "LOW")
SEASONS = ("DJF", "MAM", "JJA", "SON")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_json_bytes(obj: Any) -> bytes:
    return (json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode()


def parse_iso_date(value: str) -> date:
    return date.fromisoformat(value)


def partition_for(target_date: str) -> str | None:
    if FIT_START <= target_date <= FIT_END:
        return "FIT_HISTORY"
    if CAL_START <= target_date <= CAL_END:
        return "CALIBRATION"
    if SHADOW_START <= target_date <= SHADOW_END:
        return "LOCKED_SHADOW"
    return None


def cutoff_for(target_date: str) -> str:
    """UTC cutoff for local target-date D, represented without timezone ambiguity."""
    d = parse_iso_date(target_date) - timedelta(days=1)
    return f"{d.isoformat()}T12:00:00Z"


def season_for(target_date: str) -> str:
    month = parse_iso_date(target_date).month
    return {12: "DJF", 1: "DJF", 2: "DJF", 3: "MAM", 4: "MAM", 5: "MAM",
            6: "JJA", 7: "JJA", 8: "JJA", 9: "SON", 10: "SON", 11: "SON"}[month]


def panel_gate(n_expected: int | None, n_present: int | None) -> tuple[bool, str]:
    if n_expected is None or n_expected <= 0:
        return False, "missing_expected_member_count"
    if n_present is None or n_present < 0:
        return False, "missing_present_member_count"
    required = math.ceil(0.80 * n_expected)
    if n_present < required:
        return False, "insufficient_members"
    return True, "eligible"


def empirical_crps(members: Sequence[float], actual: float) -> float:
    """CRPS for an equally weighted empirical CDF."""
    xs = sorted(float(x) for x in members)
    if not xs:
        raise ValueError("members must be non-empty")
    n = len(xs)
    first = sum(abs(x - actual) for x in xs) / n
    pair = sum(abs(x - y) for x in xs for y in xs) / (2.0 * n * n)
    return first - pair


def point_metrics(predictions: Sequence[float], actuals: Sequence[float]) -> dict[str, float | int | None]:
    if len(predictions) != len(actuals):
        raise ValueError("prediction/actual length mismatch")
    if not predictions:
        return {"n": 0, "mae_f": None, "rmse_f": None, "bias_f": None}
    errors = [float(p) - float(a) for p, a in zip(predictions, actuals)]
    return {
        "n": len(errors),
        "mae_f": sum(abs(e) for e in errors) / len(errors),
        "rmse_f": math.sqrt(sum(e * e for e in errors) / len(errors)),
        "bias_f": sum(errors) / len(errors),
    }


def brier_ece(probabilities: Sequence[float], outcomes: Sequence[int], bins: int = 10) -> dict[str, Any]:
    if len(probabilities) != len(outcomes):
        raise ValueError("probability/outcome length mismatch")
    if not probabilities:
        return {"n": 0, "brier": None, "ece": None, "bins": []}
    rows = []
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        idx = [j for j, p in enumerate(probabilities) if (lo <= p < hi) or (i == bins - 1 and p == hi)]
        if idx:
            phat = sum(probabilities[j] for j in idx) / len(idx)
            ohat = sum(outcomes[j] for j in idx) / len(idx)
            rows.append({"bin": i, "n": len(idx), "mean_probability": phat, "event_rate": ohat,
                         "abs_calibration_error": abs(phat - ohat)})
    ece = sum(b["n"] * b["abs_calibration_error"] for b in rows) / len(probabilities)
    return {"n": len(probabilities), "brier": sum((p - y) ** 2 for p, y in zip(probabilities, outcomes)) / len(probabilities),
            "ece": ece, "bins": rows}


def connect_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)


def schema_snapshot(conn: sqlite3.Connection) -> list[dict[str, str]]:
    return [{"name": name, "sql": sql or ""} for name, sql in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name")]


def inventory(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    data = root / "data"
    gefs_path = data / "gefs_archive.db"
    settle_path = data / "kalshi_settlements.db"
    mapping_path = data / "station_mapping.json"
    spec_path = root / "docs" / "plans" / "GEFS-DIRECT-TEMPERATURE-EVALUATION-SPEC-2026-08-15.md"
    script_path = Path(__file__).resolve()

    mapping = json.loads(mapping_path.read_text())
    mapped_stations = sorted(mapping.get("stations", {}).keys())
    mapping_gate = mapped_stations == sorted(STATIONS) and mapping.get("station_count") == 20

    gc = connect_ro(gefs_path)
    sc = connect_ro(settle_path)
    try:
        gefs_schema = schema_snapshot(gc)
        settle_schema = schema_snapshot(sc)
        q = ("SELECT target_date, station, step, ensemble_mean, ensemble_min, ensemble_max, "
             "member_values, n_members, fetch_timestamp, init_cycle "
             "FROM gefs_archive WHERE step=? AND station IN (%s) AND target_date BETWEEN ? AND ? "
             "ORDER BY target_date, station, step" % ",".join("?" for _ in STATIONS))
        params: list[Any] = [STEP_HOURS, *STATIONS, FIT_START, SHADOW_END]
        forecast_rows = [dict(zip(("target_date", "station", "step", "ensemble_mean", "ensemble_min", "ensemble_max",
                                   "member_values", "n_members", "fetch_timestamp", "init_cycle"), row))
                         for row in gc.execute(q, params)]

        outcome_rows = [dict(zip(("station", "series", "event_ticker", "target_date", "kalshi_temp", "source", "source_type"), row))
                        for row in sc.execute(
                            "SELECT station, series, event_ticker, target_date, kalshi_temp, source, source_type "
                            "FROM kalshi_settlements WHERE station IN (%s) AND target_date BETWEEN ? AND ? "
                            "AND (series LIKE 'KXHIGH%%' OR series LIKE 'KXLOW%%') "
                            "ORDER BY station, target_date, series, event_ticker" % ",".join("?" for _ in STATIONS),
                            [*STATIONS, FIT_START, SHADOW_END])]
    finally:
        gc.close(); sc.close()

    member_strata = Counter(str(r["n_members"]) for r in forecast_rows)
    date_strata = Counter(partition_for(r["target_date"]) or "OUTSIDE" for r in forecast_rows)
    station_counts = Counter(r["station"] for r in forecast_rows)
    event_counts = Counter("HIGH" if str(r["series"]).startswith("KXHIGH") else "LOW" for r in outcome_rows)
    outcome_station_counts = Counter(r["station"] for r in outcome_rows)
    duplicate_outcomes = Counter((r["station"], r["target_date"], "HIGH" if str(r["series"]).startswith("KXHIGH") else "LOW") for r in outcome_rows)
    duplicate_outcomes = {"|".join(k): v for k, v in sorted(duplicate_outcomes.items()) if v > 1}

    # A source row is not event-specific and cannot prove a local-day extreme.  Keep one
    # deterministic eligibility row per archived step-24 source row.
    ledgers: list[dict[str, Any]] = []
    for r in forecast_rows:
        n = int(r["n_members"]) if r["n_members"] is not None else None
        blob_len = len(r["member_values"]) if r["member_values"] is not None else None
        reasons = [
            "missing_issue_timestamp",
            "missing_availability_provenance",
            "unverified_valid_time_semantics",
            "unverified_target_kind",
            "missing_member_identity",
            "missing_nws_cli_provenance",
        ]
        ledgers.append({
            "source": "GEFS",
            "station": r["station"],
            "target_date_local": r["target_date"],
            "event_type": None,
            "step_hours": r["step"],
            "init_cycle": r["init_cycle"],
            "issue_time_utc": None,
            "available_time_utc": None,
            "asof_cutoff_utc": cutoff_for(r["target_date"]),
            "partition": partition_for(r["target_date"]),
            "n_expected": None,
            "n_present": n,
            "n_missing": None,
            "blob_length_bytes": blob_len,
            "effective_n": None,
            "eligible": False,
            "exclusion_reasons": reasons,
        })

    source_hashes = {
        "data/gefs_archive.db": sha256_file(gefs_path),
        "data/kalshi_settlements.db": sha256_file(settle_path),
        "data/station_mapping.json": sha256_file(mapping_path),
        "docs/plans/GEFS-DIRECT-TEMPERATURE-EVALUATION-SPEC-2026-08-15.md": sha256_file(spec_path),
        "scripts/gefs_direct_temperature_evaluation.py": sha256_file(script_path),
    }
    inventory_summary = {
        "gefs_archive": {
            "path": "data/gefs_archive.db", "table": "gefs_archive", "schema": gefs_schema,
            "candidate_step24_rows": len(forecast_rows), "candidate_dates": len({r["target_date"] for r in forecast_rows}),
            "candidate_stations": len({r["station"] for r in forecast_rows}),
            "min_target_date": min((r["target_date"] for r in forecast_rows), default=None),
            "max_target_date": max((r["target_date"] for r in forecast_rows), default=None),
            "member_count_strata": dict(sorted(member_strata.items())),
            "partition_counts": dict(sorted(date_strata.items())),
            "station_counts": dict(sorted(station_counts.items())),
            "all_init_cycles": sorted({r["init_cycle"] for r in forecast_rows}),
            "issue_timestamp_column_present": False,
            "availability_timestamp_column_present": False,
            "member_identity_column_present": False,
            "event_type_column_present": False,
            "valid_time_column_present": False,
        },
        "settlement_outcomes": {
            "path": "data/kalshi_settlements.db", "table": "kalshi_settlements", "schema": settle_schema,
            "candidate_rows": len(outcome_rows), "candidate_stations": len({r["station"] for r in outcome_rows}),
            "event_type_counts": dict(sorted(event_counts.items())),
            "station_counts": dict(sorted(outcome_station_counts.items())),
            "duplicate_station_date_event_keys": duplicate_outcomes,
            "nws_cli_raw_payload_present": False, "nws_cli_report_id_present": False,
            "immutable_revision_present": False,
        },
        "station_mapping": {"path": "data/station_mapping.json", "mapped_station_count": len(mapped_stations),
                            "exact_20_station_gate": mapping_gate},
    }
    return source_hashes, ledgers, inventory_summary


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(obj))


def display_path(path: Path, root: Path) -> str:
    """Prefer workspace-relative paths, but support an external test output path."""
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    output = (args.output or root / "data" / "gefs_direct_temperature_evaluation_20260815.json").resolve()
    rows_path = output.parent / "gefs_direct_temperature_evaluation_rows_20260815.jsonl"
    manifest_path = output.parent / "gefs_direct_temperature_eligibility_manifest_20260815.json"

    source_hashes, ledger, inv = inventory(root)
    with rows_path.open("wb") as f:
        for row in ledger:
            f.write(canonical_json_bytes(row))
    rows_hash = sha256_file(rows_path)

    gate_status = {
        "source_scope_gefs_only": "PASS",
        "exact_20_station_gate": "PASS" if inv["station_mapping"]["exact_20_station_gate"] else "FAIL",
        "issue_timestamp_gate": "FAIL",
        "availability_gate": "FAIL",
        "12z_d_minus_1_cutoff_gate": "FAIL",
        "step24_valid_window_gate": "FAIL",
        "member_identity_gate": "FAIL",
        "member_missingness_no_imputation_gate": "PASS_FAIL_CLOSED",
        "nws_settlement_provenance_gate": "FAIL",
        "target_day_leakage_gate": "PASS_NO_FORECAST_READ",
        "determinism_gate": "PENDING_TWO_RUN_RECEIPT",
        "scoring_gate": "BLOCKED",
    }
    manifest = {
        "artifact_version": VERSION, "run_date_utc": RUN_DATE, "status": "BLOCKED",
        "manifest_kind": "read_only_gefs_eligibility", "scope": "GEFS-only; no ECMWF/TIGGE, HMM, trading, production, SignalRegistry, or sweep",
        "frozen_windows": {"FIT_HISTORY": [FIT_START, FIT_END], "CALIBRATION": [CAL_START, CAL_END], "LOCKED_SHADOW": [SHADOW_START, SHADOW_END]},
        "cutoff": "12:00:00 UTC on D-1", "lead_step_hours": STEP_HOURS, "event_types": list(EVENT_TYPES),
        "stations": list(STATIONS), "source_hashes": source_hashes,
        "inventory": inv, "row_artifact": {"path": display_path(rows_path, root), "sha256": rows_hash, "row_count": len(ledger)},
        "candidate_rows": len(ledger), "eligible_rows": 0, "rejected_rows": len(ledger),
        "rejection_reason_counts": dict(sorted(Counter(reason for row in ledger for reason in row["exclusion_reasons"]).items())),
        "gate_status": gate_status,
        "notes": [
            "The archive init_cycle='12Z' is a cycle label only; no dated run/issue timestamp exists.",
            "fetch_timestamp is ingestion metadata and is not used as issue or availability time.",
            "member_values have no member IDs; observed n_members 6/30/31 are reported but not normalized, padded, or imputed.",
            "step=24 does not prove a local-day HIGH or LOW valid window.",
            "Settlement rows identify series/value but retain no immutable NWS CLI report payload or revision provenance.",
        ],
    }
    write_json(manifest_path, manifest)
    manifest_hash = sha256_file(manifest_path)

    result = {
        "artifact_version": VERSION, "run_date_utc": RUN_DATE, "status": "BLOCKED",
        "disposition": "PARK/BLOCKED_PENDING_PROVENANCE_REPAIR", "accuracy_claim": False,
        "scope": "GEFS-only direct temperature; read-only QC artifact",
        "exclusions": ["ECMWF/TIGGE", "HMM", "trading", "production wiring", "SignalRegistry", "parameter sweep"],
        "contract": {"target": "NWS settlement station daily HIGH/LOW outcome", "units": "degF", "directional_epsilon_f": DIRECTION_EPSILON_F,
                     "cutoff": "12:00:00 UTC on D-1", "step_hours": STEP_HOURS,
                     "windows": {"FIT_HISTORY": [FIT_START, FIT_END], "CALIBRATION": [CAL_START, CAL_END], "LOCKED_SHADOW": [SHADOW_START, SHADOW_END]},
                     "stations": list(STATIONS), "member_policy": "fail closed; no pad/impute; n_expected and IDs required"},
        "input_hashes": source_hashes, "inventory": inv,
        "eligibility": {"manifest_path": display_path(manifest_path, root), "manifest_sha256": manifest_hash,
                        "row_artifact_path": display_path(rows_path, root), "row_artifact_sha256": rows_hash,
                        "candidate_rows": len(ledger), "eligible_rows": 0, "rejected_rows": len(ledger),
                        "observed_member_count_strata": inv["gefs_archive"]["member_count_strata"]},
        "metrics": {"status": "BLOCKED", "n_scored": 0, "MAE_f": None, "RMSE_f": None, "bias_f": None,
                    "directional_accuracy": None, "CRPS": None, "Brier": "NOT_VALID_WITHOUT_VALID_PROBABILITY_CONSTRUCTION",
                    "ECE": "NOT_VALID_WITHOUT_VALID_PROBABILITY_CONSTRUCTION", "interval_coverage": None,
                    "strata": {"station": {}, "event_type": {}, "season": {}, "lead_hours": {}, "member_count": {}, "effective_n": {}}},
        "gates": gate_status,
        "determinism": {"two_run_byte_identity_required": True, "checked_by_receipt": False},
        "limitations": [
            "No dated GEFS initialization/issue timestamp; init_cycle='12Z' cannot be dated by inference.",
            "No auditable forecast availability timestamp at or before cutoff.",
            "No member IDs in current blobs; 6/30/31 counts cannot be silently normalized.",
            "Current step-24 scalar has no proven local-day HIGH/LOW valid-window semantics.",
            "Settlement table lacks immutable NWS CLI report text, report ID, and revision provenance.",
            "No score, accuracy comparison, calibration, or production recommendation is valid from this run.",
        ],
        "accuracy_focused_suggestion": "After dated GEFS lineage and exact CLI snapshots are frozen, fit station x event-type x lead rolling bias correction with spread-preserving empirical calibration using only prior labels, then test once on LOCKED_SHADOW.",
    }
    write_json(output, result)
    print(json.dumps({"status": result["status"], "output": str(output), "sha256": sha256_file(output),
                       "manifest": str(manifest_path), "manifest_sha256": manifest_hash,
                       "rows": str(rows_path), "rows_sha256": rows_hash, "candidate_rows": len(ledger)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
