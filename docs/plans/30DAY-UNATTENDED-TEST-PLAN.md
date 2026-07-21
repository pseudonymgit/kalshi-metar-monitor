# 30-Day Unattended Test Plan

## Overview
This document outlines the setup and procedures for the 30-day unattended test of the weather trading system. This is the final validation step before any real-money trading, ensuring the system operates autonomously and safely while being continuously monitored without human intervention.

## Objectives
- Test production stability without human intervention
- Validate alert systems and monitoring capabilities
- Verify automated reporting functionality
- Confirm system behavior across typical market conditions
- Establish baseline patterns of normal operations

## Pre-Launch Configuration

### 1. Instance Setup
- Deploy to staging Sandbox (SBOX) instance using validated code from `main` branch
- Database: Read replicas only for market data (no writing to live tables)
- Connect to Kalshi Demo API endpoint using demo credentials
- Disable any actual trade execution (read-only mode only!)
- Enable comprehensive logging to capture all system activities

### 2. Security Configuration
- Disable real-money trading capabilities completely
- Implement rate limiting per Kalshi API guidelines
- Establish secure connection to remote logging and monitoring services
- Ensure all keys are for demo/sandbox mode only

### 3. Best Ensemble Selection
Based on Phase 8 results from:
- `data/phase8_combinatorial_search.json`
- `data/phase8_calibrated_search.json` 
- `data/phase8_parameter_sweep.json`
- `data/phase8_purged_cv_results.json`

Selected optimal parameters:
- Confidence threshold: [PARAMETER SWEEP RESULT VALUE]
- Agreement threshold: [PARAMETER SWEEP RESULT VALUE]
- Window length: [PARAMETER SWEEP RESULT VALUE]
- Signals: [TOP SIGNALS FROM COMBINATORIAL ANALYSIS]
- Dewpoint modulation: [CONFIGURATION RESULT]

## Monitoring & Alerting System

### 1. Daily Auto-Summaries via Discord Webhook
- Scheduled at 23:59 UTC daily
- Includes: accuracy metrics, trade count, signal-specific performance
- Distribution: designated monitoring Discord channel
- Format: structured JSON with human-readable summaries

### 2. Critical Alerts to Read-Only Discord Channel
Set up webhook integrations with these alerting rules:

#### High Priority (Immediate Notification)
- Database connection failures
- Kalshi API authentication failures
- System crashes/critical exceptions
- Accuracy drops >10% week-over-week
- Unexpected high-volume trading signals (>100 trades/day at 6 locations or >50 at 12+ locations)

#### Medium Priority (Daily Summary)
- Individual signal accuracy <60%
- System uptime degradation
- Alert frequency anomalies
- Data fetch latencies >30s

### 3. Alert De-duplication and Rate Limiting
- Implement throttling: same alert ID ignored for 6 hours
- Stateful tracking: "recovering X previously reported issue" when conditions return to normal
- Severity levels and escalation triggers

## Operational Procedures

### 1. Daily Checks (Automated)
- Verify all signals remain above 6% accuracy thresholds
- Check Kalshi market updates are retrieved successfully
- Monitor for missing data gaps in METAR feeds
- Validate alerting endpoints remain accessible

### 2. Emergency Protocols
If critical alert fires during 30-day window:
- Automatically halt further signal generation until review
- Immediately notify engineering team
- Log decision and remedial action taken
- Restart only after addressing root cause

### 3. Weekly Performance Reviews
- Manually assess automated summaries
- Verify no false positives in alert system
- Confirm accuracy patterns remain consistent
- Document environmental factors that may impact performance

## Metrics to Track

### Primary Performance Indicators
- Prediction accuracy by station
- Sharpe ratio per station and overall
- Number of trades executed (should be 0 for this test)
- Signal correlation to avoid spurious signals

### System Health Indicators
- Uptime percentage
- Database query times
- External API response times
- Memory/CPU utilization

### Alerting Efficacy
- Number of false positives
- Time to alert delivery
- Rate of alert suppression/merging
- Alert pattern consistency

## Rollback Procedures

Under the following conditions, immediately terminate the test:
- Multiple daily critical alerts triggered 
- Accuracy falls below 62% sustained period (2+ weeks)
- Any unintended real money transactions attempted

## Success Criteria

For test success and progression to real-money trading, we require:

1. **Stability**: No critical system failures causing downtime
2. **Alert Hygiene**: <5 false daily alerts on average
3. **Performance Consistency**: Prediction accuracy remains above 65% for 80% of days
4. **Monitoring Compliance**: All auto-summaries delivered as scheduled
5. **Security**: Zero breaches or unauthorized accesses

## Post-Test Actions

Upon successful completion:
1. Document all metrics and lessons learned
2. Compile performance profile across all 20 stations  
3. Validate parameter settings chosen in Phase 8.3
4. Prepare go-ahead documentation for real-money deployment
5. Create incident response playbooks based on observed patterns

## Timeline
- Launch: Immediately upon configuration completion
- Duration: 30 continuous days of operation
- Review: Day 31 morning by engineering team
- Decision Point: Proceed to real-money operation or diagnose issues

---

Document Version: 1.0  
Last Updated: [TIMESTAMP TO BE AUTO-INSERTED]  
Owner: Engineering Team  
Next Review: After 30-day test completion