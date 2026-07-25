# B7 — Formal Success Criteria

**Source:** Gray Room Round 7 — Expert 4 (Risk Systems) identified that "nobody has defined what success looks like." Merged with Expert 2's Luck Elimination Protocol criteria. All 4 experts endorsed.

**Purpose:** Define measurable Go/No-Go gates for Phase 9 completion and Phase 10 entry. Without hard criteria, a 30-day paper test produces a 30-page report nobody can use.

---

## Phase 9 Completion Gate (All items must pass)

| # | Criterion | Threshold | Status | Due |
|---|---|---|---|---|
| 1 | Label-permutation test passed | p < 0.01 or accuracy > 2σ above shuffle mean | DONE | Phase 9 |
| 2 | B5 (Dead signals removed from engine) | No dead signal registered in SignalRegistry | DONE | Phase 9 |
| 3 | B5 (Goldilocks daily signal removed) | C4 | DONE | Phase 9 |
| 4 | Seasonal regime interface fixed | evaluate() returns 'up'/'down' | DONE | Phase 9 |
| 5 | Fee-adjusted accuracy metric exists | C9 | DONE | Phase 9 |
| 6 | Luck Elimination Protocol documented | B6 | DONE | Phase 9 |
| 7 | Formal Success Criteria documented | This document | DONE | Phase 9 |
| 8 | Monitoring dashboard exists | B2 | DONE | Phase 9 |
| 9 | Daily ops check script exists | scripts/daily_ops_check.py | DONE | Phase 9 |

**Phase 9 verdict: ________ (PASS / FAIL)**

---

## Phase 10 Entry Gate (All must pass before Phase 10 begins)

| # | Criterion | Threshold | Status | Notes |
|---|---|---|---|---|
| 1 | Phase 9 completion | All 9 items above PASS | PENDING | |
| 2 | Goldilocks confidence inversion fix | Confidence correctly inversely scored for LOW markets | PENDING | A1 |
| 3 | Goldilocks naming/refactoring | Two distinct signals: spike_reversion + regime_alignment | PENDING | A3 |
| 4 | Rolling 30-day calibration | Calibration uses rolling window, not full history | PENDING | A9 |
| 5 | Station-ranking filter | Only trade stations where signal passes pre-test | PENDING | A8 |
| 6 | Frontal passage detector | C10 built, tested, gated | PENDING | C10 |
| 7 | Alert pipeline clean | ≤ 50 actionable alerts/day (B6 #13) | PENDING | |

**Phase 10 entry verdict: ________ (PASS / FAIL)**

---

## Phase 11 Entry Gate (Before fusion overhaul)

| # | Criteria | Threshold |
|---|---|---|
| 1 | Phase 10 completion | All 7 items above PASS |
| 2 | Goldilocks 72-hour latency diagnostic | No material latency > 30s in alert pipeline |
| 3 | Per-signal accuracy baseline | Each active signal has documented standalone accuracy |

---

## Phase 12 Entry Gate (Before spatial coherence)

| # | Criteria | Threshold |
|---|---|---|
| 1 | Phase 11 completion | All 3 items above PASS |
| 2 | Variance-weighted sizing | C8 implemented and verified |
| 3 | Bayesian log-odds fusion | Wired and backtested |
| 4 | Three-lane architecture | C3 adopted |

---

## Phase 13 Entry Gate (Before 30-day paper test)

| # | Criteria | Threshold |
|---|---|---|
| 1 | Phase 12 completion | All items PASS |
| 2 | Luck Elimination Protocol | Script exists (B6), 13 criteria documented |
| 3 | Three-phase rollout plan | B1 documented: Smoke (days 1-3), Stabilization (days 4-10), Autonomous (days 11-30) |
| 4 | Chaos engineering | Day 2: inject failures, verify Degraded mode |
| 5 | Stop-loss monitor wired | B14 wired into main loop |
| 6 | Unify risk config | B15: single RiskConfig source of truth |
| 7 | Cron overlap protection | B21: no concurrent cron cycles |
| 8 | SQLite WAL mode confirmed | All 50+ production DBs in WAL mode |
| 9 | Halt file mechanism confirmed | halt_check.py wired, graceful degradation tested |
| 10 | Infrastructure runbook exists | B26: abort conditions documented |

---

## Phase 14 Entry Gate (Real-money production)

| # | Criteria | Threshold |
|---|---|---|
| 1 | Phase 13 completion | PASS |
| 2 | Luck Elimination Protocol passed | All 13 criteria PASS |
| 3 | Goldilocks real-money gate | A10: ≥ 60% accuracy, 30 days shadow, ≥ 0.30 Sharpe |
| 4 | Settlement-confirmed accuracy ≥ 55% | D3 verified from actual settlement data |
| 5 | Disaster recovery runbook exists | D7 documented and tested |
| 6 | Capacity planning stress test | D10: can handle 20 stations × 2 markets × 1-min polling |
| 7 | Loss distribution acceptable | D6: no outlier trades > 3σ from mean |
| 8 | Spatial correlation accounted | D4: cluster independence verified, cluster-based budget applied |

---

## Phase 9 Go/No-Go Decision

| Criterion | Weight | Required |
|---|---|---|
| All Phase 9 items complete | Blocking | 9/9 |
| Label-permutation test confirms signal edge | Blocking | Pass |
| Dead signals removed from all active code paths | Blocking | Clean grep |
| Luck Elimination Protocol documented | Blocking | File exists |
| Formal Success Criteria documented | Blocking | File exists |

---

## Version History

| Date | Version | Author | Changes |
|---|---|---|---|
| 2026-07-24 | 1.0 | Phase 9 B-Mode | Initial document — gates defined for Phases 9-14 |