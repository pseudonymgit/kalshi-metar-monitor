#!/usr/bin/env python3
"""Read-only, fail-closed GEFS station-local temperature geometry gate.

This validator consumes only the immutable pilot bundle's official GRIB2 and
.idx objects. It does not read labels, scoring/evaluation data, trading, or
production state. It validates 3-hour TMP trajectory geometry and independent
native TMAX/TMIN interval coverage, then emits canonical deterministic QC
artifacts.
"""
from __future__ import annotations

import argparse
import email.utils
import hashlib
import json
import os
import re
import tempfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from eccodes import codes_get, codes_get_array, codes_grib_new_from_file, codes_release

MEMBERS = ("gec00", "gep01")
# The recovery bundle adds the exact official f057 boundary object. Keep it
# in the validator's source-lead contract so the native TMAX/TMIN interval
# closure is actually consumed rather than silently ignored.
POINT_LEADS = (0,) + tuple(range(6, 55, 3)) + (57,)
FORECAST_LEADS = tuple(range(6, 55, 3)) + (57,)
STATIONS = {
    "KATL": (33.6407, -84.4277, "America/New_York"),
    "KDEN": (39.8617, -104.6731, "America/Denver"),
    "KNYC": (40.7772, -73.8726, "America/New_York"),
    "KPHX": (33.4342, -112.0116, "America/Phoenix"),
    "KSEA": (47.4499, -122.3118, "America/Los_Angeles"),
}
FIXTURES = {
    "ordinary_2024_01_15": ("2024-01-15", "2024-01-14T00:00:00Z", "ordinary"),
    "dst_spring_2024_03_10": ("2024-03-10", "2024-03-09T00:00:00Z", "dst_spring"),
    "dst_fall_2024_11_03": ("2024-11-03", "2024-11-02T00:00:00Z", "dst_fall"),
    "historical_2025_11_15": ("2025-11-15", "2025-11-14T00:00:00Z", "historical"),
    "missing_expected_2020_01_15": ("2020-01-15", "2020-01-14T00:00:00Z", "expected_missing"),
}
INCLUDED = tuple(k for k, v in FIXTURES.items() if v[2] != "expected_missing")
VERSION = "gefs-temperature-geometry-fix-v1-2026-08-15"


def canon(obj) -> bytes:
    return (json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_http(value: str) -> datetime:
    return email.utils.parsedate_to_datetime(value).astimezone(timezone.utc)


def grid_index(lat: float, lon: float, meta: dict) -> dict:
    ni, nj = int(meta["Ni"]), int(meta["Nj"])
    la1, la2 = float(meta["latitudeOfFirstGridPointInDegrees"]), float(meta["latitudeOfLastGridPointInDegrees"])
    lo1, lo2 = float(meta["longitudeOfFirstGridPointInDegrees"]), float(meta["longitudeOfLastGridPointInDegrees"])
    best = None
    for j in range(nj):
        glat = la1 + j * (la2 - la1) / (nj - 1)
        for i in range(ni):
            glon = (lo1 + i * (lo2 - lo1) / (ni - 1)) % 360.0
            dlon = min(abs((lon % 360.0) - glon), abs((lon % 360.0) - glon + 360.0), abs((lon % 360.0) - glon - 360.0))
            key = ((glat - lat) ** 2 + dlon ** 2, j * ni + i, j, i)
            if best is None or key < best[0]:
                best = (key, j * ni + i, j, i, glat, glon)
    _, index, j, i, glat, glon = best
    return {"grid_index": index, "grid_i": i, "grid_j": j, "grid_latitude": round(glat, 8), "grid_longitude_0_360": round(glon, 8)}


def idx_fields(idx_path: Path) -> list[dict]:
    out = []
    for line_no, line in enumerate(idx_path.read_text(errors="replace").splitlines(), 1):
        parts = line.split(":", 4)
        if len(parts) != 5:
            continue
        if parts[3] not in ("TMP", "TMAX", "TMIN"):
            continue
        if parts[3] == "TMP" and "2 m above ground" not in parts[4]:
            continue
        if parts[3] in ("TMAX", "TMIN") and "2 m above ground" not in parts[4]:
            continue
        out.append({"line_no": line_no, "offset": int(parts[1]), "field": parts[3], "descriptor": parts[4], "line": line})
    return out


def decode_field(raw: bytes, start: int, end: int) -> tuple[dict, list[float]]:
    tf = tempfile.NamedTemporaryFile(prefix="gefs_geometry_", delete=False)
    try:
        tf.write(raw[start:end + 1]); tf.close()
        with open(tf.name, "rb") as fh:
            gid = codes_grib_new_from_file(fh)
            if gid is None:
                raise ValueError("GRIB_MESSAGE_PARSE_FAILED")
            names = ("shortName", "units", "step", "stepType", "startStep", "endStep", "validityDate", "validityTime", "dataDate", "dataTime", "typeOfLevel", "level", "Ni", "Nj", "latitudeOfFirstGridPointInDegrees", "longitudeOfFirstGridPointInDegrees", "latitudeOfLastGridPointInDegrees", "longitudeOfLastGridPointInDegrees", "gridType", "number", "perturbationNumber")
            meta = {}
            for name in names:
                try:
                    meta[name] = codes_get(gid, name)
                except Exception:
                    meta[name] = None
            values = list(codes_get_array(gid, "values"))
            codes_release(gid)
            return meta, values
    finally:
        try:
            os.unlink(tf.name)
        except FileNotFoundError:
            pass


def native_time(meta: dict, prefix: str) -> datetime:
    n, t = int(meta[prefix + "Date"]), int(meta[prefix + "Time"])
    return datetime(n // 10000, (n // 100) % 100, n % 100, t // 100, t % 100, tzinfo=timezone.utc)


def parse_object(raw_path: Path, idx_path: Path, expected_lead: int) -> dict:
    raw = raw_path.read_bytes()
    idx_bytes = idx_path.read_bytes()
    fields = idx_fields(idx_path)
    if not fields:
        raise ValueError("REQUIRED_FIELDS_ABSENT")
    by_field = defaultdict(list)
    for field in fields:
        by_field[field["field"]].append(field)
    if len(by_field["TMP"]) != 1:
        raise ValueError(f"TMP_2M_INDEX_AMBIGUOUS:{len(by_field['TMP'])}")
    decoded = {}
    for field_name, entries in by_field.items():
        for pos, entry in enumerate(entries):
            end = (fields[fields.index(entry) + 1]["offset"] - 1) if fields.index(entry) + 1 < len(fields) else len(raw) - 1
            meta, values = decode_field(raw, entry["offset"], end)
            decoded[field_name] = {"meta": meta, "values": values, "idx_line": entry["line"], "start": entry["offset"], "end": end}
            break
    tmp = decoded["TMP"]
    issue, valid = native_time(tmp["meta"], "data"), native_time(tmp["meta"], "validity")
    if valid != issue + timedelta(hours=expected_lead):
        raise ValueError("NATIVE_VALID_TIME_MISMATCH")
    return {"raw_sha256": sha_bytes(raw), "idx_sha256": sha_bytes(idx_bytes), "raw_bytes": len(raw), "idx_bytes": len(idx_bytes), "message": decoded, "issue": issue, "valid": valid}


def interval_endpoints(objs: list[dict], field: str) -> list[dict]:
    rows = []
    for obj in objs:
        item = obj["message"].get(field)
        if not item:
            continue
        meta = item["meta"]
        if meta.get("shortName") != field.lower() or meta.get("units") != "K" or meta.get("stepType") != ("max" if field == "TMAX" else "min"):
            rows.append({"start": None, "end": None, "lead": None, "valid": iso(obj["valid"]), "native_semantics_ok": False, "reason": "NATIVE_INTERVAL_SEMANTICS"})
            continue
        rows.append({"start": int(meta.get("startStep")), "end": int(meta.get("endStep")), "lead": int(meta.get("step")), "valid": iso(obj["valid"]), "native_semantics_ok": True, "value_available": True, "obj": obj})
    return sorted(rows, key=lambda x: ((x["start"] is None), x["start"] if x["start"] is not None else 0, x["end"] if x["end"] is not None else 0, x["lead"] if x["lead"] is not None else 0))


def cover_window(intervals: list[dict], start_lead: float, end_lead: float) -> dict:
    usable = [x for x in intervals if x["native_semantics_ok"] and x["start"] < end_lead and x["end"] > start_lead]
    cursor = start_lead
    chosen = []
    gaps = []
    while cursor < end_lead:
        candidates = [x for x in usable if x["start"] <= cursor and x["end"] > cursor]
        if not candidates:
            next_start = min((x["start"] for x in usable if x["start"] > cursor), default=end_lead)
            gaps.append([round(cursor, 8), round(min(next_start, end_lead), 8)])
            cursor = min(next_start, end_lead)
            if cursor >= end_lead:
                break
            continue
        best = max(candidates, key=lambda x: (x["end"], -x["start"], -x["lead"]))
        chosen.append(best)
        cursor = min(float(best["end"]), end_lead)
    return {"covered": not gaps and cursor >= end_lead, "intervals": [{k: v for k, v in x.items() if k != "obj"} for x in chosen], "gaps": gaps}


def expected_leads(issue: datetime, target: date, tz_name: str) -> tuple[dict, list[int]]:
    tz = ZoneInfo(tz_name)
    local_start = datetime.combine(target, datetime.min.time(), tzinfo=tz)
    local_end = datetime.combine(target + timedelta(days=1), datetime.min.time(), tzinfo=tz)
    start, end = local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)
    lead_start = (start - issue).total_seconds() / 3600.0
    lead_end = (end - issue).total_seconds() / 3600.0
    leads = [lead for lead in FORECAST_LEADS if start <= issue + timedelta(hours=lead) < end]
    boundary = {
        "local_start": local_start.isoformat(), "local_end_exclusive": local_end.isoformat(),
        "window_start_utc": iso(start), "window_end_utc_exclusive": iso(end),
        "elapsed_utc_hours": (end - start).total_seconds() / 3600.0,
        "start_lead_hours": lead_start, "end_lead_hours_exclusive": lead_end,
        "expected_point_leads": leads,
        "unrepresented_point_boundary_leads": [x for x in (lead_start, lead_end) if x not in FORECAST_LEADS],
    }
    return boundary, leads


def native_crosscheck(objs: list[dict], station_index_value: int, window_start_lead: float, window_end_lead: float) -> dict:
    rows = []
    for field in ("TMAX", "TMIN"):
        for interval in interval_endpoints(objs, field):
            if not interval["native_semantics_ok"] or interval["start"] >= window_end_lead or interval["end"] <= window_start_lead:
                continue
            item = interval["obj"]["message"][field]
            native_value = float(item["values"][station_index_value])
            samples = []
            for obj in objs:
                lead = (obj["valid"] - obj["issue"]).total_seconds() / 3600.0
                if interval["start"] <= lead < interval["end"]:
                    samples.append(float(obj["message"]["TMP"]["values"][station_index_value]))
            relation_ok = None if not samples else (native_value >= max(samples) if field == "TMAX" else native_value <= min(samples))
            rows.append({"field": field, "interval_start_lead": interval["start"], "interval_end_lead": interval["end"], "native_value_k": round(native_value, 8), "sample_count": len(samples), "sample_max_k": round(max(samples), 8) if samples else None, "sample_min_k": round(min(samples), 8) if samples else None, "semantic_relation_ok": relation_ok})
    value_pairs = defaultdict(dict)
    for row in rows:
        value_pairs[(row["interval_start_lead"], row["interval_end_lead"])][row["field"]] = row["native_value_k"]
    for pair in value_pairs.values():
        if "TMAX" in pair and "TMIN" in pair:
            if pair["TMAX"] < pair["TMIN"]:
                rows.append({"field": "PAIR", "semantic_relation_ok": False, "reason": "TMAX_BELOW_TMIN"})
    # A native interval product is not required to equal the sparse 3-hour
    # instantaneous TMP samples: TMAX/TMIN are native interval semantics and
    # may use sub-step model values. Keep that comparison descriptive, while
    # hard-gating only metadata semantics and TMAX>=TMIN pair ordering.
    return {"rows": rows, "checked_rows": len(rows), "relation_failures": sum(1 for x in rows if x.get("reason") == "TMAX_BELOW_TMIN"), "sampled_rows": sum(1 for x in rows if x.get("sample_count", 0) > 0)}


def run(input_dir: Path, output_dir: Path) -> dict:
    manifest_path = input_dir / "manifests/request_manifest.json"
    fixtures_path = input_dir / "fixtures.json"
    manifest = json.loads(manifest_path.read_text())
    fixture_doc = json.loads(fixtures_path.read_text())
    requests = {(x["fixture_id"], x["member_id"], int(x["lead"]), x["kind"]): x for x in manifest.get("requests", [])}
    failures = []
    missingness = []
    lineage = []
    geometry = []
    native_rows = []
    object_rows = []
    row_summary = []
    by_group = defaultdict(list)
    source_root = input_dir
    for fixture_id in INCLUDED:
        target_s, run_s, case = FIXTURES[fixture_id]
        target, issue_expected = date.fromisoformat(target_s), parse_iso(run_s)
        cutoff = datetime.combine(target - timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=12)
        for member in MEMBERS:
            objects = {}
            for lead in POINT_LEADS:
                g = requests.get((fixture_id, member, lead, "grib2")); i = requests.get((fixture_id, member, lead, "idx"))
                if not g or not i or g.get("status") != 200 or i.get("status") != 200:
                    failures.append({"code": "MISSING_OBJECT_PAIR", "fixture_id": fixture_id, "member_id": member, "lead": lead})
                    continue
                gp, ip = source_root / g["path"], source_root / i["path"]
                if not gp.is_file() or not ip.is_file():
                    failures.append({"code": "OBJECT_PATH_MISSING", "fixture_id": fixture_id, "member_id": member, "lead": lead}); continue
                try:
                    parsed = parse_object(gp, ip, lead)
                except Exception as exc:
                    failures.append({"code": str(exc).split(":", 1)[0], "fixture_id": fixture_id, "member_id": member, "lead": lead, "detail": str(exc)}); continue
                if parsed["issue"] != issue_expected or parsed["issue"] > cutoff:
                    failures.append({"code": "ISSUE_CUTOFF_FAILURE", "fixture_id": fixture_id, "member_id": member, "lead": lead, "issue": iso(parsed["issue"]), "cutoff": iso(cutoff)})
                available_raw = g.get("response_headers", {}).get("last-modified")
                if not available_raw or parse_http(available_raw) > cutoff:
                    failures.append({"code": "AVAILABILITY_CUTOFF_FAILURE", "fixture_id": fixture_id, "member_id": member, "lead": lead, "available": available_raw, "cutoff": iso(cutoff)})
                tmp_meta = parsed["message"]["TMP"]["meta"]
                native_member = "gec00" if tmp_meta.get("number") == 0 else (f"gep{int(tmp_meta.get('number')):02d}" if tmp_meta.get("number") is not None else None)
                if native_member != member:
                    failures.append({"code": "NATIVE_MEMBER_MISMATCH", "fixture_id": fixture_id, "member_id": member, "lead": lead, "native_member": native_member})
                if tmp_meta.get("shortName") != "2t" or tmp_meta.get("units") != "K" or tmp_meta.get("typeOfLevel") != "heightAboveGround" or tmp_meta.get("level") != 2:
                    failures.append({"code": "TMP_FIELD_CONTRACT_FAILURE", "fixture_id": fixture_id, "member_id": member, "lead": lead})
                objects[lead] = parsed
                object_rows.append({"fixture_id": fixture_id, "member_id": member, "lead": lead, "raw_sha256": parsed["raw_sha256"], "idx_sha256": parsed["idx_sha256"], "raw_bytes": parsed["raw_bytes"], "idx_bytes": parsed["idx_bytes"], "message_sha256": {k: sha_bytes(gp.read_bytes()[v["start"]:v["end"] + 1]) for k, v in sorted(parsed["message"].items())}})
            if set(objects) != set(POINT_LEADS):
                missingness.append({"fixture_id": fixture_id, "member_id": member, "kind": "source_leads", "expected_leads": list(POINT_LEADS), "present_leads": sorted(objects), "missing_leads": sorted(set(POINT_LEADS) - set(objects))})
            if not objects:
                continue
            ordered = [objects[x] for x in sorted(objects)]
            for station, (lat, lon, tz_name) in STATIONS.items():
                mapping = grid_index(lat, lon, ordered[0]["message"]["TMP"]["meta"])
                boundary, expected = expected_leads(issue_expected, target, tz_name)
                start_lead, end_lead = boundary["start_lead_hours"], boundary["end_lead_hours_exclusive"]
                points = []
                for lead in expected:
                    if lead not in objects:
                        continue
                    obj = objects[lead]; local = obj["valid"].astimezone(ZoneInfo(tz_name)); value = float(obj["message"]["TMP"]["values"][mapping["grid_index"]])
                    points.append({"lead": lead, "valid_time_utc": iso(obj["valid"]), "valid_time_local": local.isoformat(), "fold": local.fold, "value_k": round(value, 8), "value_f": round((value - 273.15) * 9.0 / 5.0 + 32.0, 8), "raw_sha256": obj["raw_sha256"], "idx_sha256": obj["idx_sha256"]})
                tmp_ok = [x["lead"] for x in points] == expected and len(points) == len(set(x["lead"] for x in points))
                tmax_cov, tmin_cov = cover_window(interval_endpoints(ordered, "TMAX"), start_lead, end_lead), cover_window(interval_endpoints(ordered, "TMIN"), start_lead, end_lead)
                cross = native_crosscheck(ordered, mapping["grid_index"], start_lead, end_lead)
                native_ok = tmax_cov["covered"] and tmin_cov["covered"] and cross["relation_failures"] == 0
                row_ok = tmp_ok and native_ok and set(objects) == set(POINT_LEADS)
                reasons = []
                if not tmp_ok: reasons.append("TMP_POINT_CADENCE_OR_MISSINGNESS")
                if not tmax_cov["covered"]: reasons.append("TMAX_INTERVAL_GAP")
                if not tmin_cov["covered"]: reasons.append("TMIN_INTERVAL_GAP")
                if cross["relation_failures"]: reasons.append("NATIVE_CROSSCHECK_FAILURE")
                if set(objects) != set(POINT_LEADS): reasons.append("SOURCE_LEAD_SET_INCOMPLETE")
                interval_gaps = tmax_cov["gaps"] + tmin_cov["gaps"]
                exact_boundary_leads = sorted({int((gap[1] // 3) * 3) if gap[1] % 3 == 0 else int(((gap[1] // 3) + 1) * 3) for gap in interval_gaps if gap[1] > gap[0]})
                missingness.append({"fixture_id": fixture_id, "station_id": station, "member_id": member, "kind": "station_local_geometry", "expected_point_leads": expected, "present_point_leads": [x["lead"] for x in points], "missing_point_leads": sorted(set(expected) - set(x["lead"] for x in points)), "boundary_leads": boundary["unrepresented_point_boundary_leads"], "exact_missing_boundary_leads": exact_boundary_leads, "tmax_interval_gaps": tmax_cov["gaps"], "tmin_interval_gaps": tmin_cov["gaps"], "status": "PASS" if row_ok else "BLOCKED"})
                geom_row = {"fixture_id": fixture_id, "case": case, "target_date_local": target_s, "station_id": station, "member_id": member, "timezone": tz_name, "window": boundary, "required_source_leads": list(POINT_LEADS), "present_source_leads": sorted(objects), "boundary_leads_not_sampled": boundary["unrepresented_point_boundary_leads"], "exact_missing_boundary_leads": sorted({int((gap[1] // 3) * 3) if gap[1] % 3 == 0 else int(((gap[1] // 3) + 1) * 3) for gap in (tmax_cov["gaps"] + tmin_cov["gaps"]) if gap[1] > gap[0]}), "points": points, "high_f": max((x["value_f"] for x in points), default=None), "low_f": min((x["value_f"] for x in points), default=None), "tmax_coverage": tmax_cov, "tmin_coverage": tmin_cov, "native_crosscheck": cross, "geometry_ok": row_ok, "status": "PASS" if row_ok else "BLOCKED", "block_reasons": reasons, "grid_mapping": mapping}
                geometry.append(geom_row)
                row_summary.append({
                    "fixture_id": fixture_id,
                    "station_id": station,
                    "member_id": member,
                    "case": case,
                    "target_date_local": target_s,
                    "geometry_ok": row_ok,
                    "block_reasons": reasons,
                    "tmax_covered": tmax_cov["covered"],
                    "tmin_covered": tmin_cov["covered"],
                    "native_relation_failures": cross["relation_failures"],
                    "expected_point_count": len(expected),
                    "present_point_count": len(points),
                    "boundary_leads_not_sampled": boundary["unrepresented_point_boundary_leads"],
                    "exact_missing_boundary_leads": geom_row["exact_missing_boundary_leads"],
                })
                native_rows.append({"fixture_id": fixture_id, "station_id": station, "member_id": member, "tmax_coverage": tmax_cov, "tmin_coverage": tmin_cov, "crosscheck": cross})
    # Expected-missing fixture: preserve and verify exact declared 404 pair.
    expected_missing = [{"fixture_id": x.get("fixture_id"), "member_id": x.get("member_id"), "lead": int(x.get("lead", -1)), "kind": x.get("kind"), "status": x.get("status")} for x in manifest.get("failed", [])]
    expected_declared = sorted([{"fixture_id": "missing_expected_2020_01_15", "member_id": "gec00", "lead": 24, "kind": "grib2", "status": 404}, {"fixture_id": "missing_expected_2020_01_15", "member_id": "gec00", "lead": 24, "kind": "idx", "status": 404}], key=canon)
    expected_missing_ok = sorted(expected_missing, key=canon) == expected_declared
    if not expected_missing_ok:
        failures.append({"code": "EXPECTED_MISSING_CONTRACT_FAILURE", "observed": expected_missing, "expected": expected_declared})
    geometry.sort(key=lambda x: (x["fixture_id"], x["station_id"], x["member_id"]))
    object_rows.sort(key=lambda x: (x["fixture_id"], x["member_id"], x["lead"]))
    missingness.sort(key=canon)
    native_rows.sort(key=lambda x: (x["fixture_id"], x["station_id"], x["member_id"]))
    row_summary.sort(key=lambda x: (x["fixture_id"], x["station_id"], x["member_id"]))
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs = {
        "object_hash_lineage.jsonl": object_rows,
        "geometry_lineage.jsonl": geometry,
        "native_tmax_tmin_crosscheck.jsonl": native_rows,
        "missingness_ledger.jsonl": missingness,
        "row_summary.jsonl": row_summary,
    }
    for name, rows in outputs.items():
        (output_dir / name).write_bytes(b"".join(canon(x) for x in rows))
    repo_root = Path(__file__).resolve().parents[1]
    source_hashes = {"fixtures_sha256": sha_file(fixtures_path), "request_manifest_sha256": sha_file(manifest_path), "validator_sha256": sha_file(Path(__file__).resolve()), "spec_sha256": sha_file(repo_root / "docs/plans/GEFS-TEMPERATURE-GEOMETRY-FIX-SPEC-2026-08-15.md")}
    boundary_total = sum(len(x["boundary_leads_not_sampled"]) for x in row_summary)
    boundary_complete = sum(1 for x in row_summary if not x["boundary_leads_not_sampled"] and not x["exact_missing_boundary_leads"])
    counts = {"successful_raw_objects": sum(1 for x in manifest.get("requests", []) if x.get("kind") == "grib2" and x.get("status") == 200), "successful_idx_objects": sum(1 for x in manifest.get("requests", []) if x.get("kind") == "idx" and x.get("status") == 200), "included_fixtures": len(INCLUDED), "geometry_rows": len(geometry), "geometry_pass_rows": sum(x["geometry_ok"] for x in geometry), "native_crosscheck_rows": len(native_rows), "missingness_rows": len(missingness), "failure_count": len(failures), "expected_missing_ok": expected_missing_ok, "boundary_lead_completeness": {"rows_complete": boundary_complete, "rows_total": len(row_summary), "row_fraction": boundary_complete / len(row_summary) if row_summary else 0.0, "unrepresented_boundary_lead_count": boundary_total}}
    disposition = "PILOT_PASS" if not failures and len(geometry) == len(INCLUDED) * len(MEMBERS) * len(STATIONS) and all(x["geometry_ok"] for x in geometry) else "PILOT_BLOCKED"
    qc = {"version": VERSION, "scope": "read-only official GEFS raw-object station-local geometry; no scoring", "source_hashes": source_hashes, "counts": counts, "failures": sorted(failures, key=canon), "disposition": disposition, "no_scoring": True, "expected_missing": {"observed": expected_missing, "declared": expected_declared, "ok": expected_missing_ok}, "accuracy_suggestion": "On a separately approved holdout, compare trajectory-derived HIGH/LOW with native TMAX/TMIN extrema by station, event type, DST class, and boundary coverage; do not use that comparison to relax this gate."}
    (output_dir / "qc_report.json").write_bytes(canon(qc))
    return qc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path)
    args = ap.parse_args()
    qc = run(args.input.resolve(), args.output.resolve())
    print(json.dumps({"disposition": qc["disposition"], "counts": qc["counts"], "output": str(args.output / "qc_report.json")}, sort_keys=True))
    return 0 if qc["disposition"] == "PILOT_PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
