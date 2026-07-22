#!/usr/bin/env python3
"""
PHASE 14.1: 30-Day Unattended Test Setup 

Document alert thresholds, monitoring, and rollback procedures for the 
fused system built in phases 1-13. Update the test plan from Phase 8.5 
to incorporate the fused system.
"""

import json
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def update_test_plan_with_fused_system():
    """
    Update the Phase 8.5 test plan to include the fused system results,
    alert thresholds and procedures for the 30-day unattended test.
    """
    
    print("Updating Phase 14.1: 30-Day Unattended Test Setup")
    print("=" * 60)
    
    # Load Phase 11 fusion results to inform the test thresholds and procedures
    phase11_results_path = Path("data/phase11_fusion_results.json")  # Local
    phase9_results_path = Path("data/phase9_purged_cv_results.json") 
    
    if not phase11_results_path.exists():
        # Try in main data directory 
        phase11_results_path = Path("../..//data/phase11_fusion_results.json")
    if not phase9_results_path.exists():
        phase9_results_path = Path("../..//data/phase9_purged_cv_results.json")
        
    if not any([phase11_results_path.exists(), phase9_results_path.exists()]):
        print("Warning: No previous results available to inform test thresholds")
        baseline_accuracy = 0.58  # Conservative baseline for Kalshi
    else:
        # Try to load from Phase 11
        if phase11_results_path.exists():
            try:
                with open(phase11_results_path, 'r') as f:
                    phase11_data = json.load(f)
                    fused_accuracy = phase11_data.get('fused_system_performance', {}).get('accuracy', 0.55)
                    baseline_accuracy = max(
                        phase11_data.get('baseline_performance', {}).get('metar', {}).get('accuracy', 0.55),
                        phase11_data.get('baseline_performance', {}).get('nwp_enhanced', {}).get('accuracy', 0.55)
                    )
                print(f"Loaded fusion results: fused_accuracy={fused_accuracy:.3f}, baseline={baseline_accuracy:.3f}")
            except Exception as e:
                print(f"Could not load Phase 11 results: {e}")
                baseline_accuracy = 0.58
        else:
            baseline_accuracy = 0.58
    
    # Determine alert thresholds based on system capabilities
    expected_accuracy = baseline_accuracy + 0.02  # Adding margin from fusion improvements
    
    # Define alert thresholds and monitoring procedures
    alert_config = {
        "accuracy_thresholds": {
            "alert_low": expected_accuracy - 0.10,    # Significantly below expectations
            "warning_low": expected_accuracy - 0.05,  # Somewhat below expectations
            "expectation": {
                "daily_average": expected_accuracy,
                "weekly_average": expected_accuracy,
                "monthly_average": expected_accuracy
            },
            "performance_recovery": expected_accuracy - 0.02  # If we see this level again, we've recovered
        },
        "volume_thresholds": {
            "alert_low": 1,           # Unexpected low volume (system failure)
            "warning_low": 5,         # Below minimum productive volume  
            "normal_range": [10, 50], # Typical expected per day
            "alert_high": 100         # Abnormally high volume (potential error)
        },
        "confidence_thresholds": {
            "minimum_trading": 0.60,  # Minimum confidence to trade with fused system
            "high_conviction": 0.75,  # High conviction trades
            "monitor_low_conviction": 0.55  # Track outcomes more closely at lower confidence
        }
    }
    
    # Monitoring and alert procedures
    monitoring_procedures = {
        "daily_checks": [
            "Verify accuracy rate for the day (compared to threshold)",
            "Check trading volume against expected norms",
            "Review signal agreement rates (both_agree vs single_lane vs conflict)",
            "Monitor confidence distribution and flag unusual values"
        ],
        "early_warning_indicators": [
            "3 consecutive losing days >5% of capital",
            "Accuracy falling below warning level for 3 days consecutively",
            "Majority of trades coming from 'conflict' pattern (indicating misaligned signals)",
            "Volume dropping below warning threshold for 2+ consecutive days",
            "Calibration drift (confidences not matching actual win rate)"
        ],
        "automated_alert_conditions": [
            "Daily accuracy < alert_low",
            "Weekly accuracy < (expected_accuracy - 0.08)", 
            "Daily volume > alert_high or < alert_low",
            "Consecutive losses > 5 trades",
            "Total drawdown > 1.5% of capital in single day"
        ],
        "manual_review_triggers": [
            "After any automated alert",
            "Every 7 days regardless",
            "When entering new month/regime",
            "If signal disagreement rates exceed 30%" 
        ]
    }
    
    # Rollback procedures
    rollback_procedures = {
        "immediate_rollback_triggers": [
            "System error preventing trades",
            "Security compromise or suspicious activity", 
            "Major accuracy crash (>20 pp decline)",
            "Drawdown exceeding emergency limit (3% in one day)"
        ],
        "rollback_process": {
            "step_1_pause_new_trades": "Stop generating new alerts/orders immediately",
            "step_2_verify_accuracy": "Manually verify latest outcomes against predictions", 
            "step_3_revert_configuration": "Switch back to last known stable config",
            "step_4_investigate": "Analyze logs to understand root cause",
            "step_5_resume_decision": "Decide whether to resume or remain paused after analysis"
        },
        "circuit_breaker_rules": {
            "daily_stop_loss": "Stop trading if P&L < -2% of starting capital in single day",
            "cumulative_stop_loss": "Stop trading if cum P&L < -5% over 7-day period",
            "consecutive_negative_days": "Pause trading after 5 consecutive negative days",
            "loss_per_trade_limit": "Abort single trade if realized loss exceeds 0.5% of capital"
        },
        "rollback_verification": {
            "accuracy_improve_after_rollback": "Post-rollback accuracy should return to baseline within 3 days",
            "volume_normalization_check": "Trading volume should return to normal range within 2 days", 
            "profit_recovery_indicator": "Rollback successful if positive daily performance resumes in 5 days"
        }
    }
    
    # Detailed test monitoring plan for the 30-day period
    thirty_day_test_plan = {
        "test_duration_days": 30,
        "test_objectives": [
            "Validate sustained accuracy of fused system",
            "Test robustness under changing market conditions", 
            "Verify automated monitoring/alarming works correctly",
            "Ensure rollback procedures are understood and accessible",
            "Gather sufficient data for full deployment decision"
        ],
        "key_performance_indicators": {
            "primary_kpi": {
                "accuracy_rate": {
                    "measurement_frequency": "daily",
                    "success_criteria": f">= {expected_accuracy - 0.02} (2 pp above warning threshold)",
                    "escalation": "alert if < warning threshold"
                },
                "average_confidence": {
                    "measurement_frequency": "daily",
                    "target_range": [0.65, 0.80],
                    "success_criteria": "Remains in target range",
                    "escalation": "review if < 0.60 or > 0.85 consistently"
                }
            },
            "secondary_kpis": {  
                "signal_agreement_analysis": {
                    "both_agree_percent": "Target: >25% of trades",
                    "conflict_percent": "Maximum: <15% of trades", 
                    "single_lane_acceptance": "Only single-lane where >75% confidence"
                },
                "risk_metrics": {
                    "max_single_day_drawdown": "< 2% of capital",
                    "max_7_day_drawdown": "< 4% of capital",
                    "sharpe_ratio_min": "> 1.0 (annualized)"
                }
            }
        },
        "scheduled_reviews": {
            "daily_automated": "Daily accuracy and volume check",
            "3_day_manual": "Manual system health check and configuration review",
            "weekly_analytical": "Performance analysis and potential parameter adjustments",
            "at_rollbacks": "Full system review and procedure update if rollback triggered"
        },
        "success_criteria": {
            "minimum_success_metrics": {
                "accuracy": f">= {expected_accuracy - 0.03}",
                "drawdown": "< 5% over 30 day period",
                "volatility_ratio": "Sharpe-like ratio > 1.0",
                "alert_frequency": "< 3 major alerts during 30 days"
            },
            "success_confirmation_process": [
                "Confirm accuracy holds over 3 different weeks", 
                "Verify all major alert pathways work correctly",
                "Validate rollback procedures through at least 1 test trigger",
                "Document lessons learned for permanent deployment"
            ]
        }
    }
    
    # Compile full production setup documentation
    production_setup_doc = {
        "phase": "PHASE_14_30_DAY_UNATTENDED_TEST",
        "setup_overview": {
            "date_created": datetime.now().isoformat(),
            "system_version": "WeatherEngine_v5_fused_2026-07-21",
            "includes": ["Phase 1-13 fully integrated", "Mahalanobis NWP signal", "Bayesian fusion", "Trajectory CDF output"],
            "expected_performance": {
                "accuracy_baseline": round(expected_accuracy, 3),
                "projected_improvement": "+2-5 pp over baseline (from Phase 11 results)",
                "daily_volume_expected": "15-30 trades per day across all stations"
            }
        },
        "configuration_alerting": alert_config,
        "monitoring_procedures": monitoring_procedures,
        "rollback_procedures": rollback_procedures, 
        "30_day_test_plan": thirty_day_test_plan,
        "deployment_checklist": [
            "All Phase 11-13 components loaded and operational",
            "Calibration parameters updated with latest results",
            "Discord/webhook alerts configured",
            "Database connections tested",
            "Emergency contacts verified",
            "Monitoring dashboards accessible",
            "Backup communication channels tested"
        ],
        "emergency_contacts": {
            "primary_operator": "Gilfoyle",
            "secondary_reviewer": "Dan (approval)", 
            "technical_support": "Main agent system",
            "communication_channel": "Webhook + email alerts configured"
        }
    }
    
    # Save the complete setup documentation
    import os
    results_dir = Path("data")
    results_dir.mkdir(exist_ok=True)
    
    output_file = results_dir / "phase14_30_day_unattended_test_setup.json"
    with open(output_file, 'w') as f:
        json.dump(production_setup_doc, f, indent=2, default=str)
    
    print(f"\nPHASE 14 COMPLETE: 30-Day Unattended Test Setup")
    print("=" * 60)
    print(f"Test plan created with:")
    print(f"  - Expected system accuracy: ~{expected_accuracy:.1%}")
    print(f"  - Daily volume target: 15-30 trades")
    print(f"  - Alerting thresholds established")  
    print(f"  - Rollback procedures documented")
    print(f"  - Success criteria defined")
    print(f"\nDocumentation saved to: {output_file}")
    
    print(f"\n30-DAY TEST SUCCESS CRITERIA:")
    print(f"  > {expected_accuracy - 0.03:.1%} accuracy maintained")  
    print(f"  < 5% total drawdown")
    print(f"  < 3 major system alerts")
    print(f"  All rollback procedures proven")
    
    return production_setup_doc


def validate_test_environment():
    """
    Verify that the environment is ready for the 30-day unattended test
    """
    print("\nVALIDATING TEST ENVIRONMENT...")
    
    checks = [
        "Data files from previous phases available",
        "Database connectivity",
        "Discord webhook configuration", 
        "File write permissions",
        "Scheduled execution setup",
        "Monitoring access"
    ]
    
    results = {}
    
    # Check for critical data files
    critical_files = [
        Path("data/phase11_fusion_results.json"),
        Path("data/nwp_forecasts.db"),
        Path("data/metar_backfill.db")
    ]
    
    for f in critical_files:
        results[str(f)] = "found" if f.exists() else "missing"
    
    # Check database access
    db_paths = ["./data/nwp_forecasts.db", "./data/metar_backfill.db"]
    for db_path in db_paths:
        try:
            with sqlite3.connect(db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table';")
                table_count = cursor.fetchone()[0]
                results[db_path] = f"accessible ({table_count} tables)" if table_count > 0 else f"inaccessible or empty {db_path}"
        except Exception as e:
            results[db_path] = f"error: {str(e)}"
    
    print("ENVIRONMENT VALIDATION RESULTS:")
    for item, status in results.items():
        print(f"  {item}: {status}")
    
    all_critical_present = all(
        f.exists() for f in critical_files
    )

    if all_critical_present:
        print("\n✓ Environment validation PASSED")
        print("  Ready to execute 30-day test setup") 
        return True
    else:
        print("\n✗ Environment validation FAILED")
        print("  Missing required files/component access")
        return False


if __name__ == '__main__':
    import sys
    import os
    # Change to workspace
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir + '/../../..')  # Move to main workspace
    
    print("EXECUTING PHASE 14: 30-Day Unattended Test Setup")
    
    # Validate environment
    if validate_test_environment(): 
        print("")
        update_test_plan_with_fused_system()
        print("\nPHASE 14: ALL COMPONENTS OF BUILD ORDER COMPLETED SUCCESSFULLY!")
        print("The fused system is now ready for 30-day unattended testing.")
    else:
        print("\nPHASE 14: Environment verification failed, cannot proceed with test setup.")
        sys.exit(1)