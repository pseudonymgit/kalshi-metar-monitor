#!/usr/bin/env python3
"""
validate_data_quality.py — Data quality validation that runs BEFORE every sweep.

Fails hard if any data source violates its manifest requirements.
This is the prevention mechanism for the "incomplete backfill" class of bugs.
"""

import json, os, sqlite3, sys
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(REPO, "data")
MANIFEST = os.path.join(DATA, "data_quality_manifest.json")


def _load_manifest(path):
    """Load manifest, stripping JS-style comments (lines starting with # or //)."""
    with open(path) as f:
        lines = []
        for line in f:
            stripped = line.strip()
            if stripped.startswith('#') or stripped.startswith('//'):
                continue
            lines.append(line)
    return json.loads(''.join(lines))


def check():
    """Validate all data sources against the manifest. Returns (failures, warnings)."""
    manifest = _load_manifest(MANIFEST)

    failures = []
    warnings_list = []
    total_checks = 0

    for db_rel_path, requirements in manifest.items():
        db_path = os.path.join(REPO, db_rel_path) if not os.path.isabs(db_rel_path) else db_rel_path
        label = requirements.get("description", db_rel_path)
        print(f"\n{'=' * 60}")
        print(f"  {label}")
        print(f"  {db_rel_path}")
        print(f"{'=' * 60}")

        if not os.path.exists(db_path):
            msg = f"DB NOT FOUND: {db_path}"
            print(f"  ❌ {msg}")
            failures.append(msg)
            continue

        try:
            db = sqlite3.connect(db_path)
            db.execute("PRAGMA busy_timeout=5000")

            # ── Row count check ──
            if "min_rows" in requirements:
                total_checks += 1
                cnt = db.execute(f"SELECT COUNT(*) FROM (SELECT name FROM sqlite_master WHERE type='table' LIMIT 1)").fetchone()
                # Get the actual table name
                table = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1").fetchone()
                if table:
                    cnt = db.execute(f'SELECT COUNT(*) FROM "{table[0]}"').fetchone()[0]
                    min_rows = requirements["min_rows"]
                    status = "✅" if cnt >= min_rows else "❌ FAIL"
                    if cnt < min_rows:
                        failures.append(f"{db_rel_path}: {cnt:,} rows < minimum {min_rows:,}")
                    print(f"  {status} Row count: {cnt:,} (min {min_rows:,})")
                else:
                    print(f"  ⚠️  No tables found in {db_path}")

            # ── Date range check ──
            if "min_date_range" in requirements:
                total_checks += 1
                if table:
                    date_col = None
                    for col_candidate in ["target_date", "date"]:
                        try:
                            row = db.execute(f'SELECT MIN({col_candidate}), MAX({col_candidate}) FROM "{table[0]}"').fetchone()
                            if row and row[0]:
                                date_col = col_candidate
                                break
                        except:
                            continue
                    if date_col:
                        actual_min, actual_max = db.execute(f'SELECT MIN({date_col}), MAX({date_col}) FROM "{table[0]}"').fetchone()
                        required_min, required_max = requirements["min_date_range"]
                        ok_min = actual_min <= required_min
                        ok_max = actual_max >= required_max
                        if ok_min and ok_max:
                            print(f"  ✅ Date range: {actual_min} → {actual_max} (requires {required_min} → {required_max})")
                        else:
                            msg = f"{db_rel_path}: date range {actual_min} → {actual_max} doesn't cover {required_min} → {required_max}"
                            print(f"  ❌ FAIL {msg}")
                            failures.append(msg)
                    else:
                        print(f"  ⚠️  No date column found")

            # ── Per-station min rows ──
            if "per_station_min_rows" in requirements:
                total_checks += 1
                min_rows = requirements["per_station_min_rows"]
                if table:
                    try:
                        station_col = "station" if "station" in [d[1] for d in db.execute(f'PRAGMA table_info("{table[0]}")').fetchall()] else None
                        if station_col:
                            low_stations = db.execute(f'SELECT "{station_col}", COUNT(*) FROM "{table[0]}" GROUP BY "{station_col}" HAVING COUNT(*) < ?', (min_rows,)).fetchall()
                            if low_stations:
                                msg = f"{len(low_stations)} stations below minimum {min_rows} rows: {[r[0] for r in low_stations[:5]]}"
                                print(f"  ❌ FAIL {msg}")
                                failures.append(msg)
                            else:
                                print(f"  ✅ All stations ≥ {min_rows} rows")
                        else:
                            print(f"  ⚠️  No station column found")
                    except:
                        print(f"  ⚠️  Station check failed")

            # ── Member count checks ──
            for check_type in ["member_checks", "blob_checks"]:
                for ck in requirements.get(check_type, []):
                    total_checks += 1
                    try:
                        rows = db.execute(ck["sql"]).fetchone()
                        count = rows[0] if rows else 0
                        max_ok = ck.get("max_allowed", 0)
                        passed = count <= max_ok
                        if passed:
                            print(f"  ✅ {ck['description']}: {count} violations (max {max_ok})")
                        else:
                            code = "❌ FAIL" if ck.get("severity") == "fail" else "⚠️ WARN"
                            msg = f"{ck['description']}: {count:,} violations (max {max_ok})"
                            print(f"  {code} {msg}")
                            if ck.get("severity") == "fail":
                                failures.append(msg)
                            else:
                                warnings_list.append(msg)
                    except Exception as e:
                        print(f"  ⚠️  Check failed: {e}")

            # ── Unique pairs check ──
            for ck in requirements.get("unique_pairs", []):
                total_checks += 1
                try:
                    duplicates = db.execute(ck["sql"]).fetchall()
                    count = len(duplicates)
                    max_ok = ck.get("max_allowed", 0)
                    passed = count <= max_ok
                    if passed:
                        print(f"  ✅ {ck['description']}: {count} duplicates (max {max_ok})")
                    else:
                        code = "❌ FAIL" if ck.get("severity") == "fail" else "⚠️ WARN"
                        print(f"  {code} {ck['description']}: {count} duplicates")
                        if ck.get("severity") == "fail":
                            failures.append(msg := f"{db_rel_path}: {count} duplicate pairs")
                except Exception as e:
                    print(f"  ⚠️  Check failed: {e}")

            db.close()

        except Exception as e:
            msg = f"{db_rel_path}: Error connecting: {e}"
            print(f"  ❌ {msg}")
            failures.append(msg)

    # ── Summary ──
    print(f"\n{'=' * 60}")
    print(f"  DATA QUALITY SUMMARY")
    print(f"{'=' * 60}")
    print(f"  Checks run: {total_checks}")
    print(f"  Failures:   {len(failures)}")
    print(f"  Warnings:   {len(warnings_list)}")

    if failures:
        print(f"\n  ❌ {len(failures)} FAILURES — CANNOT SWEEP")
        for f in failures:
            print(f"     - {f}")
        return 1
    else:
        w = len(warnings_list)
        print(f"\n  ✅ ALL CHECKS PASSED{' (with ' + str(w) + ' warnings)' if w else ''}")
        return 0


if __name__ == "__main__":
    sys.exit(check())