#!/usr/bin/env python3
"""
Near-Miss Audit Query Tool

Usage:
    python query_near_miss_audit.py --help
    python query_near_miss_audit.py --station KDEN --hours 24
    python query_near_miss_audit.py --type STATION_COOLDOWN --limit 10
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

# Add the weather-engine-source directory to the path (from script location)
scripts_dir = Path(__file__).parent.resolve()
workspace_dir = scripts_dir.parent.resolve()

# Add workspace root to path
sys.path.insert(0, str(workspace_dir))

# Now import from core
from core.near_miss_audit import (
    query_near_miss_log,
    get_near_miss_summary,
    NEAR_MISS_TYPES,
)


def format_record(record: dict) -> str:
    """Format a near-miss record for display."""
    details = record.get("details", {})
    metadata = record.get("metadata", {})
    
    lines = [
        f"[{record['created_utc']}] {record['station']}",
        f"  Type: {record['near_miss_type']}",
        f"  Severity: {record['severity'] or 'N/A'}",
    ]
    
    if record.get("suppressed_alert_type"):
        lines.append(f"  Suppressed Alert: {record['suppressed_alert_type']}")
    
    if details:
        lines.append("  Details:")
        for key, value in sorted(details.items()):
            if isinstance(value, (dict, list)):
                value = json.dumps(value)[:100] + "..." if len(json.dumps(value)) > 100 else json.dumps(value)
            lines.append(f"    {key}: {value}")
    
    if metadata:
        lines.append("  Metadata:")
        for key, value in sorted(metadata.items()):
            if isinstance(value, (dict, list)):
                value = json.dumps(value)[:100] + "..." if len(json.dumps(value)) > 100 else json.dumps(value)
            lines.append(f"    {key}: {value}")
    
    return "\n".join(lines)


def print_summary(summary: dict) -> None:
    """Print a near-miss summary in a readable format."""
    print(f"\n{'=' * 60}")
    print("NEAR-MISS AUDIT SUMMARY")
    print(f"{'=' * 60}")
    print(f"Window: Last {summary['window_hours']} hours")
    print(f"Generated: {summary['generated_at']}")
    print(f"Total Near Misses: {summary['total_count']}")
    
    if not summary['total_count']:
        print("No near-miss events found in this window.")
        return
    
    print("\n--- Severity Breakdown ---")
    for sev, count in summary.get('severity_breakdown', {}).items():
        if count > 0:
            print(f"  {sev}: {count}")
    
    print("\n--- Top Near-Miss Types ---")
    for mt, count in sorted(
        summary.get('by_type', {}).items(),
        key=lambda x: -x[1]
    )[:10]:
        pct = (count / summary['total_count'] * 100) if summary['total_count'] > 0 else 0
        print(f"  {mt}: {count} ({pct:.1f}%)")
    
    if summary.get('suppressed_alert_types'):
        print("\n--- Suppressed Alert Types ---")
        for at, count in sorted(
            summary['suppressed_alert_types'].items(),
            key=lambda x: -x[1]
        )[:10]:
            pct = (count / summary['total_count'] * 100) if summary['total_count'] > 0 else 0
            print(f"  {at}: {count} ({pct:.1f}%)")
    
    print("\n--- Recent Records (Last 10) ---")
    for i, record in enumerate(summary.get('recent_records', []), 1):
        print(f"\n  [{i}] {record['created_utc']} - {record['station']}")
        print(f"      Type: {record['near_miss_type']}, Severity: {record['severity']}")
        if record.get('suppressed_alert_type'):
            print(f"      Suppressed Alert: {record['suppressed_alert_type']}")
        details = record.get('details', {})
        if details:
            sample_details = list(details.items())[:3]
            for key, value in sample_details:
                print(f"      {key}: {value}")
    
    print(f"\n{'=' * 60}")


def main():
    parser = argparse.ArgumentParser(
        description="Query near-miss audit log for weather engine signal evaluation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Get summary for all stations in last 24 hours
  python query_near_miss_audit.py --hours 24

  # Get near misses for a specific station
  python query_near_miss_audit.py --station KDEN --hours 24

  # Filter by near-miss type
  python query_near_miss_audit.py --type STATION_COOLDOWN --limit 20

  # Query all near misses
  python query_near_miss_audit.py --all

  # Show summary statistics
  python query_near_miss_audit.py --summary
        """,
    )
    
    parser.add_argument("--station", "-s", help="Station code (ICAO)")
    parser.add_argument("--type", "-t", help="Near-miss type (see NEAR_MISS_TYPES)")
    parser.add_argument("--hours", "-H", type=int, default=24, help="Lookback hours (default: 24)")
    parser.add_argument("--limit", "-l", type=int, default=50, help="Max records to return (default: 50)")
    parser.add_argument("--summary", action="store_true", help="Show summary statistics")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--all", action="store_true", help="Return all records (ignores limit)")
    
    args = parser.parse_args()
    
    try:
        # Query records
        records = query_near_miss_log(
            station=args.station,
            near_miss_type=args.type,
            hours=args.hours,
            limit=5000 if args.all else args.limit,
        )
        
        if args.json:
            # Output as JSON
            output = {
                "query": {
                    "station": args.station,
                    "near_miss_type": args.type,
                    "hours": args.hours,
                    "limit": args.limit,
                },
                "total_count": len(records),
                "records": records,
            }
            if args.summary:
                output["summary"] = get_near_miss_summary(
                    station=args.station,
                    hours=args.hours,
                    by_type=True,
                )
            print(json.dumps(output, indent=2, default=str))
        else:
            # Output formatted
            print(f"\n{'=' * 60}")
            print("NEAR-MISS AUDIT QUERY")
            print(f"{'=' * 60}")
            print(f"Station: {args.station or 'ALL'}")
            print(f"Near-Miss Type: {args.type or 'ALL'}")
            print(f"Hours: {args.hours}")
            print(f"Limit: {args.limit if not args.all else 'ALL'}")
            print(f"Found: {len(records)} records")
            
            if records:
                print(f"\n--- Recent Records ---")
                for record in records[:args.limit]:
                    print(f"\n{format_record(record)}")
            
            if args.summary or len(records) > 0:
                summary = get_near_miss_summary(
                    station=args.station,
                    hours=args.hours,
                    by_type=True,
                )
                if not args.json:
                    print_summary(summary)
        
        return 0
    
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
