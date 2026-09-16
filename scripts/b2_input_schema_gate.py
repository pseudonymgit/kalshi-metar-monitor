#!/usr/bin/env python3
"""Read-only B2 input completion and schema-contract gate.

No inference, model execution, price/probability synthesis, or DB writes.  Raw
z8 prices are converted only under an explicit declared unit contract:
``dollars`` is identity; ``cents`` divides by 100.  ``auto`` is forbidden for
eligibility because the persisted table has no unit provenance column.
"""
import argparse, csv, datetime as dt, json, math, sqlite3
from pathlib import Path

REQUIRED_CAL = ("snapshot_hour", "station", "series_ticker", "market_ticker",
                "market_type", "strike_temp", "strike_source_field",
                "strike_provenance_status", "calibrated_probability",
                "calibration_source", "calibration_version")
SNAPSHOT_COLS = ("snapshot_time", "station", "series_ticker", "market_ticker",
                 "strike_temp", "strike_source_field", "strike_provenance_status",
                 "market_type", "yes_bid", "yes_ask", "mid_price",
                 "price_unit", "bid_source_field", "ask_source_field",
                 "api_contract_version", "provenance_status")


def convert_price(value, unit):
    if value is None or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return None, "missing_or_nonfinite"
    if unit == "dollars": converted = float(value)
    elif unit == "cents": converted = float(value) / 100.0
    else: return None, "unit_not_declared"
    if not (0.0 < converted < 1.0): return None, "outside_open_probability_range"
    return converted, None


def _strike_key(value):
    """Canonicalize numeric/string strike spellings without changing identity."""
    try:
        return format(float(value), ".15g")
    except (TypeError, ValueError):
        return str(value)


def _key(row):
    """Exact join identity; strike is part of the contract, never inferred."""
    return tuple(row.get(k) for k in ("snapshot_hour", "station", "series_ticker", "market_ticker", "market_type")) + (_strike_key(row.get("strike_temp")),)


def inspect(snapshot_db, calibrated_csv, price_unit="dollars"):
    report = {"price_contract": {"declared_unit": price_unit, "conversion": "identity" if price_unit == "dollars" else "raw/100" if price_unit == "cents" else None}, "snapshot": {}, "calibration": {}, "join": {}, "missing_inputs": []}
    with sqlite3.connect(f"file:{snapshot_db}?mode=ro", uri=True) as conn:
        cols = {r[1] for r in conn.execute("pragma table_info(market_snapshots)")}
        report["snapshot"]["schema_missing_columns"] = sorted(set(SNAPSHOT_COLS) - cols)
        if report["snapshot"]["schema_missing_columns"]:
            report["missing_inputs"].append("snapshot_schema")
            report["eligible"] = False
            return report
        rows = [dict(zip(SNAPSHOT_COLS, r)) for r in conn.execute("select " + ",".join(SNAPSHOT_COLS) + " from market_snapshots")]
    hours = [str(r["snapshot_time"])[:13] for r in rows if r["snapshot_time"]]
    report["snapshot"].update({"rows": len(rows), "distinct_hours": len(set(hours)), "hours": sorted(set(hours)), "hour_coverage_ok": len(set(hours)) >= 72})
    if len(set(hours)) < 72: report["missing_inputs"].append("snapshot_hour_coverage_lt_72")
    invalid, ambiguous, ordering = 0, 0, 0
    for r in rows:
        if (r["strike_provenance_status"] != "verified" or
                not r["strike_source_field"] or
                r["price_unit"] != "cents" or
                r["bid_source_field"] != "yes_bid" or
                r["ask_source_field"] != "yes_ask" or
                not r["api_contract_version"] or
                r["provenance_status"] != "verified"):
            invalid += 1
            continue
        converted = []
        for field in ("yes_bid", "yes_ask", "mid_price"):
            val, reason = convert_price(r[field], "cents")
            if reason: invalid += 1
            converted.append(val)
        if any(v is None for v in converted): continue
        bid, ask, mid = converted
        if not (bid <= mid <= ask): ordering += 1
    report["snapshot"].update({"price_invalid_values": invalid, "price_ordering_failures": ordering, "prices_valid": invalid == 0 and ordering == 0})
    if invalid or ordering: report["missing_inputs"].append("validated_price_rows")
    path = Path(calibrated_csv)
    cal_rows = list(csv.DictReader(path.open(newline=""))) if path.exists() else []
    fields = set(cal_rows[0]) if cal_rows else set()
    report["calibration"] = {"file": str(path), "exists": path.exists(), "rows": len(cal_rows), "missing_columns": sorted(set(REQUIRED_CAL) - fields), "row_schema_valid": True, "invalid_probability_rows": 0, "invalid_strike_provenance_rows": 0}
    seen_cal = {}
    for row in cal_rows:
        if set(REQUIRED_CAL) - set(row): report["calibration"]["row_schema_valid"] = False
        try: p = float(row["calibrated_probability"])
        except (KeyError, TypeError, ValueError): p = math.nan
        if not math.isfinite(p) or not 0.0 <= p <= 1.0: report["calibration"]["invalid_probability_rows"] += 1
        if row.get("strike_provenance_status") != "verified" or not row.get("strike_source_field"):
            report["calibration"]["invalid_strike_provenance_rows"] += 1
        seen_cal[_key(row)] = seen_cal.get(_key(row), 0) + 1
    if not path.exists() or not cal_rows: report["missing_inputs"].append("row_level_calibrated_probability_artifact")
    if (report["calibration"]["missing_columns"] or not report["calibration"]["row_schema_valid"] or
            report["calibration"]["invalid_probability_rows"] or report["calibration"]["invalid_strike_provenance_rows"]):
        report["missing_inputs"].append("calibration_schema_or_probability_values")
    snap_keys = {}
    for r in rows:
        k = (str(r["snapshot_time"])[:13], r["station"], r["series_ticker"], r["market_ticker"], r["market_type"], _strike_key(r["strike_temp"]))
        snap_keys[k] = snap_keys.get(k, 0) + 1
    matched = sum(1 for k in snap_keys if k in seen_cal)
    report["join"] = {"snapshot_keys": len(snap_keys), "calibration_keys": len(seen_cal), "matched_keys": matched, "unmatched_snapshot_keys": len(set(snap_keys)-set(seen_cal)), "unmatched_calibration_keys": len(set(seen_cal)-set(snap_keys)), "duplicate_snapshot_keys": sum(v > 1 for v in snap_keys.values()), "duplicate_calibration_keys": sum(v > 1 for v in seen_cal.values()), "exact_key_complete": bool(snap_keys) and matched == len(snap_keys) == len(seen_cal) and all(v == 1 for v in snap_keys.values()) and all(v == 1 for v in seen_cal.values())}
    if not report["join"]["exact_key_complete"]: report["missing_inputs"].append("exact_key_join_completeness")
    report["eligible"] = not report["missing_inputs"]
    return report


def run(db, calibration, output, price_unit):
    result = inspect(db, calibration, price_unit)
    result.update({"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(), "status": "eligible" if result["eligible"] else "gated", "mode": "read_only_no_ai_no_synthetic_data"})
    Path(output).write_text(json.dumps(result, indent=2) + "\n")
    if not result["eligible"]: raise SystemExit(2)
    return result

if __name__ == "__main__":
    p = argparse.ArgumentParser(); p.add_argument("--snapshot-db", required=True); p.add_argument("--calibrated-csv", required=True); p.add_argument("--output", required=True); p.add_argument("--price-unit", choices=("dollars", "cents"), required=True)
    a = p.parse_args(); print(json.dumps(run(a.snapshot_db, a.calibrated_csv, a.output, a.price_unit), indent=2))
