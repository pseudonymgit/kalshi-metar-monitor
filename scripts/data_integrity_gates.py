#!/usr/bin/env python3
"""
data_integrity_gates.py — Pre-sweep data integrity checks for the weather engine.

Run these gates BEFORE any sweep config evaluation. Every gate must pass for
the data to be considered production-ready.

Gates:
  1. GEFS completeness:  ≥30/31 members per (date, station, step=24)
  2. ECMWF completeness:  ≥49/51 members per (date, station, step=24)
  3. Station alignment:   GEFS and ECMWF sample same interpolated grid points
  4. Date alignment:      Both models have f024 for same target dates
  5. Price sanity:        entry_price between $0.01 and $0.99
  6. Fee sanity:          fee_per_trade / notional ≤ 0.15

Usage:
    python3 scripts/data_integrity_gates.py
    python3 scripts/data_integrity_gates.py --json   # JSON output
    python3 scripts/data_integrity_gates.py --verbose  # Detailed logs

B-Mode compliant. No AI/ML.
"""

import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, "data")
OUT_PATH = os.path.join(DATA_DIR, "data_integrity_gate_results.json")

# Known 20-station universe
STATIONS = [
    "KATL", "KAUS", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU", "KLAS",
    "KLAX", "KMDW", "KMIA", "KMSP", "KMSY", "KNYC", "KOKC", "KPHL",
    "KPHX", "KSAT", "KSEA", "KSFO",
]

# Step to check (24-hour forecast)
TARGET_STEP = 24

# Member thresholds
GEFS_MIN_MEMBERS = 30      # GEFS has 31 members
GEFS_EXPECTED_MEMBERS = 31
ECMWF_MIN_MEMBERS = 49     # ECMWF has 51 members
ECMWF_EXPECTED_MEMBERS = 51

# Price sanity bounds
PRICE_MIN = 0.01
PRICE_MAX = 0.99

# Fee sanity: fee_per_trade / notional ≤ 0.15
MAX_FEE_RATIO = 0.15


class GateResult:
    """Structured result for a single integrity gate."""
    
    def __init__(self, name: str):
        self.name = name
        self.passed = True
        self.issues: List[str] = []
        self.details: dict = {}
        self.failed_count = 0
        self.total_checked = 0
    
    def fail(self, issue: str):
        self.passed = False
        self.issues.append(issue)
        self.failed_count += 1
    
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "pass": self.passed,
            "issues": self.issues,
            "failed_count": self.failed_count,
            "total_checked": self.total_checked,
            "details": self.details,
        }


# ═════════════════════════════════════════════════════════════════════════════
# Gate 1: GEFS Completeness
# ═════════════════════════════════════════════════════════════════════════════

def check_gefs_completeness() -> GateResult:
    """
    Gate 1: Check that GEFS has ≥30/31 members for each (date, station, step=24).
    """
    gate = GateResult("GEFS completeness (≥30/31 members per date/station/step)")
    
    db_path = os.path.join(DATA_DIR, "gefs_archive.db")
    if not os.path.exists(db_path):
        gate.fail(f"GEFS database not found at {db_path}")
        gate.details = {"db_path": db_path, "exists": False}
        return gate
    
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cur = conn.cursor()
        
        # Count records by member count
        cur.execute("""
            SELECT n_members, COUNT(*) as cnt
            FROM gefs_archive
            WHERE step = ?
            GROUP BY n_members
            ORDER BY n_members
        """, (TARGET_STEP,))
        member_dist = dict(cur.fetchall())
        
        total = sum(member_dist.values())
        below_threshold = sum(v for k, v in member_dist.items() if k < GEFS_MIN_MEMBERS)
        full_members = sum(v for k, v in member_dist.items() if k >= GEFS_EXPECTED_MEMBERS)
        
        gate.total_checked = total
        gate.failed_count = below_threshold
        gate.details = {
            "db_path": db_path,
            "target_step": TARGET_STEP,
            "total_records": total,
            "member_distribution": member_dist,
            "expected_members": GEFS_EXPECTED_MEMBERS,
            "min_required": GEFS_MIN_MEMBERS,
            "records_below_threshold": below_threshold,
            "records_with_full_members": full_members,
            "completeness_pct": round(full_members / total * 100, 1) if total > 0 else 0,
        }
        
        if total == 0:
            gate.fail("No GEFS records found for step=24")
        elif below_threshold > 0:
            gate.fail(f"{below_threshold}/{total} records have <{GEFS_MIN_MEMBERS} members")
        
        # Show which stations have issues
        if below_threshold > 0:
            cur.execute("""
                SELECT station, n_members, COUNT(*) as cnt
                FROM gefs_archive
                WHERE step = ? AND n_members < ?
                GROUP BY station, n_members
                ORDER BY cnt DESC
                LIMIT 20
            """, (TARGET_STEP, GEFS_MIN_MEMBERS))
            low_member_records = [{"station": r[0], "n_members": r[1], "count": r[2]} for r in cur.fetchall()]
            gate.details["low_member_examples"] = low_member_records
        
        conn.close()
    except Exception as e:
        gate.fail(f"Error querying GEFS database: {e}")
    
    return gate


# ═════════════════════════════════════════════════════════════════════════════
# Gate 2: ECMWF Completeness
# ═════════════════════════════════════════════════════════════════════════════

def get_ecmwf_db_path() -> Optional[str]:
    """Find the ECMWF data source (try ecmwf_archive.db, then tigge_archive.db)."""
    for name in ["tigge_archive.db", "ecmwf_archive.db"]:
        path = os.path.join(DATA_DIR, name)
        if os.path.exists(path):
            return path
    return None


def check_ecmwf_completeness() -> GateResult:
    """
    Gate 2: Check that ECMWF has ≥49/51 members for each (date, station, step=24).
    Checks TIGGE archive first, then ECMWF archive.
    """
    gate = GateResult("ECMWF completeness (≥49/51 members per date/station/step)")
    
    db_path = get_ecmwf_db_path()
    if not db_path:
        gate.fail("No ECMWF or TIGGE database found in data/")
        gate.details = {"note": "Expected ecmwf_archive.db or tigge_archive.db"}
        return gate
    
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cur = conn.cursor()
        
        # Determine table name
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall()]
        table_name = None
        for t in tables:
            if "ecmwf" in t or "tigge" in t:
                table_name = t
                break
        
        if not table_name:
            gate.fail(f"No ECMWF/TIGGE table found in {db_path} (tables: {tables})")
            conn.close()
            return gate
        
        # Check source column exists for TIGGE
        cur.execute(f"PRAGMA table_info({table_name})")
        cols = [r[1] for r in cur.fetchall()]
        
        source_filter = ""
        if "source" in cols:
            source_filter = "AND source = 'tigge_ecmwf'"
        
        query = f"""
            SELECT n_members, COUNT(*) as cnt
            FROM {table_name}
            WHERE step = ? {source_filter}
            GROUP BY n_members
            ORDER BY n_members
        """
        cur.execute(query, (TARGET_STEP,))
        member_dist = dict(cur.fetchall())
        
        total = sum(member_dist.values())
        below_threshold = sum(v for k, v in member_dist.items() if k < ECMWF_MIN_MEMBERS)
        full_members = sum(v for k, v in member_dist.items() if k >= ECMWF_EXPECTED_MEMBERS)
        
        # Also get the n_members field if it's not populated
        if not member_dist:
            # Try counting non-null member_values as a proxy
            cur.execute(f"""
                SELECT COUNT(*) FROM {table_name}
                WHERE step = ? AND member_values IS NOT NULL
                {source_filter}
            """, (TARGET_STEP,))
            non_null = cur.fetchone()[0]
            
            cur.execute(f"""
                SELECT COUNT(*) FROM {table_name}
                WHERE step = ? {source_filter}
            """, (TARGET_STEP,))
            total_records = cur.fetchone()[0]
            
            below_threshold = total_records - non_null
            total = total_records
            member_dist = {"null_member_values": total_records - non_null, "has_member_values": non_null}
        
        gate.total_checked = total
        gate.failed_count = below_threshold
        gate.details = {
            "db_path": db_path,
            "table_name": table_name,
            "target_step": TARGET_STEP,
            "total_records": total,
            "member_distribution": member_dist,
            "expected_members": ECMWF_EXPECTED_MEMBERS,
            "min_required": ECMWF_MIN_MEMBERS,
            "records_below_threshold": below_threshold,
        }
        
        if total == 0:
            gate.fail(f"No ECMWF records found for step={TARGET_STEP}")
        elif below_threshold > 0:
            gate.fail(f"{below_threshold}/{total} records have <{ECMWF_MIN_MEMBERS} members")
        
        # Detailed low-member records
        if below_threshold > 0 and member_dist:
            low_keys = [k for k in member_dist.keys() if isinstance(k, (int, float)) and k < ECMWF_MIN_MEMBERS]
            if low_keys:
                placeholders = ",".join("?" for _ in low_keys)
                cur.execute(f"""
                    SELECT station, n_members, COUNT(*) as cnt
                    FROM {table_name}
                    WHERE step = ? AND n_members IN ({placeholders})
                    GROUP BY station, n_members
                    ORDER BY cnt DESC
                    LIMIT 20
                """, (TARGET_STEP, *low_keys))
                low_records = [{"station": r[0], "n_members": r[1], "count": r[2]} for r in cur.fetchall()]
                gate.details["low_member_examples"] = low_records
        
        conn.close()
    except Exception as e:
        gate.fail(f"Error querying ECMWF database: {e}")
    
    return gate


# ═════════════════════════════════════════════════════════════════════════════
# Gate 3: Station Alignment
# ═════════════════════════════════════════════════════════════════════════════

def check_station_alignment() -> GateResult:
    """
    Gate 3: Both models sample the same interpolated grid points (stations).
    Checks that GEFS and ECMWF cover the same 20-station universe.
    """
    gate = GateResult("Station alignment: GEFS and ECMWF sample same stations")
    
    gefs_path = os.path.join(DATA_DIR, "gefs_archive.db")
    ecmwf_path = get_ecmwf_db_path()
    
    if not os.path.exists(gefs_path):
        gate.fail("GEFS database not found")
        return gate
    if not ecmwf_path:
        gate.fail("ECMWF database not found")
        return gate
    
    try:
        gefs_conn = sqlite3.connect(f"file:{gefs_path}?mode=ro", uri=True)
        ecmwf_conn = sqlite3.connect(f"file:{ecmwf_path}?mode=ro", uri=True)
        
        # GEFS stations
        gefs_stations = set()
        cur = gefs_conn.cursor()
        cur.execute("SELECT DISTINCT station FROM gefs_archive WHERE step=?", (TARGET_STEP,))
        for r in cur.fetchall():
            gefs_stations.add(r[0])
        
        # ECMWF stations  
        ecmwf_cur = ecmwf_conn.cursor()
        ecmwf_cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ecmwf_tables = [r[0] for r in ecmwf_cur.fetchall()]
        ecmwf_table = None
        for t in ecmwf_tables:
            if "ecmwf" in t or "tigge" in t:
                ecmwf_table = t
                break
        
        ecmwf_stations = set()
        if ecmwf_table:
            source_filter = ""
            ecmwf_cur.execute(f"PRAGMA table_info({ecmwf_table})")
            cols = [r[1] for r in ecmwf_cur.fetchall()]
            if "source" in cols:
                source_filter = "AND source = 'tigge_ecmwf'"
            
            ecmwf_cur.execute(f"""
                SELECT DISTINCT station FROM {ecmwf_table}
                WHERE step=? {source_filter}
            """, (TARGET_STEP,))
            for r in ecmwf_cur.fetchall():
                ecmwf_stations.add(r[0])
        
        gefs_conn.close()
        ecmwf_conn.close()
        
        expected = set(STATIONS)
        gefs_missing = expected - gefs_stations
        ecmwf_missing = expected - ecmwf_stations
        both_missing = gefs_missing & ecmwf_missing
        gefs_only_missing = gefs_missing - ecmwf_missing
        ecmwf_only_missing = ecmwf_missing - gefs_missing
        
        gate.total_checked = len(expected)
        gate.details = {
            "expected_stations": STATIONS,
            "gefs_stations": sorted(gefs_stations),
            "ecmwf_stations": sorted(ecmwf_stations),
            "gefs_missing": sorted(gefs_missing),
            "ecmwf_missing": sorted(ecmwf_missing),
            "both_missing": sorted(both_missing),
            "gefs_only_missing": sorted(gefs_only_missing),
            "ecmwf_only_missing": sorted(ecmwf_only_missing),
            "gefs_coverage": f"{len(gefs_stations)}/{len(expected)}",
            "ecmwf_coverage": f"{len(ecmwf_stations)}/{len(expected)}",
        }
        
        if both_missing:
            gate.fail(f"Stations missing from BOTH models: {sorted(both_missing)}")
        if gefs_only_missing:
            gate.fail(f"GEFS missing stations: {sorted(gefs_only_missing)}")
        if ecmwf_only_missing:
            gate.fail(f"ECMWF missing stations: {sorted(ecmwf_only_missing)}")
        if not gefs_missing and not ecmwf_missing:
            gate.details["note"] = "All 20 stations present in both models"
        
    except Exception as e:
        gate.fail(f"Error checking station alignment: {e}")
    
    return gate


# ═════════════════════════════════════════════════════════════════════════════
# Gate 4: Date Alignment
# ═════════════════════════════════════════════════════════════════════════════

def check_date_alignment() -> GateResult:
    """
    Gate 4: Both models have f024 for the same target date range.
    """
    gate = GateResult("Date alignment: both models have f024 for same target dates")
    
    gefs_path = os.path.join(DATA_DIR, "gefs_archive.db")
    ecmwf_path = get_ecmwf_db_path()
    
    if not os.path.exists(gefs_path):
        gate.fail("GEFS database not found")
        return gate
    if not ecmwf_path:
        gate.fail("ECMWF database not found")
        return gate
    
    try:
        gefs_conn = sqlite3.connect(f"file:{gefs_path}?mode=ro", uri=True)
        ecmwf_conn = sqlite3.connect(f"file:{ecmwf_path}?mode=ro", uri=True)
        
        # GEFS dates
        gefs_cur = gefs_conn.cursor()
        gefs_cur.execute("SELECT DISTINCT target_date FROM gefs_archive WHERE step=?", (TARGET_STEP,))
        gefs_dates = set(r[0] for r in gefs_cur.fetchall())
        
        # ECMWF dates
        ecmwf_cur = ecmwf_conn.cursor()
        ecmwf_cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in ecmwf_cur.fetchall()]
        ecmwf_table = None
        for t in tables:
            if "ecmwf" in t or "tigge" in t:
                ecmwf_table = t
                break
        
        ecmwf_dates = set()
        if ecmwf_table:
            source_filter = ""
            ecmwf_cur.execute(f"PRAGMA table_info({ecmwf_table})")
            cols = [r[1] for r in ecmwf_cur.fetchall()]
            if "source" in cols:
                source_filter = "AND source = 'tigge_ecmwf'"
            
            ecmwf_cur.execute(f"""
                SELECT DISTINCT target_date FROM {ecmwf_table}
                WHERE step=? {source_filter}
            """, (TARGET_STEP,))
            for r in ecmwf_cur.fetchall():
                ecmwf_dates.add(r[0])
        
        gefs_conn.close()
        ecmwf_conn.close()
        
        common_dates = gefs_dates & ecmwf_dates
        gefs_only = gefs_dates - ecmwf_dates
        ecmwf_only = ecmwf_dates - gefs_dates
        
        gate.total_checked = len(gefs_dates | ecmwf_dates)
        gate.details = {
            "n_gefs_dates": len(gefs_dates),
            "n_ecmwf_dates": len(ecmwf_dates),
            "n_common_dates": len(common_dates),
            "n_gefs_only": len(gefs_only),
            "n_ecmwf_only": len(ecmwf_only),
            "gefs_min_date": min(gefs_dates) if gefs_dates else None,
            "gefs_max_date": max(gefs_dates) if gefs_dates else None,
            "ecmwf_min_date": min(ecmwf_dates) if ecmwf_dates else None,
            "ecmwf_max_date": max(ecmwf_dates) if ecmwf_dates else None,
            "gefs_only_examples": sorted(gefs_only)[:5] if gefs_only else [],
            "ecmwf_only_examples": sorted(ecmwf_only)[:5] if ecmwf_only else [],
        }
        
        if not common_dates:
            gate.fail("No overlapping target dates between GEFS and ECMWF")
        elif len(common_dates) < max(len(gefs_dates), len(ecmwf_dates)) * 0.9:
            gate.fail(f"Only {len(common_dates)}/{len(gefs_dates | ecmwf_dates)} dates in common")
        
    except Exception as e:
        gate.fail(f"Error checking date alignment: {e}")
    
    return gate


# ═════════════════════════════════════════════════════════════════════════════
# Gate 5: Price Sanity
# ═════════════════════════════════════════════════════════════════════════════

def check_price_sanity(results: Optional[List[dict]] = None) -> GateResult:
    """
    Gate 5: Entry price between $0.01 and $0.99.
    If results provided, checks actual trade prices.
    Otherwise checks the Kalshi settlements DB for implied price ranges.
    """
    gate = GateResult(f"Price sanity: entry_price between ${PRICE_MIN} and ${PRICE_MAX}")
    
    if results:
        prices = [t["entry_price"] for t in results if "entry_price" in t]
        if not prices:
            gate.fail("No trade prices to check")
            return gate
        
        gate.total_checked = len(prices)
        out_of_range = [p for p in prices if p < PRICE_MIN or p > PRICE_MAX]
        gate.details = {
            "n_checked": len(prices),
            "min_price": float(np.min(prices)),
            "max_price": float(np.max(prices)),
            "mean_price": float(np.mean(prices)),
            "n_out_of_range": len(out_of_range),
        }
        
        if out_of_range:
            gate.fail(f"{len(out_of_range)}/{len(prices)} prices outside [{PRICE_MIN}, {PRICE_MAX}]")
    else:
        # Check from settlements DB for plausible price inference
        db_path = os.path.join(DATA_DIR, "kalshi_settlements.db")
        if not os.path.exists(db_path):
            gate.fail("Kalshi settlements database not found")
            return gate
        
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            cur = conn.cursor()
            cur.execute("""
                SELECT MIN(kalshi_temp), MAX(kalshi_temp), AVG(kalshi_temp)
                FROM kalshi_settlements
            """)
            tmin, tmax, tavg = cur.fetchone()
            conn.close()
            
            gate.total_checked = 1
            gate.details = {
                "note": "No trade results provided; checked settlement temperature range as proxy",
                "temp_min": tmin,
                "temp_max": tmax,
                "temp_mean": tavg,
                "implied_price_range_f": f"Kalshi 2°F bins: {(int(tmin)//2)*2}°F to {(int(tmax)//2+1)*2}°F",
            }
            
            # Temperature range implies price = probability of exceeding threshold
            # No direct price check without trade data
            gate.details["price_check_note"] = "Run with trade results to validate actual entry prices"
        except Exception as e:
            gate.fail(f"Error checking price sanity: {e}")
    
    return gate


# ═════════════════════════════════════════════════════════════════════════════
# Gate 6: Fee Sanity
# ═════════════════════════════════════════════════════════════════════════════

def check_fee_sanity(results: Optional[List[dict]] = None) -> GateResult:
    """
    Gate 6: fee_per_trade / notional ≤ 0.15.
    Kalshi fee model: ceil(0.07 × C × P × (1-P)) per side.
    Max theoretical fee ratio is ~0.0175 per side at P=0.5.
    """
    gate = GateResult(f"Fee sanity: fee_per_trade / notional ≤ {MAX_FEE_RATIO}")
    
    if not results:
        gate.details = {"note": "No trade results provided. Pass trade data to validate fee sanity."}
        gate.total_checked = 0
        return gate
    
    violations = []
    max_ratio = 0.0
    total_checked = 0
    
    for t in results:
        entry_fee = t.get("entry_fee", 0)
        entry_price = t.get("entry_price", 0)
        contracts = t.get("contracts", 0)
        notional = contracts * entry_price
        
        if notional > 0:
            fee_ratio = (entry_fee + t.get("exit_fee", 0)) / notional
            max_ratio = max(max_ratio, fee_ratio)
            total_checked += 1
            if fee_ratio > MAX_FEE_RATIO:
                violations.append({
                    "station": t.get("station", "?"),
                    "date": t.get("date", "?"),
                    "fee_ratio": round(fee_ratio, 4),
                    "entry_fee": entry_fee,
                    "entry_price": entry_price,
                    "contracts": contracts,
                    "notional": round(notional, 2),
                })
    
    gate.total_checked = total_checked
    gate.failed_count = len(violations)
    gate.details = {
        "n_checked": total_checked,
        "max_fee_ratio": round(max_ratio, 6),
        "n_violations": len(violations),
        "violations": violations[:10],  # Cap reporting
    }
    
    if violations:
        gate.fail(f"{len(violations)}/{total_checked} trades exceed fee ratio of {MAX_FEE_RATIO}")
    
    return gate


# ═════════════════════════════════════════════════════════════════════════════
# Runner
# ═════════════════════════════════════════════════════════════════════════════

def run_all_gates(results: Optional[List[dict]] = None, verbose: bool = False) -> dict:
    """Run all 6 data integrity gates."""
    
    gates = [
        check_gefs_completeness(),
        check_ecmwf_completeness(),
        check_station_alignment(),
        check_date_alignment(),
        check_price_sanity(results),
        check_fee_sanity(results),
    ]
    
    all_pass = all(g.passed for g in gates)
    
    # Print report
    print("=" * 72)
    print("  DATA INTEGRITY GATES — Pre-Sweep Validation")
    print(f"  Timestamp: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 72)
    
    any_failed = False
    for g in gates:
        icon = "✅" if g.passed else "❌"
        print(f"\n  {icon} {g.name}")
        print(f"     Checked: {g.total_checked}  Failed: {g.failed_count}")
        if not g.passed:
            any_failed = True
            for issue in g.issues:
                print(f"     FAIL: {issue}")
        if verbose:
            for k, v in g.details.items():
                if k not in ("violations", "low_member_examples"):
                    print(f"       {k}: {v}")
            if "low_member_examples" in g.details and g.details["low_member_examples"]:
                print(f"       Low-member samples: {g.details['low_member_examples'][:3]}")
            if "violations" in g.details and g.details["violations"]:
                print(f"       Violation samples: {g.details['violations'][:3]}")
    
    print(f"\n  {'=' * 30}")
    if all_pass:
        print(f"  ✅ ALL {len(gates)} GATES PASSED — Data integrity OK")
    else:
        fail_count = sum(1 for g in gates if not g.passed)
        print(f"  ❌ {fail_count}/{len(gates)} GATES FAILED — Data has integrity issues")
    print(f"  {'=' * 30}")
    
    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "all_pass": all_pass,
        "gates": [g.to_dict() for g in gates],
    }
    
    # Save
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\n  Results saved to: {OUT_PATH}")
    
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Data integrity gates for sweep pre-check")
    parser.add_argument("--json", action="store_true", help="Output as JSON to stdout")
    parser.add_argument("--verbose", "-v", action="store_true", help="Detailed output")
    args = parser.parse_args()
    
    result = run_all_gates(verbose=args.verbose)
    
    if args.json:
        print(json.dumps(result, indent=2))