#!/usr/bin/env python3
"""
Test alert_builder.py - Paper Trading Alert Format v2.1 (B-MODE v2)
"""

import sys
sys.path.insert(0, '/home/node/.openclaw/workspace/prototypes/weather-engine-source/core')

from alert_builder import (
    build_paper_trade_alert,
    build_paper_trade_alert_dev,
    format_alert_for_discord,
    OpportunityGrade,
    LaneType,
    compute_opportunity_grade,
    classify_lane,
    GRADE_THRESHOLDS,
)

def test_compute_opportunity_grade():
    """Test Opportunity Grade computation."""
    print("Testing Opportunity Grade computation:")
    
    test_cases = [
        (0.85, 0.70, 2.5, "Edge=0.15, Sharpe=2.5 -> Should be S"),
        (0.75, 0.60, 2.0, "Edge=0.15, Sharpe=2.0 -> Should be A (edge=15%, Sharpe=2.0)"),
        (0.65, 0.55, 1.8, "Edge=0.10, Sharpe=1.8 -> Should be A"),
        (0.60, 0.50, 1.5, "Edge=0.10, Sharpe=1.5 -> Should be B"),
        (0.55, 0.50, 1.2, "Edge=0.05, Sharpe=1.2 -> Should be C"),
        (0.52, 0.50, 1.0, "Edge=0.02, Sharpe=1.0 -> Should be D"),
        (0.50, 0.48, 0.8, "Edge=0.02, Sharpe=0.8 -> Should be F (Sharpe too low)"),
    ]
    
    for conf, prob, sharpe, desc in test_cases:
        grade, edge = compute_opportunity_grade(conf, prob, sharpe)
        print(f"  {desc}")
        print(f"    Result: Grade={grade.value}, Edge={edge:.2%}")
        print()

def test_classify_lane():
    """Test lane classification."""
    print("Testing lane classification:")
    
    test_cases = [
        (0.85, "calendar_climatology", "High conf + regular signal -> Sure_Thing"),
        (0.85, "goldilocks_reversion", "High conf + Goldilocks -> Goldilocks"),
        (0.65, "late_day_momentum_hourly", "Med conf + regular -> Regular"),
        (0.45, "goldilocks_reversion", "Low conf + Goldilocks -> Goldilocks"),
    ]
    
    for conf, signal_type, desc in test_cases:
        lane = classify_lane(conf, signal_type)
        print(f"  {desc}")
        print(f"    Result: Lane={lane.value} ({lane.name})")
        print()

def test_build_alert():
    """Test alert building."""
    print("Testing alert building:")
    
    # Sample trade result - High quality alert (edge >= 10%, conf >= 65%, Grade B+)
    trade_result = {
        "confidence": 0.78,
        "market_price": 0.62,
        "analytical_prob": 0.78,
        "position_size_usd": 125.50,
        "sharpe": 2.3,
        "functionality": "late_day_momentum_hourly",
        "trade_uuid": "test-uuid-123",
        "trade_version": "v2.1",
        "hit_rate": 0.72,  # 72% directional accuracy
        "hit_rate_n": 245,  # 245 historical samples
    }
    
    station = "KDEN"
    market_type = "HIGH"
    direction = "UP"
    
    alert_data = build_paper_trade_alert(trade_result, station, market_type, direction, 
                                        instance=None, hit_rate=0.72, hit_rate_n=245,
                                        current_bucket=82, trading_bucket=84)
    
    print(f"  Trade Result: {trade_result}")
    print(f"  Station: {station}, Market: {market_type}, Direction: {direction}")
    print(f"  Alert Data:")
    print(f"    Grade: {alert_data['grade']} ({alert_data['grade_label']})")
    print(f"    Lane: {alert_data['lane']} ({alert_data['lane_label']})")
    print(f"    Edge: {alert_data['edge_pct']}")
    print(f"    Sharpe: {alert_data['Sharpe']}")
    print(f"    Trade Conf: {alert_data['trade_confidence']:.0%}")
    print(f"    Market prob: {alert_data['market_prob']:.2%}")
    print(f"    Market URL: {alert_data['market_url']}")
    
    # Check filtering
    if alert_data.get('skip_reason'):
        print(f"    [FILTERED] {alert_data['skip_reason']}")
    else:
        print(f"    [PASS] Alert passed all filters")
    print()
    
    # Format for Discord
    discord_payload = format_alert_for_discord(alert_data)
    print("  Discord Payload:")
    print("  " + "-" * 50)
    if discord_payload.get('content'):
        print(f"  Content: {discord_payload['content']}")
    if discord_payload.get('embeds'):
        for i, embed in enumerate(discord_payload['embeds']):
            print(f"  [Embed {i+1}]")
            print(f"    Title: {embed.get('title', 'N/A')}")
            print(f"    Color: #{embed.get('color', 0):06X}")
            print(f"    Description: {embed.get('description', 'N/A')}")
            if embed.get('fields'):
                for field in embed['fields']:
                    print(f"    {field.get('name', 'N/A')}: {field.get('value', 'N/A')}")
            if embed.get('footer'):
                print(f"    Footer: {embed['footer'].get('text', 'N/A')}")
    print("  " + "-" * 50)
    print()

def test_filtered_alert():
    """Test alert that should be filtered (edge too low)."""
    print("Testing alert filtering (edge < 10%):")
    
    # Low edge alert (should be filtered)
    trade_result = {
        "confidence": 0.68,
        "market_price": 0.65,
        "position_size_usd": 75.25,
        "sharpe": 1.5,
        "functionality": "calendar_climatology",
        "trade_uuid": "test-uuid-filter",
        "trade_version": "v2.1",
    }
    
    alert_data = build_paper_trade_alert(trade_result, "KLAX", "LOW", "DOWN", 
                                        instance=None, hit_rate=None, hit_rate_n=None,
                                        current_bucket=80, trading_bucket=81)
    
    print(f"  Trade Result: {trade_result}")
    
    if alert_data.get('skip_reason'):
        print(f"  [FILTERED] {alert_data['skip_reason']}")
        print(f"  [PASS] Alert correctly filtered")
    else:
        print(f"  Alert Data:")
        print(f"    Grade: {alert_data['grade']} ({alert_data['grade_label']})")
        print(f"    Edge: {alert_data['edge_pct']}")
        print(f"    Trade Conf: {alert_data['trade_confidence']:.0%}")
        print(f"    [UNEXPECTED] Alert was not filtered")
        print(f"  [FAIL] Alert should have been filtered")
    print()

def test_dev_alert():
    """Test DEV variant alert building."""
    print("Testing DEV variant alert building:")
    
    trade_result = {
        "confidence": 0.70,
        "market_price": 0.55,
        "position_size_usd": 75.25,
        "sharpe": 1.8,
        "functionality": "calendar_climatology",
        "trade_uuid": "test-uuid-456",
        "trade_version": "v2.1",
    }
    
    station = "KLAX"
    market_type = "LOW"
    direction = "DOWN"
    
    alert_data = build_paper_trade_alert_dev(trade_result, station, market_type, direction, 
                                            instance=None, hit_rate=0.68, hit_rate_n=183,
                                            current_bucket=80, trading_bucket=82)
    
    print(f"  Trade Result: {trade_result}")
    
    if alert_data.get('skip_reason'):
        print(f"    [FILTERED] {alert_data['skip_reason']}")
    else:
        print(f"  Alert Data:")
        print(f"    Grade: {alert_data['grade']} ({alert_data['grade_label']})")
        print(f"    Lane: {alert_data['lane']} ({alert_data['lane_label']})")
        print(f"    Edge: {alert_data['edge_pct']}")
        print(f"    Sharpe: {alert_data['Sharpe']}")
        print(f"    [PASS] Alert passed all filters")
    print()
    
    # Format for Discord
    discord_payload = format_alert_for_discord(alert_data)
    print("  Discord Payload:")
    print("  " + "-" * 50)
    if discord_payload.get('content'):
        print(f"  Content: {discord_payload['content']}")
    if discord_payload.get('embeds'):
        for i, embed in enumerate(discord_payload['embeds']):
            print(f"  [Embed {i+1}]")
            print(f"    Title: {embed.get('title', 'N/A')}")
            print(f"    Color: #{embed.get('color', 0):06X}")
            print(f"    Description: {embed.get('description', 'N/A')}")
            if embed.get('fields'):
                for field in embed['fields']:
                    print(f"    {field.get('name', 'N/A')}: {field.get('value', 'N/A')}")
            if embed.get('footer'):
                print(f"    Footer: {embed['footer'].get('text', 'N/A')}")
    print("  " + "-" * 50)
    print()

def test_hard_filters():
    """Test all hard filter conditions."""
    print("Testing hard filters:")
    
    filter_tests = [
        {"name": "Edge < 10% (positive but too low)", "conf": 0.68, "prob": 0.62, "sharpe": 1.5},
        {"name": "Edge < 0 (negative)", "conf": 0.60, "prob": 0.65, "sharpe": 1.5},
        {"name": "Conf < 65% (edge=10%)", "conf": 0.64, "prob": 0.54, "sharpe": 1.5},  # conf=0.64 < 0.65, edge=10%
        {"name": "Grade D (should be filtered)", "conf": 0.55, "prob": 0.50, "sharpe": 1.0},
        {"name": "Grade F (should be filtered)", "conf": 0.52, "prob": 0.50, "sharpe": 0.8},
    ]
    
    for test in filter_tests:
        trade_result = {
            "confidence": test["conf"],
            "market_price": test["prob"],
            "position_size_usd": 50.0,
            "sharpe": test["sharpe"],
            "functionality": "late_day_momentum_hourly",
            "trade_uuid": f"filter-test-{test['name']}",
            "trade_version": "v2.1",
        }
        
        alert_data = build_paper_trade_alert(trade_result, "KDEN", "HIGH", "UP", 
                                            instance=None, current_bucket=80, trading_bucket=81)
        
        if alert_data.get('skip_reason'):
            print(f"  ✓ {test['name']}: Filtered - {alert_data['skip_reason']}")
        else:
            print(f"  ✗ {test['name']}: NOT FILTERED (Grade={alert_data['grade']}, Edge={alert_data['edge_pct']})")
    print()

if __name__ == "__main__":
    print("=" * 60)
    print("Alert Builder v2.1 (B-MODE v2) Tests")
    print("=" * 60)
    print()
    
    test_compute_opportunity_grade()
    test_classify_lane()
    
    # Test filtered alert first
    test_filtered_alert()
    test_hard_filters()
    
    # Test passing alert
    test_build_alert()
    test_dev_alert()
    
    print("All tests completed!")
