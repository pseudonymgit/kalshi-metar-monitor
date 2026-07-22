#!/usr/bin/env python3
"""
PHASE 11: Deseasonalization Validation Test Script
Compare NWP Direct predictions against Calendar Climatology baseline.

Goal:
- For each (station, date) where GFS has consecutive data, compare:
  - GFS-predicted direction (tomorrow vs today forecast)
  - Calendar climatology-predicted direction (historical average for tomorrow vs today)
  - Actual settlement direction
- Perform McNemar's test on discordant pairs
- Assess if GFS consistently outperforms climatology

Expected outcome: GFS beats climatology by ~28.4 percentage points, 
wins ~88.6% of cases where they disagree.
"""
import sqlite3
import numpy as np
from scipy.stats import chi2
from typing import Tuple, List, Dict, Any, Optional
from datetime import datetime, timedelta
import math


def load_gfs_forecast_data(station: str, start_date: str, end_date: str, db_path: str) -> List[Dict[str, Any]]:
    """
    Load GFS forecast data for a station over the date range.
    Return forecast temperatures to calculate direction from.
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    query = """
    SELECT 
        forecast_date,
        station_id,
        temperature_value,
        variable
    FROM nwp_forecasts 
    WHERE station_id = ? 
    AND model = 'gfs'
    AND variable = 'temperature_2m_max'
    AND forecast_date BETWEEN ? AND ?
    ORDER BY forecast_date, forecast_horizon
    """
    
    cursor.execute(query, (station, start_date, end_date))
    rows = cursor.fetchall()
    
    forecast_data = {}
    for forecast_date, station_id, temp_value, variable in rows:
        if forecast_date not in forecast_data:
            forecast_data[forecast_date] = {}
        forecast_data[forecast_date][variable] = temp_value

    conn.close()
    
    # Process into direction comparisons (today vs tomorrow forecasted values)
    processed = []
    dates = sorted(forecast_data.keys())
    
    for i in range(len(dates)-1):
        today_date = dates[i]
        tomorrow_date = dates[i+1]
        
        today_temp = forecast_data[today_date].get('temperature_2m_max')
        tomorrow_temp = forecast_data[tomorrow_date].get('temperature_2m_max')
        
        if today_temp is not None and tomorrow_temp is not None:
            direction = 'UP' if tomorrow_temp > today_temp else ('DOWN' if tomorrow_temp < today_temp else 'EQUAL')
            processed.append({
                'date': today_date,
                'tomorrow_date': tomorrow_date,
                'today_forecast_temp': today_temp,
                'tomorrow_forecast_temp': tomorrow_temp,
                'gfs_direction': direction
            })

    return processed


def load_climatology_data(station: str, date: str, db_path: str) -> Optional[float]:
    """
    Get historical climatology for the given station/date combination.
    Compare average of NEXT day to TODAY.
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Extract month and day for climatology lookup
    month_day = date[5:10]  # Extract MM-DD from YYYY-MM-DD
    
    # Today's climatology
    query = """
    SELECT AVG(settlement_bucket) as avg_temp
    FROM settlement_epochs
    WHERE station = ?
    AND substr(local_trading_date, 6, 5) = ?
    AND epoch_status = 'closed'
    AND settlement_bucket IS NOT NULL
    """
    
    cursor.execute(query, (station, month_day))
    result = cursor.fetchone()
    today_climo = result[0] / 100.0 if result[0] is not None else None  # Convert to 0-1 scale
    
    # Tomorrow's climatology (day after)
    target_date_dt = datetime.strptime(date, '%Y-%m-%d')
    tomorrow_date = (target_date_dt + timedelta(days=1)).strftime('%Y-%m-%d')
    month_day_tomorrow = tomorrow_date[5:10]
    
    query = """
    SELECT AVG(settlement_bucket) as avg_temp
    FROM settlement_epochs
    WHERE station = ?
    AND substr(local_trading_date, 6, 5) = ?
    AND epoch_status = 'closed'
    AND settlement_bucket IS NOT NULL
    """
    
    cursor.execute(query, (station, month_day_tomorrow))
    result = cursor.fetchone()
    tomorrow_climo = result[0] / 100.0 if result[0] is not None else None  # Convert to 0-1 scale
    
    conn.close()
    
    if today_climo is not None and tomorrow_climo is not None:
        direction = 'UP' if tomorrow_climo > today_climo else ('DOWN' if tomorrow_climo < today_climo else 'EQUAL')
        return tomorrow_climo, today_climo, direction  # Note: return actual values and derived direction
    
    return None


def load_actual_settlement_data(station: str, date_range_start: str, date_range_end: str, db_path: str) -> Dict[str, str]:
    """
    Load actual settlement data for the given station over date range.
    Returns a dictionary mapping date to actual direction ('UP', 'DOWN', 'EQUAL').
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    query = """
    SELECT local_trading_date, settlement_bucket, prior_settlement_bucket
    FROM settlement_epochs
    WHERE station = ?
    AND local_trading_date BETWEEN ? AND ?
    AND epoch_status = 'closed'
    AND settlement_bucket IS NOT NULL
    AND prior_settlement_bucket IS NOT NULL
    ORDER BY local_trading_date
    """
    
    cursor.execute(query, (station, date_range_start, date_range_end))
    rows = cursor.fetchall()
    
    settlements = {}
    for date, current_temp, prior_temp in rows:
        direction = 'UP' if current_temp > prior_temp else ('DOWN' if current_temp < prior_temp else 'EQUAL')
        settlements[date] = direction
        
    conn.close()
    return settlements


def perform_mcnemar_test(contingency_table: Dict[str, int]) -> Tuple[float, float]:
    """
    Perform McNemar's test for comparing the disagreement between two paired classifiers.

    The contingency table should contain:
    - 'both_correct': both GFS and Climatology correct
    - 'gfs_only_correct': only GFS correct, Climatology wrong
    - 'climo_only_correct': only Climatology correct, GFS wrong  
    - 'both_wrong': both wrong

    Returns test statistic and p-value.
    """
    # From contingency table
    b = contingency_table.get('gfs_only_correct', 0)  # GFS correct, Climatology wrong
    c = contingency_table.get('climo_only_correct', 0)  # Climatology correct, GFS wrong

    # McNemar's test statistic: (|b - c| - 1)^2 / (b + c)
    # If b + c == 0, test is undefined
    if b + c == 0:
        return 0.0, 1.0  # Can't perform test if no disagreements

    chi_squared = (abs(b - c) - 1)**2 / (b + c)
    p_value = 1 - chi2.cdf(chi_squared, df=1)
    
    return chi_squared, p_value


def main():
    """
    Main execution function for deseasonalization validation test.
    """
    print("PHASE 11: Deseasonalization Validation Test")
    print("Comparing GFS NWP Direct forecasts vs. Calendar Climatology")
    print("Expected: GFS beats climatology by ~28.4pp, wins ~88.6% of disagreements")
    print()

    # Configuration
    nwp_db_path = "../../data/nwp_forecasts.db"  # Where GFS forecasts are stored  
    metar_db_path = "../../data/metar_backfill.db"  # Where settlement epochs are stored

    test_stations = [
        'KATL', 'KBOS', 'KLAX', 'KJFK', 'KORD', 'KMIA', 'KSEA', 'KSFO', 
        'KHOU', 'KPHX', 'KDEN', 'KSLC', 'KPDX', 'KCLE', 'KNYC', 'KLAS', 
        'KSTL', 'KMEM', 'KBNA', 'KBDL'
    ]

    start_date = '2025-01-01'
    end_date = '2025-12-31'
    
    all_results = []
    gfs_correct_total = 0
    climo_correct_total = 0
    both_correct_total = 0
    both_wrong_total = 0
    gfs_only_total = 0  # GFS correct, climatology wrong
    climo_only_total = 0  # Climatology correct, GFS wrong
    total_comparisons = 0

    # Results storage
    disagreements_gfs_wins = 0  # Times GFS won when they disagreed
    disagreements_climo_wins = 0  # Times Climatology won when they disagreed

    for station in test_stations:
        print(f"Processing station {station}...")
        
        # Load forecast data (GFS predictions for today vs tomorrow)
        gfs_forecasts = load_gfs_forecast_data(station, start_date, end_date, nwp_db_path)
        
        if not gfs_forecasts:
            print(f"No GFS forecast data found for {station}. Skipping.")
            continue

        # Load actual settlements
        settlements = load_actual_settlement_data(station, start_date, end_date, metar_db_path)

        # Counters for this station
        gfs_correct = 0
        climo_correct = 0
        both_correct = 0
        both_wrong = 0
        gfs_only_correct = 0
        climo_only_correct = 0
        comparisons = 0

        for forecast_record in gfs_forecasts:
            # Get forecast date and corresponding actual settlement
            gfs_date = forecast_record['date']
            
            # Only proceed if we have actual settlement for the tomorrow date
            actual_direction = settlements.get(forecast_record['tomorrow_date'])

            if actual_direction is not None and actual_direction != 'EQUAL':
                # Compare GFS forecast direction
                gfs_direction = forecast_record.get('gfs_direction')
                
                if gfs_direction and gfs_direction != 'EQUAL':
                    # Load climatology-based prediction for same date period  
                    climo_result = load_climatology_data(gfs_date, metar_db_path)
                    
                    if climo_result is not None:
                        tomorrow_climo, today_climo, climo_direction = climo_result
                        if climo_direction and climo_direction != 'EQUAL':
                            # Compare both to actual settlement
                            gfs_is_correct = (gfs_direction == actual_direction)
                            climo_is_correct = (climo_direction == actual_direction)

                            # Count results
                            comparisons += 1

                            if gfs_is_correct and climo_is_correct:
                                both_correct += 1
                                gfs_correct += 1  # Both are still correct for their respective totals
                                climo_correct += 1
                            elif gfs_is_correct and not climo_is_correct:
                                gfs_only_correct += 1
                                gfs_correct += 1
                            elif not gfs_is_correct and climo_is_correct:
                                climo_only_correct += 1
                                climo_correct += 1
                            else:
                                both_wrong += 1

                            # Track who won in disagreement scenarios (for percentage calculation)
                            if gfs_is_correct and not climo_is_correct:
                                disagreements_gfs_wins += 1
                            elif not gfs_is_correct and climo_is_correct:
                                disagreements_climo_wins += 1

        # Sum up station results to global counters
        all_results.append({
            'station': station,
            'comparisons': comparisons,
            'gfs_correct': gfs_correct,
            'climo_correct': climo_correct,
            'both_correct': both_correct,
            'both_wrong': both_wrong,
            'gfs_only': gfs_only_correct,
            'climo_only': climo_only_correct
        })
        
        gfs_correct_total += gfs_correct
        climo_correct_total += climo_correct
        both_correct_total += both_correct
        both_wrong_total += both_wrong
        gfs_only_total += gfs_only_correct
        climo_only_total += climo_only_correct
        total_comparisons += comparisons
        
    print(f"\nResults Summary:")
    print(f"Total comparisons: {total_comparisons}")
    print(f"GFS correct predictions: {gfs_correct_total}")
    print(f"Climatology correct predictions: {climo_correct_total}")
    
    gfs_accuracy = gfs_correct_total / total_comparisons if total_comparisons > 0 else 0
    climo_accuracy = climo_correct_total / total_comparisons if total_comparisons > 0 else 0
    
    print(f"GFS accuracy: {gfs_accuracy:.4f} ({gfs_accuracy * 100:.2f}%)")
    print(f"Climatology accuracy: {climo_accuracy:.4f} ({climo_accuracy * 100:.2f}%)")
    
    accuracy_advantage = gfs_accuracy - climo_accuracy
    print(f"GFS advantage over climatology: {accuracy_advantage*100:.2f} percentage points")
    
    # Calculate disagreement wins percentage
    total_disagreements = disagreements_gfs_wins + disagreements_climo_wins
    if total_disagreements > 0:
        gfs_win_rate = disagreements_gfs_wins / total_disagreements
        print(f"When GFS ≠ Climatology, GFS wins: {gfs_win_rate * 100:.2f}% of time ({disagreements_gfs_wins}/{total_disagreements})")
    else:
        print("No disagreements found between GFS and climatology during the test period.")

    # Build contingency table for McNemar's test
    contingency_table = {
        'both_correct': both_correct_total,
        'gfs_only_correct': gfs_only_total,
        'climo_only_correct': climo_only_total,
        'both_wrong': both_wrong_total
    }
    
    print(f"\nContingency Table Results:")
    print(f"Both correct: {both_correct_total}")
    print(f"GFS only correct (Climatology wrong): {gfs_only_total}")
    print(f"Climatology only correct (GFS wrong): {climo_only_total}")
    print(f"Both wrong: {both_wrong_total}")

    # Perform McNemar's test
    chi2_stat, p_value = perform_mcnemar_test(contingency_table)
    
    print(f"\nMcNemar's Test Results:")
    print(f"Chi-squared statistic: {chi2_stat:.4f}")
    print(f"P-value: {p_value:.4f}")
    
    if p_value < 0.05:
        print("Result is statistically significant (p < 0.05) - the difference in accuracy is meaningful.")
    else:
        print("Result is not statistically significant (p >= 0.05).")
    
    print(f"\nValidation Check:")
    expected_advantage = 0.284  # 28.4 percentage points advantage as specified
    expected_disagreement_win_rate = 0.886  # 88.6% of cases where GFS wins
    
    if abs(accuracy_advantage - expected_advantage) < 0.05:  # Allow 5% tolerange
        print(f"✓ Accuracy advantage ({accuracy_advantage*100:.2f}pp) matches expected ({expected_advantage*100:.2f}pp)")
    else:
        print(f"✗ Accuracy advantage ({accuracy_advantage*100:.2f}pp) does not match expected ({expected_advantage*100:.2f}pp)")
    
    if total_disagreements > 0:
        actual_disagreement_win_rate = disagreements_gfs_wins / total_disagreements
        if abs(actual_disagreement_win_rate - expected_disagreement_win_rate) < 0.05:  # 5% tolerance
            print(f"✓ Disagreement win rate ({actual_disagreement_win_rate*100:.2f}%) matches expected ({expected_disagreement_win_rate*100:.2f}%)")
        else:
            print(f"✗ Disagreement win rate ({actual_disagreement_win_rate*100:.2f}%) does not match expected ({expected_disagreement_win_rate*100:.2f}%)")
    
    # Report per-station results
    print(f"\nPer-Station Breakdown:")
    print(f"{'Station':<10} {'Comparisons':<11} {'GFS Acc':<8} {'Climo Acc':<10} {'Advantage':<10}")
    print("-" * 52)
    
    for result in all_results:
        if result['comparisons'] > 0:
            gfs_acc = result['gfs_correct'] / result['comparisons']
            climo_acc = result['climo_correct'] / result['comparisons']
            advantage = gfs_acc - climo_acc
            
            print(f"{result['station']:<10} {result['comparisons']:<11} {gfs_acc:.4f}   {climo_acc:.4f}   {advantage*100:8.2f}pp")

    print(f"\nDe-seasonalization test complete!")


def save_validation_results(results, filepath: str = "../../data/phase11_deseasonalization_results.json"):
    """
    Save results of the de-seasonalization validation test.
    """
    import json
    from datetime import datetime
    
    output_data = {
        "validation_run": datetime.now().isoformat(),
        "description": "NWP Direct vs Calendar Climatology deseasonalization validation",
        "stations_analyzed": len([r for r in results if r.get('comparisons', 0) > 0]),
        "total_comparisons": results.get("total_comparisons", 0),
        "gfs_accuracy": results.get("gfs_accuracy", 0),
        "climo_accuracy": results.get("climo_accuracy", 0),
        "accuracy_advantage": results.get("accuracy_advantage", 0),
        "mcnemars_test": results.get("mcnemars_test", {}),
        "expected_outcomes": {
            "accuracy_pp_advantage": 0.284,
            "disagreement_win_rate": 0.886
        }
    }
    
    with open(filepath, 'w') as f:
        json.dump(output_data, f, indent=2)
    
    print(f"Validation results saved to {filepath}")


if __name__ == "__main__":
    main()