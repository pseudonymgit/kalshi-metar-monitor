#!/usr/bin/env python3
"""Focused, offline KHOU SRRS conflict review.

Reads only the retained v2 manifest/raw evidence. It writes a new v3 manifest
and deterministic review artifacts; it never mutates v2 and never performs
network acquisition.
"""
from __future__ import annotations
import argparse, copy, hashlib, importlib.util, json, re, shutil
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
V2_DEFAULT = ROOT / "data/raw/srrs_cli_backfill/2026-08-15-v2-full"
V3_DEFAULT = ROOT / "data/raw/srrs_cli_backfill/2026-08-15-khou-conflict-v3"
SPEC_DEFAULT = ROOT / "docs/plans/SRRS-KHOU-CONFLICT-REVIEW-SPEC-2026-08-15.md"
KEY = "KHOU|2025-01-26"


def sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha_file(p: Path) -> str:
    return sha_bytes(p.read_bytes())


def canon(obj) -> bytes:
    return (json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n").encode()


def read_jsonl(p: Path):
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def atomic_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_bytes(json.dumps(obj, indent=2, sort_keys=True).encode() + b"\n")
    tmp.replace(p)


def issue_key(header: str) -> tuple[int, int]:
    # WMO header CDUS44 KHGX DDHHMM [CCA]. Within this one target month,
    # DDHHMM is sufficient and is used only to prove the explicit CCA order.
    m = re.search(r"\bKHGX\s+(\d{6})\b", header)
    if not m:
        raise AssertionError(f"missing KHGX issue timestamp: {header!r}")
    x = m.group(1)
    return int(x[:2]), int(x[2:])


def run(v2: Path, v3: Path, spec: Path) -> dict:
    manifest_path = v2 / "manifest.jsonl"
    rows = read_jsonl(manifest_path)
    by_key = {r.get("key"): r for r in rows}
    if len(rows) != len(by_key) or KEY not in by_key:
        raise AssertionError("v2 manifest key uniqueness/KHOU row check failed")
    original_v2_manifest_sha = sha_file(manifest_path)
    original_v2_summary_sha = sha_file(v2 / "run_summary.json")
    khou = by_key[KEY]
    if khou.get("status") != "blocked_conflict":
        raise AssertionError(f"expected v2 blocked_conflict, got {khou.get('status')}")
    if khou.get("candidate_rows") != len(khou.get("accepted_reports", [])) + len(khou.get("failures", [])):
        raise AssertionError("candidate-row accounting failed")

    reports = khou["accepted_reports"]
    full = [r for r in reports if r.get("base_class") == "full_day"]
    inter = [r for r in reports if r.get("base_class") == "intermediate"]
    if len(full) != 2 or len(inter) != 2:
        raise AssertionError(f"expected 2 full-day + 2 intermediate, got {len(full)} + {len(inter)}")

    checked = []
    for r in reports:
        p = v2 / r["raw_path"]
        if not p.is_file():
            raise AssertionError(f"missing retained raw: {p}")
        actual = sha_file(p)
        if actual != r["raw_sha256"]:
            raise AssertionError(f"raw hash mismatch: {p}")
        text = p.read_text(errors="replace")
        if "CLIHOU" not in text[:400] or r.get("internal_summary_date") != "2025-01-26":
            raise AssertionError("accepted KHOU identity/date check failed")
        checked.append((r, text))

    cca = [r for r, text in checked if re.search(r"\bCCA\b", r.get("issuance_header", ""))]
    plain = [r for r, text in checked if not re.search(r"\bCCA\b", r.get("issuance_header", "")) and r.get("base_class") == "full_day"]
    if len(cca) != 1 or len(plain) != 1:
        raise AssertionError(f"explicit CCA/full-day predecessor check failed: cca={len(cca)} plain={len(plain)}")
    corrected, superseded = cca[0], plain[0]
    if issue_key(corrected["issuance_header"]) <= issue_key(superseded["issuance_header"]):
        raise AssertionError("CCA report is not later than superseded report")
    if tuple((corrected.get("pil"), corrected.get("internal_summary_date"))) != tuple((superseded.get("pil"), superseded.get("internal_summary_date"))):
        raise AssertionError("CCA and predecessor identity differs")
    if (corrected.get("max_f"), corrected.get("min_f")) == (superseded.get("max_f"), superseded.get("min_f")):
        raise AssertionError("fixture no longer represents a conflict")

    # Build a new v3 manifest. All other keys are copied byte-for-byte as JSON
    # records; only the reviewed key receives explicit, evidence-backed labels.
    v3rows = copy.deepcopy(rows)
    v3k = next(r for r in v3rows if r["key"] == KEY)
    for r in v3k["accepted_reports"]:
        r["settlement_selected"] = r["raw_sha256"] == corrected["raw_sha256"]
        if r["raw_sha256"] == corrected["raw_sha256"]:
            r["revision"] = True
            r.setdefault("lineage_labels", []).extend(x for x in ["revision", "official_cca_correction", "supersedes:" + superseded["raw_sha256"]] if x not in r.setdefault("lineage_labels", []))
        elif r["raw_sha256"] == superseded["raw_sha256"]:
            r.setdefault("lineage_labels", []).extend(x for x in ["superseded_by:" + corrected["raw_sha256"], "official_cca_correction_predecessor"] if x not in r.setdefault("lineage_labels", []))
    v3k["status"] = "accepted_full_day"
    v3k["selected_report"] = next(r for r in v3k["accepted_reports"] if r["settlement_selected"])
    v3k["lineage"] = {
        "status": "accepted_full_day", "full_day_count": 2,
        "intermediate_count": 2, "duplicate_content_count": 0,
        "conflict_count": 0, "resolved_conflict_count": 1,
        "resolution": "official_cca_correction",
        "superseded_raw_sha256": superseded["raw_sha256"],
        "selected_raw_sha256": corrected["raw_sha256"],
    }
    v3.mkdir(parents=True, exist_ok=True)
    (v3 / "raw").mkdir(exist_ok=True)
    # v3 carries immutable references, and also copies the four reviewed raw
    # bodies so the focused artifact is independently inspectable.
    for r, _ in checked:
        src = v2 / r["raw_path"]
        dst = v3 / r["raw_path"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())
    out_manifest = v3 / "manifest.jsonl"
    out_manifest.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in v3rows))

    status_counts = Counter(r.get("status") for r in v3rows)
    classes = Counter()
    raw_hashes = Counter()
    raw_bytes = 0
    for row in v3rows:
        for r in row.get("accepted_reports", []):
            classes[r.get("base_class", r.get("class"))] += 1
            raw_hashes[r.get("raw_sha256")] += 1
            raw_bytes += int(r.get("raw_bytes", 0))
    kalshi_exact = False
    db = ROOT / "data/kalshi_settlements.db"
    if db.exists():
        import sqlite3
        con = sqlite3.connect(db)
        try:
            kalshi_exact = con.execute("select 1 from kalshi_settlements where station=? and target_date=? limit 1", ("KHOU", "2025-01-26")).fetchone() is not None
        finally:
            con.close()
    summary = {
        "schema_version": 3, "artifact": "srrs-khou-conflict-review-v3",
        "scope": {"station": "KHOU", "target_date": "2025-01-26", "keys": len(v3rows)},
        "counts": {"status_counts": dict(sorted(status_counts.items())), "station_date_keys": len(v3rows), "accepted_full_day": status_counts["accepted_full_day"], "blocked_conflict": status_counts["blocked_conflict"], "full_day_retained": classes["full_day"], "intermediate_retained": classes["intermediate"], "raw_body_occurrences": sum(raw_hashes.values()), "raw_unique_bodies": len(raw_hashes), "raw_duplicate_content_occurrences": sum(n-1 for n in raw_hashes.values() if n > 1), "raw_bytes": raw_bytes, "khou_candidate_rows": khou["candidate_rows"], "khou_failures_retained": len(khou["failures"]), "khou_conflict_resolved": 1},
        "khou_resolution": {"status": "accepted_full_day", "policy": "official_cca_correction", "selected_max_f": corrected["max_f"], "selected_min_f": corrected["min_f"], "selected_raw_sha256": corrected["raw_sha256"], "superseded_raw_sha256": superseded["raw_sha256"], "selected_issue": corrected["issuance_header"], "superseded_issue": superseded["issuance_header"]},
        "kalshi_identity": {"series": "KXHIGHTHOU", "issuedby": "HOU", "exact_settlement_row_present": kalshi_exact, "disposition": "not_available" if not kalshi_exact else "descriptive_only"},
        "v2_stale_summary": {"path": str(v2 / "run_summary.json"), "sha256": original_v2_summary_sha, "warning": "v2-full run_summary.json predates the final 280-key state and must not be used as the current summary"},
        "integrity": {"v2_manifest_sha256": original_v2_manifest_sha, "v2_summary_sha256": original_v2_summary_sha, "v3_manifest_sha256": sha_file(out_manifest), "spec_sha256": sha_file(spec)},
        "decision": "GO_TO_RESUME_BACKFILL_ONLY",
        "accuracy_suggestion": "After separate source acceptance, compare the selected corrected CLI value with delayed finalized official data; retain both hashes and the CCA lineage rather than overwriting history.",
    }
    atomic_json(v3 / "run_summary.json", summary)
    checkpoint = {"schema_version": 3, "artifact": "srrs-khou-conflict-review-v3", "source_v2_manifest_sha256": original_v2_manifest_sha, "manifest_sha256": sha_file(out_manifest), "keys": len(v3rows), "status_counts": dict(sorted(status_counts.items()))}
    atomic_json(v3 / "checkpoint.json", checkpoint)
    review = {"schema_version": 1, "decision": "GO_TO_RESUME_BACKFILL_ONLY", "key": KEY, "classification": "explicit_official_revision", "policy": "official_cca_correction", "evidence": {"superseded": {"issue": superseded["issuance_header"], "values": [superseded["max_f"], superseded["min_f"]], "raw_sha256": superseded["raw_sha256"]}, "corrected": {"issue": corrected["issuance_header"], "values": [corrected["max_f"], corrected["min_f"]], "raw_sha256": corrected["raw_sha256"]}}, "escalation_if_rejected": "If CCA is not accepted as an official supersession marker, do not resume; escalate both full-day hashes/values to NWS/NCEI for an authoritative correction relation.", "v2_immutable_hash": original_v2_manifest_sha}
    atomic_json(v3 / "review.json", review)
    return summary


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--v2", default=str(V2_DEFAULT)); p.add_argument("--v3", default=str(V3_DEFAULT)); p.add_argument("--spec", default=str(SPEC_DEFAULT))
    args = p.parse_args(); print(json.dumps(run(Path(args.v2), Path(args.v3), Path(args.spec)), sort_keys=True))

if __name__ == "__main__": main()
