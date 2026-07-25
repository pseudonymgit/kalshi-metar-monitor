# A-Mode Autonomous Phase Execution Runbook

## Overview
This runbook is executed by the A-Mode dispatcher cron job. It reads the current state from `.meta/continuity/weather-engine/a-mode-state.json`, determines the next action, executes it, and updates the state.

## Operating Principle
A-mode executes the phased roadmap (Phases 8.5-14) autonomously. Each phase has clear tasks, success criteria, and gates. The dispatcher does NOT need Dan or Donna to route each phase — it reads the state file and executes the next uncompleted task.

B-mode (production reliability) runs in parallel. P0 items are B-mode, not deferred.

## Entry Point
When the cron fires, the subagent should:
1. Read `.meta/continuity/weather-engine/a-mode-state.json`
2. Read this runbook
3. Read `docs/plans/GRAY-ROOM-ROUND7-SYNTHESIS.md` for full task specs
4. Read `docs/plans/WEATHER-ENGINE-MASTER-ROADMAP.md` for roadmap context
5. Determine the current phase and next uncompleted task
6. Execute the task
7. Update `a-mode-state.json` with results
8. Surface any blocking issues via announce delivery

9. **Self-improvement step (mandatory):** After every task, include a `## Lessons Learned` section in the handoff file. Document:
    - What went wrong or was harder than expected
    - Pattern check: search 2-3 past continuity files for similar keywords. If this has happened before, note it.
    - Whether this should become a durable rule (lint check, runbook update, config fix)
    - If the pattern appears ≥3 times across any continuity files, escalate to Dan with a recommendation

## Phase Execution Rules

### Phase 0 — Verify P0 Monitors Against Live System

**Priority: FIRST — before any build work.**

B-Mode Cycle 1 built 5 P0 diagnostic monitors. They need to be verified against the live running system NOW.

1. **Verify stalled ingestion detection**
   - Run the `data_freshness_monitor` against the live system
   - Confirm it detects when a station hasn't received data in >15 minutes
   - Confirm it enters DEGRADED at >30 min, HALT at >120 min
   - Reference: `.meta/continuity/weather-engine/bmode-p0-cycle1.md` P0a

2. **Verify ingestion parity checker**
   - Run `check_ingestion_parity.py` against all 20 stations
   - Confirm it flags stations whose last fetch time > 2× median
   - Reference: `.meta/continuity/weather-engine/bmode-p0-cycle1.md` P0b

3. **Verify alert emissions audit**
   - Run `audit_alert_emissions.py` for the last 24h
   - Confirm station-by-station delta of transitions vs alerts emitted
   - Reference: `.meta/continuity/weather-engine/bmode-p0-cycle1.md` P0c

4. **Verify integer-cross consistency**
   - Run diagnostic script that routes same temperature through both implementations
   - Confirm they produce identical results within floating-point tolerance
   - Reference: `.meta/continuity/weather-engine/bmode-p0-cycle1.md` P0d

5. **Verify per-station scheduler execution**
   - Run `get_station_polling_status()` for all active stations
   - Confirm each station polled within expected interval (3min METAR)
   - Reference: `.meta/continuity/weather-engine/bmode-p0-cycle1.md` P0e

**Gate check:** All 5 P0 monitors verified against live system → Phase 9

### Phase 8.5 — Infrastructure Hardening (DEFERRED)

Phase 8.5 remaining items (disk space monitoring, pre-trade integrity check, StopLossMonitor wiring, risk config unification, cron overlap protection, abort condition runbooks, DST/timezone audit) are deferred. They are lower priority than signal cleanup and Goldilocks V2. They will be addressed after Phase 10 if needed, or as B-mode items.

### Phase 9 — Signal Cleanup & Diagnostics

**Critical order:** Label-permutation test MUST run first. If it fails, ALL downstream work is invalidated.

1. **Label-permutation test (B5) — DAY 1, MUST RUN FIRST**
   - Shuffle prediction labels vs. outcomes, re-run calibration
   - If shuffled accuracy > 53% → signal methodology has structural leakage → STOP, escalate
   - If < 53% → signals are genuine → PROCEED
   - Reference: GRAY-ROOM-ROUND7-SYNTHESIS.md Section B5

2. **Kill daily-level GoldilocksSignal from signal list (A2)**
   - Remove `GoldilocksSignal` from signal list in `paper_trading_engine.py` lines 973-982 and 318-326
   - The real-time Goldilocks in `metar_monitor.py` remains as a separate lane
   - Reference: GRAY-ROOM-ROUND7-SYNTHESIS.md Section A2

3. **KILL dead signals from active roster (C4)**
   - KILL: persistence, metar_dtdt, pressure_tendency, seasonal_regime, esdr, nwp_direct
   - Remove from signal registry and all signal lists
   - Reference: GRAY-ROOM-ROUND7-SYNTHESIS.md Section C4

4. **Fix seasonal_regime interface mismatch (C7)**
   - Currently returns regime names ('deep_winter') instead of directional predictions
   - Either fix the interface or remove from active signal list
   - Reference: GRAY-ROOM-ROUND7-SYNTHESIS.md Section C7

5. **Fee-adjusted accuracy (C9)**
   - Implement fee-adjusted accuracy as primary metric
   - Display alongside raw accuracy everywhere
   - Reference: GRAY-ROOM-ROUND7-SYNTHESIS.md Section C9

6. **Unified monitoring dashboard (B2)**
   - `scripts/daily_ops_check.py` — run via cron at 06:00 UTC
   - Queries: infrastructure checks, data pipeline, trading checks, Goldilocks shadow
   - Posts structured report to Discord
   - Reference: GRAY-ROOM-ROUND7-SYNTHESIS.md Section B2

7. **Luck Elimination Protocol (B6)**
   - 13-point pass criteria in `scripts/test_statistical_significance.py`
   - Reference: GRAY-ROOM-ROUND7-SYNTHESIS.md Section B6

8. **Formal success criteria (B7)**
   - Document pass/fail thresholds before Phase A begins
   - Reference: GRAY-ROOM-ROUND7-SYNTHESIS.md Section B7

**Gate check:** All 8 tasks complete + label-permutation test passes → Phase 10.

### Phase 10 — Goldilocks V2 & Frontal Detector

1. **Fix Goldilocks confidence inversion (A1)**
   - `metar_monitor.py:144-185`: Replace `is_daily_high` logic with `is_transient_spike` logic
   - `running_max_delta < 0.3°F → base=0.50, else base=0.10`
   - Add margin penalty and observations-since-spike bonus
   - Reference: GRAY-ROOM-ROUND7-SYNTHESIS.md Section A1

2. **LOW market detection (A2-lane)**
   - Track running MINIMUM alongside running maximum
   - Detect transient downward dips
   - Symmetric confidence logic (`is_transient_low_dip`)
   - Create LOW market trades when Goldilocks fires on downward transient
   - Reference: GRAY-ROOM-ROUND7-SYNTHESIS.md Section A2 (LOW)

3. **Rename/refactor Goldilocks (A3)**
   - Daily-level `GoldilocksSignal` → `MeanReversionSignal` (then KILL per A2)
   - Real-time Goldilocks → `MicrostructureSpikeDetector` in `metar_monitor.py`
   - Reference: GRAY-ROOM-ROUND7-SYNTHESIS.md Section A3

4. **Dual-hypothesis engine (A4) — shadow mode**
   - Run two parallel engines: H1 (overreaction → bet AGAINST) and H2 (underreaction → bet WITH)
   - Record market price at t=0 and t=1h for each event
   - After 30 events: whichever has >60% accuracy is the production edge
   - Reference: GRAY-ROOM-ROUND7-SYNTHESIS.md Section A4

5. **METAR QC flag parsing (A5)**
   - `goldilocks_qc_filter()` — skip if auto_station, maintenance_flag, or sensor_malfunction
   - Reference: GRAY-ROOM-ROUND7-SYNTHESIS.md Section A5

6. **Settlement-aware execution gate (A6)**
   - Before executing Goldilocks trade: verify `running_max_delta < 0.3°F`
   - Gate position sizing on settlement scenario classification
   - Reference: GRAY-ROOM-ROUND7-SYNTHESIS.md Section A6

7. **Station-rank-selective activation (A8)**
   - `GOLDILOCKS_ACTIVE_STATIONS` limited to ~6-8 major airports
   - Reference: GRAY-ROOM-ROUND7-SYNTHESIS.md Section A8

8. **Rolling 30-day calibration window (A9)**
   - Use trailing 30-day window of spike/reversion statistics, not fixed parameters
   - Reference: GRAY-ROOM-ROUND7-SYNTHESIS.md Section A9

9. **Frontal passage detector (C10)**
   - Build from Round 5 spec A16
   - Reference: GRAY-ROOM-ROUND7-SYNTHESIS.md Section C10

**Gate check:** All 9 tasks complete + Goldilocks shadow mode running → Phase 11.

### Phase 11 — Fusion & Calibration Overhaul

1. **Variance-weighted ensemble position sizing (C8)**
   - Weight signals by inverse variance of their error distribution
   - Reduces impact of high-variance signals on ensemble
   - Reference: GRAY-ROOM-ROUND7-SYNTHESIS.md Section C8

2. **Bayesian log-odds fusion (Gray Room Round 6)**
   - MI-decorrelation + log-odds fusion
   - Replace existing OR-gate fusion with proper Bayesian log-odds
   - Reference: `docs/plans/WEATHER-ENGINE-GRAY-ROOM-ROUND6-SYNTHESIS.md`

3. **Three-lane architecture (C3)**
   - Formal adoption: Lane 1 (ensemble forecasting), Lane 2 (Goldilocks microstructure), Lane 3 (NWP/analog)
   - Reference: GRAY-ROOM-ROUND7-SYNTHESIS.md Section C3

**Gate check:** All 3 tasks complete → Phase 12.

### Phase 12 — Spatial Coherence Gate

1. **Spatial correlation gate (D4)**
   - Cluster stations by weather regime
   - Count cluster-level rather than station-level trade independence
   - Recompute all metrics with effective N
   - Reference: GRAY-ROOM-ROUND7-SYNTHESIS.md Section D4

2. **Process-aware trade coordination (D2)**
   - Prevent self-trading across cron instances
   - Add process lock + trade deduplication
   - Reference: GRAY-ROOM-ROUND7-SYNTHESIS.md Section D2

**Gate check:** Both tasks complete → Phase 13.

### Phase 13 — 30-Day Formal Paper Test

1. **Three-phase rollout (B1)**
   - Phase A (Days 1-3): Smoke test + chaos engineering
   - Phase B (Days 4-10): Stabilization, daily check-ins
   - Phase C (Days 11-30): Autonomous, weekly check-ins
   - Reference: GRAY-ROOM-ROUND7-SYNTHESIS.md Section B1

2. **Goldilocks shadow mode (B10)**
   - Phase A: disabled entirely
   - Phase B: shadow ON, separate DB
   - Phase C: shadow ON if Phase B accuracy > 50%
   - Reference: GRAY-ROOM-ROUND7-SYNTHESIS.md Section B10

3. **Graduate test rollout (B11)**
   - $100 → $500 → $2,500 → $10,000
   - Reference: GRAY-ROOM-ROUND7-SYNTHESIS.md Section B11

4. **Settlement-confirmed accuracy logging (D3)**
   - Compare prediction outcome to actual settlement bucket
   - Implement delayed accuracy metric
   - Reference: GRAY-ROOM-ROUND7-SYNTHESIS.md Section D3

**Gate:** All 13 Luck Elimination Protocol criteria pass → Phase 14.

### Phase 14 — Production

1. Goldilocks real-money allocation gate (A10)
2. Fix loss distribution / structural Sharpe problem (D6)
3. Disaster recovery runbook (D7)
4. Capacity planning / stress testing (D10)
5. SQLite migration to PostgreSQL (D8)

## Stuck Detection
If the dispatcher finds the current phase has been running for > 7 days without progress:
1. Check if any task has been in-progress > 48 hours without completion
2. If stuck: escalate with full state summary via announce delivery
3. Do NOT skip tasks — they're in priority order for a reason

## Escalation Triggers
Surface to Dan (via announce delivery) when:
- Label-permutation test fails (shuffled accuracy > 53%)
- A phase is stuck > 7 days
- System enters HALTED or EMERGENCY state
- A critical bug is discovered that invalidates previous work
- A phase completes (summary of results)

Do NOT surface for:
- Normal task completion (just update state file)
- Expected warnings or degraded states
- B-mode items that are running normally

## File Locations
- Continuity state: `.meta/continuity/weather-engine/a-mode-state.json`
- Master roadmap: `docs/plans/WEATHER-ENGINE-MASTER-ROADMAP.md`
- Gray Room Round 7 synthesis: `docs/plans/GRAY-ROOM-ROUND7-SYNTHESIS.md`
- Runbook (this file): `docs/plans/A-MODE-RUNBOOK.md`
- Weather engine source: `prototypes/weather-engine-source/`
- ROLLING_TODO.md: `prototypes/weather-engine-source/docs/ROLLING_TODO.md`

## Model Recommendations
- Phase 8.5 (infrastructure): technical Python/SQL — use Qwen3 Coder Plus or GLM-5.2
- Phase 9 (signal cleanup): stats + signal architecture — use GPT-5.4 or DeepSeek V3.1
- Phase 10 (Goldilocks V2): signal logic + METAR domain — use GPT-5.4 or Qwen3 Coder Plus
- Phase 11 (fusion): math-heavy — use GPT-5.4
- Phase 12 (spatial): stats + clustering — use GPT-5.4 or GLM-5.2
- Phase 13 (testing): operational — use Qwen3 Coder Plus
- Phase 14 (production): operational — use Qwen3 Coder Plus

## Important Constraints
- All merges go through Dan unless one-off exception granted
- No AI model calls inside backtest/trading loops (scripts only)
- fee_rate = 0.0205 everywhere (actual Kalshi cost)
- Goldilocks shadow mode only — no real-money Goldilocks trades
- B-mode runs continuously in parallel — do not defer P0 items
- SOP: update tracking files after every subagent completion