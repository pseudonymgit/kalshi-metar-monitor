#!/usr/bin/env python3
"""
Settlement Time Calendar Generator v1.0 — Phase 6.7

Generates a CSV of known settlement epochs for all weather station locations (20 stations).
Kalshi bucket contracts settle at 12:00 UTC daily for HIGH/LOW temperature markets.
Format: station, date, market_type, settlement_epoch, timezone

This script generates a calendar for the next 30 days for common US weather stations.
"""

import csv
import json
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Tuple
import os


def get_common_weather_stations() -> List[Dict[str, str]]:
    """
    Returns a list of common US weather stations with location details.
    This could be extended with a more comprehensive list as needed by the weather engine.
    
    Each entry contains station (ICAO code), name, and timezone info.
    """
    stations = [
        # Major US airports - common targets for weather prediction markets
        {"station": "KATL", "name": "Atlanta Hartsfield-Jackson", "timezone": "America/New_York"},
        {"station": "KLAX", "name": "Los Angeles International", "timezone": "America/Los_Angeles"},
        {"station": "KORD", "name": "Chicago O'Hare", "timezone": "America/Chicago"},
        {"station": "KJFK", "name": "New York JFK", "timezone": "America/New_York"},
        {"station": "KSFO", "name": "San Francisco International", "timezone": "America/Los_Angeles"},
        {"station": "KDEN", "name": "Denver International", "timezone": "America/Denver"},
        {"station": "KDFW", "name": "Dallas/Fort Worth", "timezone": "America/Chicago"},
        {"station": "KSEA", "name": "Seattle-Tacoma", "timezone": "America/Los_Angeles"},
        {"station": "KMIA", "name": "Miami International", "timezone": "America/New_York"},
        {"station": "KBOS", "name": "Boston Logan", "timezone": "America/New_York"},
        {"station": "KLGA", "name": "New York LaGuardia", "timezone": "America/New_York"},
        {"station": "KEWR", "name": "Newark Liberty", "timezone": "America/New_York"},
        {"station": "KPHX", "name": "Phoenix Sky Harbor", "timezone": "America/Phoenix"},
        {"station": "KIAD", "name": "Washington Dulles", "timezone": "America/New_York"},
        {"station": "KPWM", "name": "Portland Jetport", "timezone": "America/New_York"},
        {"station": "KBWI", "name": "Baltimore Washington", "timezone": "America/New_York"},
        {"station": "KTPA", "name": "Tampa International", "timezone": "America/New_York"},
        {"station": "KDCA", "name": "DC Reagan National", "timezone": "America/New_York"},
        {"station": "KMSP", "name": "Minneapolis-Saint Paul", "timezone": "America/Chicago"},
        {"station": "KSLC", "name": "Salt Lake City", "timezone": "America/Denver"}
    ]
    
    return stations


def generate_station_events(
    station: str, 
    timezone_name: str, 
    num_days: int = 30
) -> List[Dict[str, str]]:
    """
    Generate settlement events for a single station over specified number of days.
    
    For weather markets, settlements typically occur:
    - For High temperature: by 12:00 UTC on day D
    - For Low temperature: by 12:00 UTC on day D
    
    Args:
        station: Station ICAO code
        timezone_name: Named timezone from tz database
        num_days: Number of days to generate (forward from today)
    
    Returns:
        List of settlement records for HIGH and LOW markets
    """
    
    # We'll approximate the timezones here and generate UTC times directly since 
    # we know Kalshi settles at 12:00 UTC for temperature markets
    # The timezone in our output refers to the locale the station is in
    # but the settlement epoch is always at 12:00 UTC as stated
    
    events = []
    start_date = datetime.now().date()
    
    # For each day in next N days
    for i in range(num_days):
        current_date = start_date + timedelta(days=i)
        
        # Settlement is at 12:00 UTC daily
        settlement_dt_utc = datetime.combine(current_date, datetime.min.time()) \
                           .replace(tzinfo=timezone.utc) \
                           .replace(hour=12, minute=0, second=0, microsecond=0)
        
        # Create records for both HIGH and LOW markets for this station+date
        for market_type in ["HIGH", "LOW"]:
            event = {
                "station": station,
                "date": current_date.strftime("%Y-%m-%d"),
                "market_type": market_type,
                "settlement_epoch": int(settlement_dt_utc.timestamp()),
                "timestamp_iso": settlement_dt_utc.isoformat(),
                "timezone": timezone_name
            }
            events.append(event)
    
    return events


def generate_settlement_calendar(output_file: str = "data/settlement_calendar.csv", num_days: int = 30):
    """
    Main function to generate the entire settlement calendar for all stations.
    
    Args:
        output_file: Path where to save the CSV file
        num_days: Number of days ahead to generate settlements for
    """
    # Ensure output directory exists
    os.makedirs(os.path.dirname(output_file) if os.path.dirname(output_file) else '.', exist_ok=True)
    
    all_events = []
    
    # Get all stations
    stations = get_common_weather_stations()
    
    print(f"Generating settlement calendar for {len(stations)} stations over {num_days} days...")
    
    # Generate events for each station
    for station_info in stations:
        station_code = station_info["station"]
        timezone_name = station_info["timezone"]
        
        events = generate_station_events(station_code, timezone_name, num_days)
        all_events.extend(events)
        
        print(f"  Generated {len(events)} events for {station_code} ({station_info['name']})")
    
    # Sort events by date, then by station, then by market type
    all_events.sort(key=lambda x: (x['date'], x['station'], x['market_type']))
    
    # Write to CSV file
    fieldnames = ["station", "date", "market_type", "settlement_epoch", "timestamp_iso", "timezone"]
    
    with open(output_file, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        
        for event in all_events:
            writer.writerow(event)
    
    print(f"Settlement calendar generated: {output_file}")
    print(f"Total events: {len(all_events)}")
    print(f"Date range: {all_events[0]['date']} to {all_events[-1]['date']}")
    
    return {
        "output_file": output_file,
        "total_events": len(all_events),
        "date_range": (all_events[0]["date"], all_events[-1]["date"]),
        "stations_processed": len(stations)
    }


def read_settlement_calendar(input_file: str = "data/settlement_calendar.csv"):
    """
    Utility function to read the generated settlement calendar.
    
    Args:
        input_file: Path to the CSV file to read
    
    Returns:
        List of settlement calendar records
    """
    if not os.path.exists(input_file):
        raise FileNotFoundError(f"Settlement calendar file not found: {input_file}")
    
    with open(input_file, "r", newline="") as csvfile:
        reader = csv.DictReader(csvfile)
        return [row for row in reader]


def get_upcoming_settlements(
    input_file: str = "data/settlement_calendar.csv",
    days_ahead: int = 7
) -> List[Dict[str, str]]:
    """
    Get settlements occurring in the next N days.
    
    Args:
        input_file: Path to the CSV settlement calendar
        days_ahead: Number of days to look ahead (default 7)
    
    Returns:
        List of upcoming settlements
    """
    events = read_settlement_calendar(input_file)
    
    # Convert to datetime for comparison
    now_timestamp = datetime.now(timezone.utc).timestamp()
    cutoff_timestamp = datetime.now(timezone.utc).timestamp() + (days_ahead * 24 * 3600)
    
    upcoming = []
    for event in events:
        event_timestamp = int(event["settlement_epoch"])
        if now_timestamp <= event_timestamp <= cutoff_timestamp:
            upcoming.append(event)
    
    # Sort upcoming events by timestamp
    upcoming.sort(key=lambda x: int(x["settlement_epoch"]))
    
    print(f"Found {len(upcoming)} upcoming settlements in next {days_ahead} days")
    return upcoming


def validate_settlement_calendar(input_file: str = "data/settlement_calendar.csv") -> List[str]:
    """
    Quick validation of the settlement calendar format and consistency.
    
    Returns:
        List of validation issues found
    """
    issues = []
    
    try:
        calendar = read_settlement_calendar(input_file)
        if not calendar:
            issues.append("Calendar contains no records")
            return issues
        
        # Check for required fields in first record
        first_record = calendar[0]
        required_fields = ["station", "date", "market_type", "settlement_epoch", "timezone"]
        for field in required_fields:
            if field not in first_record:
                issues.append(f"Missing required field: {field}")
        
        # Check first and last dates make sense
        sorted_records = sorted(calendar, key=lambda x: int(x["settlement_epoch"]))
        first_date = sorted_records[0]["date"]
        last_date = sorted_records[-1]["date"]
        
        # Verify settlement times are consistently at noon (12 UTC)
        for record in sorted_records[:5]:  # Check first few for pattern
            event_datetime = datetime.fromtimestamp(int(record["settlement_epoch"]), tz=timezone.utc)
            if event_datetime.hour != 12 or event_datetime.minute != 0:
                issues.append(f"Non-standard settlement time found: {record}")
                break
                
        # Check for duplicate entries
        seen_keys = set()
        duplicates = []
        for record in calendar:
            key = f"{record['station']}_{record['date']}_{record['market_type']}"
            if key in seen_keys:
                duplicates.append(key)
            else:
                seen_keys.add(key)
        
        if duplicates:
            issues.append(f"Found {len(duplicates)} duplicate settlement entries")
            
        # Check numeric values
        for idx, record in enumerate(sorted_records[:10]):  # Check first 10
            try:
                se = int(record["settlement_epoch"])
                if se <= 0:
                    issues.append(f"Invalid settlement epoch in record {idx}: {se}")
            except ValueError:
                issues.append(f"Non-numeric settlement epoch in record {idx}: {record['settlement_epoch']}")
    
    except Exception as e:
        issues.append(f"Error validating calendar: {str(e)}")
    
    return issues


if __name__ == "__main__":
    # Generate a 30-day settlement calendar for all stations
    output_info = generate_settlement_calendar("data/settlement_calendar.csv", num_days=30)
    
    print("\nValidation Checks:")
    issues = validate_settlement_calendar("data/settlement_calendar.csv")
    if issues:
        print("Issues found:")
        for issue in issues:
            print(f"  - {issue}")
    else:
        print("✓ Calendar validation passed")
    
    print(f"\nSample of upcoming settlements (next 5 days):")
    upcoming = get_upcoming_settlements("data/settlement_calendar.csv", days_ahead=5)
    for event in upcoming[:10]:  # Show first 10
        from_dt = datetime.fromtimestamp(int(event["settlement_epoch"]), tz=timezone.utc)
        print(f"  {event['station']} {event['market_type']} {event['date']} @ {from_dt.strftime('%H:%M UTC')}")
    
    # Verify that the output file contains reasonable entries
    with open("data/settlement_calendar.csv", "r") as f:
        first_3_lines = [f.readline().strip() for _ in range(4)]  # header + first 3 data lines
        
    print(f"\nSample output (first 3 data rows):")
    for line in first_3_lines:
        print(f"  {line}")
    
    print(f"\nSettlement calendar generator completed successfully!")