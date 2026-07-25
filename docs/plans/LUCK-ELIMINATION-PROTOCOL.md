# B6 — Luck Elimination Protocol

## 13-Point Pass Criteria

**Source:** Gray Room Round 7 — Expert 2 (Market Microstructure), Expert 3 (Signal Architecture), Expert 4 (Risk Systems). Synthesized from consensus.

**Purpose:** Distinguish genuine trading edge from statistical luck. Without this, a lucky month could send a bad system to production.

**When to run:** Weekly during Phase C (Days 11-30 of formal paper test). Final run at end of Phase C before Phase 14 entry.

---

### Scorecard

| # | Criterion | Threshold | Status | Notes |
|---|---|---|---|---|
| 1 | Binomial test | p < 0.01 vs 55% | PENDING | |
| 2 | Bootstrap CI | Lower bound ≥ 55% | PENDING | |
| 3 | Time-stratified | Each week > 55% | PENDING | |
| 4 | Station-stratified | ≥ 80% of stations > 50% | PENDING | |
| 5 | Mirror test | Opposite P&L negative by ≥ 10pp | PENDING | |
| 6 | Signal attribution | ≥ 95% of trades attributed | PENDING | |
| 7 | Uptime | ≥ 99% | PENDING | |
| 8 | Kill-switch activations | 0 | PENDING | |
| 9 | DB corruption | 0 incidents | PENDING | |
| 10 | Config drift | 0 incidents | PENDING | |
| 11 | Operator interventions | ≤ 3 | PENDING | |
| 12 | Data coverage | ≥ 95% | PENDING | |
| 13 | Actionable alerts/day | ≤ 50 | PENDING | |

---

### Detailed Criteria

#### 1. Binomial Test (p < 0.01 vs 55%)

- **Test:** One-sided binomial test on the overall directional accuracy of all trades.
- **Null hypothesis:** True directional accuracy ≤ 55%.
- **Pass condition:** p-value < 0.01 (reject null — edge is statistically significant).
- **Implementation:** `scipy.stats.binomtest(k=crosses, n=trades, p=0.55, alternative='greater')`
- **Why this threshold:** 55% is the breakeven after fees (0.0205 per trade). Beating it at p < 0.01 means the edge is real, not random.

#### 2. Bootstrap Confidence Interval (Lower Bound ≥ 55%)

- **Test:** Generate 10,000 bootstrap resamples of the trade-level accuracy.
- **Pass condition:** 5th percentile of the bootstrap distribution ≥ 55%.
- **Implementation:** `np.random.choice(trade_outcomes, (10000, len(trade_outcomes))).mean(axis=1)`
- **Why:** Ensures the result is robust to outliers and single-station effects.

#### 3. Time- Stratified (Each Week > 55%)

- **Test:** Split the 30-day test into 4-week windows (or 5 6-day windows).
- **Pass condition:** Every window independently shows > 55% directional accuracy.
- **Why:** Prevents a system that works for 3 weeks and collapses in week 4 from graduating.

#### 4. Station- Stratified (≥ 80% of Stations > 50%)

- **Test:** Per-station directional accuracy.
- **Pass condition:** At least 16 of 20 stations (80%) have accuracy > 50%.
- **Why:** A system that only works on 2 stations is a weather-specific artifact, not a general trading edge.

#### 5. Mirror Test (Opposite P&L Negative by ≥ 10pp)

- **Test:** Take the opposite side of every trade and compute the resulting P&L.
- **Pass condition:** Opposite-direction P&L ≤ -10pp (i.e., the mirror strategy loses money).
- **Why:** If the opposite strategy also makes money, the system has a structural bias (e.g., always buying, always leaning up), not a genuine direction-prediction edge.

#### 6. Signal Attribution (≥ 95% of Trades)

- **Test:** Every trade must have a traceable signal_name and reason in the database.
- **Pass condition:** ≥ 95% of trades have complete attribution.
- **Why:** Untraceable trades indicate logging gaps, data corruption, or execution path bugs.

#### 7. Uptime (≥ 99%)

- **Test:** System availability over the 30-day test period.
- **Pass condition:** ≥ 99% uptime (≤ 7.2 hours of cumulative downtime).
- **Why:** A system that's down 10% of the time is not ready for production.

#### 8. Zero Kill-Switch Activations

- **Test:** Count of emergency_kill_switch or halt_file activations.
- **Pass condition:** 0 activations.
- **Why:** Every kill-switch activation represents a systemic failure that should have been caught by monitoring or graceful degradation.

#### 9. Zero DB Corruption Incidents

- **Test:** Daily `PRAGMA integrity_check` across all SQLite databases.
- **Pass condition:** 0 failures across the entire test period.
- **Why:** Silent DB corruption invalidates all historical metrics and trading decisions.

#### 10. Zero Config Drift Incidents

- **Test:** Compare running config to source-of-truth config daily.
- **Pass condition:** 0 unexpected changes.
- **Why:** An environment variable change or config file edit mid-test invalidates before/after comparisons.

#### 11. ≤ 3 Operator Interventions

- **Test:** Count of manual interventions (restarts, config changes, workarounds) during the test.
- **Pass condition:** ≤ 3 total.
- **Why:** Every intervention is a point of human error and a sign of insufficient automation.

#### 12. Data Coverage ≥ 95%

- **Test:** For each station, percentage of settlement dates with METAR data.
- **Pass condition:** ≥ 95% coverage across all stations.
- **Why:** Missing data = missing trades = selection bias.

#### 13. ≤ 50 Actionable Alerts Per Day

- **Test:** Count of alerts from the alert pipeline that require human action.
- **Pass condition:** ≤ 50 per day average.
- **Why:** Alert fatigue destroys operational discipline. If the system generates 200 alerts a day, operators will ignore them.

---

### Execution

#### Prerequisites
- Label-permutation test (B5) must have passed first
- All Phase 9 dead signals must be removed from active roster
- `core/unified_backtest.py` must support per-station, per-week, per-signal metrics

#### Script
A separate script `scripts/test_statistical_significance.py` should implement all 13 tests. Output format:

```json
{
  "version": "1.0",
  "timestamp": "2026-07-25T00:00:00Z",
  "results": {
    "binomial_test": {"pass": true, "p_value": 0.003, "trades": 1234},
    "bootstrap_ci": {"pass": true, "lower_bound": 0.572, "iterations": 10000},
    "time_stratified": {"pass": true, "weeks": [0.58, 0.59, 0.57, 0.56]},
    "station_stratified": {"pass": true, "stations_above_50": 17, "total": 20},
    "mirror_test": {"pass": true, "opposite_pnl_pct": -0.15},
    "signal_attribution": {"pass": true, "attributed_pct": 0.98},
    "uptime": {"pass": true, "uptime_pct": 0.995},
    "kill_switch": {"pass": true, "activations": 0},
    "db_integrity": {"pass": true, "failures": 0},
    "config_drift": {"pass": true, "drifts": 0},
    "operator_interventions": {"pass": true, "count": 1},
    "data_coverage": {"pass": true, "coverage_pct": 0.97},
    "alerts_per_day": {"pass": true, "avg": 32}
  },
  "overall_pass": true,
  "fail_count": 0,
  "total_criteria": 13
}
```

#### Verdict
- **PASS:** All 13 criteria met → system has verifiable edge, proceed to Phase 14 gating.
- **PARTIAL:** 1-3 criteria fail → diagnose failures, fix, re-run after 1 week minimum.
- **FAIL:** 4+ criteria fail → system does not have a demonstrable edge. Do not advance to Phase 14. Return to Phase 9-11 for signal and methodology overhaul.

---

### History

| Date | Verdict | Failures | Notes |
|---|---|---|---|
| — | PENDING | — | Document created |