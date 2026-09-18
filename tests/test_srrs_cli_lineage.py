from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("fetch_srrs_cli", ROOT / "scripts" / "fetch_srrs_cli.py")
assert SPEC and SPEC.loader
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)

RAW = ROOT / "data" / "raw" / "srrs_cli_backfill" / "2026-08-15" / "raw"


def _body(station: str, semantic: str) -> str:
    pil = cli.STATIONS[station][2]
    city = {"KATL": "ATLANTA", "KAUS": "AUSTIN", "KDFW": "DALLAS", "KDEN": "DENVER", "KHOU": "HOUSTON"}[station]
    period = "YESTERDAY" if semantic == "full_day" else "TODAY"
    valid = "" if semantic == "full_day" else "\nVALID AS OF 0800 PM LOCAL TIME."
    return f"CDUS42 KXXX 160900\n\n{pil}\n\nCLIMATE REPORT\n\n...THE {city} CLIMATE SUMMARY FOR JANUARY 15 2025...{valid}\n\nTEMPERATURE (F)\n\n {period}\n  MAXIMUM         63\n  MINIMUM         35\nPRECIPITATION (IN)\n"


def _record(station: str, body: str, name: str, *, max_f: int = 63, min_f: int = 35):
    return {
        "station": station,
        "target_date": "2025-01-15",
        "class": cli.classify(body),
        "base_class": cli.classify(body),
        "raw_path": name,
        "raw_sha256": cli.sha(body.encode()),
        "max_f": max_f,
        "min_f": min_f,
        "issuance_header": name,
    }


def test_same_date_intermediate_and_full_day_is_accepted_lineage():
    for station in ("KATL", "KDFW", "KAUS", "KDEN", "KHOU"):
        intermediate = _record(station, _body(station, "intermediate"), f"{station}-intermediate")
        full_day = _record(station, _body(station, "full_day"), f"{station}-full-day")
        result = cli.select_lineage([intermediate, full_day])
        assert result["status"] == "accepted_full_day"
        assert result["selected"]["base_class"] == "full_day"
        assert sum(r["base_class"] == "intermediate" for r in result["reports"]) == 1
        assert sum(r["settlement_selected"] for r in result["reports"]) == 1


def test_conflicting_full_day_revisions_remain_blocked():
    a = _record("KATL", _body("KATL", "full_day") + "A", "a", max_f=52)
    b = _record("KATL", _body("KATL", "full_day") + "B", "b", max_f=53)
    result = cli.select_lineage([a, b])
    assert result["status"] == "blocked_conflicting_full_day_revisions"
    assert result["selected"] is None
    assert result["conflict_count"] == 2


def test_unclassifiable_raw_text_is_not_inferred_from_size():
    raw = {"raw_sha256": "x", "raw_path": "opaque", "class": "unresolved", "base_class": "unresolved"}
    result = cli.select_lineage([raw])
    assert result["status"] == "blocked_unresolved_lineage"


def test_cached_station_examples_select_full_day():
    expected = {"KATL": 52, "KDFW": 62, "KAUS": 63, "KDEN": 49, "KHOU": 54}
    for station, max_f in expected.items():
        files = sorted(RAW.glob(f"{station}_2025-01-15_*.raw"))
        records = []
        for path in files:
            text = path.read_text(errors="replace")
            parsed = cli.report_classification(text)
            if parsed["base_class"] not in {"intermediate", "full_day"}:
                continue
            records.append({
                "station": station,
                "target_date": "2025-01-15",
                "class": parsed["base_class"],
                "base_class": parsed["base_class"],
                "revision": parsed["revision"],
                "raw_path": str(path),
                "raw_sha256": cli.sha(path.read_bytes()),
                "max_f": cli.parse_temp(text, "MAXIMUM"),
                "min_f": cli.parse_temp(text, "MINIMUM"),
                "issuance_header": text.splitlines()[0],
            })
        result = cli.select_lineage(records)
        assert result["status"] == "accepted_full_day"
        assert result["selected"]["max_f"] == max_f
        assert any(r["base_class"] == "intermediate" for r in result["reports"])
