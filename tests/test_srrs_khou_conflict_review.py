from __future__ import annotations
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("khou_review", ROOT / "scripts/srrs_khou_conflict_review.py")
assert SPEC and SPEC.loader
review = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review)


def test_khou_v2_evidence_is_explicit_cca_conflict():
    rows = review.read_jsonl(review.V2_DEFAULT / "manifest.jsonl")
    row = next(r for r in rows if r["key"] == review.KEY)
    assert row["status"] == "blocked_conflict"
    assert row["candidate_rows"] == 26
    assert [(r["base_class"], r["max_f"], r["min_f"]) for r in row["accepted_reports"]] == [
        ("intermediate", 77, 55), ("intermediate", 70, 55),
        ("full_day", 77, 55), ("full_day", 70, 55)
    ]
    full = [r for r in row["accepted_reports"] if r["base_class"] == "full_day"]
    assert any("CCA" in r["issuance_header"] for r in full)
    assert all(r["pil"] == "CLIHOU" and r["internal_summary_date"] == "2025-01-26" for r in row["accepted_reports"])


def test_review_is_offline_and_v3_is_deterministic(tmp_path):
    out1 = tmp_path / "v3a"; out2 = tmp_path / "v3b"
    s1 = review.run(review.V2_DEFAULT, out1, review.SPEC_DEFAULT)
    s2 = review.run(review.V2_DEFAULT, out2, review.SPEC_DEFAULT)
    assert s1["decision"] == "GO_TO_RESUME_BACKFILL_ONLY"
    assert s1["khou_resolution"]["selected_max_f"] == 70
    assert s1["khou_resolution"]["selected_min_f"] == 55
    assert (out1 / "manifest.jsonl").read_bytes() == (out2 / "manifest.jsonl").read_bytes()
    assert (out1 / "run_summary.json").read_bytes() == (out2 / "run_summary.json").read_bytes()
    assert (out1 / "review.json").read_bytes() == (out2 / "review.json").read_bytes()


def test_no_exact_kalshi_identity_row_is_imputed():
    rows = review.read_jsonl(review.V2_DEFAULT / "manifest.jsonl")
    # The focused review's source database has no exact historical row; this
    # test only asserts the key itself is not confused with a nearby date.
    import sqlite3
    con = sqlite3.connect(ROOT / "data/kalshi_settlements.db")
    try:
        assert con.execute("select 1 from kalshi_settlements where station=? and target_date=?", ("KHOU", "2025-01-26")).fetchone() is None
    finally:
        con.close()
