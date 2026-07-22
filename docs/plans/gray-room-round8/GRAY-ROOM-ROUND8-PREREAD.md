# Gray Room Round 8 — Phase 16: Functionality Review & System Architecture

**Date:** 2026-07-22
**Pre-read for:** All experts
**Context:** Weather engine has completed Phases 1-15. Phase 15 code review (119 files, 54 with bugs) revealed architectural issues requiring expert judgment.

---

## System State (as of 2026-07-22)

### Core Runtime
- **Predictions:** Next-day HIGH/LOW directional for 20 US cities
- **Best ensemble:** `pressure_delta + forecast_disagreement + calendar_climatology` at agree=3
- **Accuracy:** 72.30% (2,657 trades, 20 stations)
- **At agree=2:** 66.20% (9,977 trades)
- **Primary signals (9 active):** calendar_climatology, forecast_disagreement, frontal_detector, gaussian, gaussian_v2, persistence, pressure_delta, regime, wind_direction_shift
- **NWP registered:** NwpDirectSignal (92.7% GFS direction) — NOT wired into main pipeline
- **Pipeline output:** Paper trading engine (deterministic, no AI in loop)

### Codebase
- **Total files:** 118 `.py` files in `core/`
- **Lines of code:** ~52,000 total
- **Biggest files:**
  - `metar_monitor.py`: 5,007 lines (polling, normalization, state management)
  - `paper_trading_engine.py`: 3,104 lines (signal pipeline, sizing, journaling, alerting)
  - `kalshi_monitor.py`: 3,062 lines (market monitoring, price fetching)
  - `signal_fusion.py`: 1,268 lines (fusion, conviction, scoring, trade decisions)
- **3 files = ~12,400 lines = 24% of codebase**

### Phase 15 Code Review Findings
- 54 files with actual bugs found
- 87+ division by zero risks
- 49+ naive `datetime.now()` (no timezone)
- 18 bare except clauses
- 5 hardcoded fee rates (0.05, should be 0.0 — Kalshi charges zero commission)
- 1 dead signal still registered (nwp_analog, 49.2%)
- 1 live signal not wired in (nwp_direct, 92.7%)
- Code defaults agreement threshold to 2 (info map recommends 3)

### 4 Orphan Files Restored (from git commit 0f99ac6)
- `alert_formatter.py` (242 lines) — STALE, may conflict with current alert architecture
- `conviction.py` (265 lines) — STALE, duplicates signal_fusion.py
- `nine_signal_ensemble.py` (311 lines) — DEAD, replaced by fusion architecture
- `unified_backtest.py` (332 lines) — DEAD, replaced by p3_backtest_engine

### Phase 14 Deployment Test
- Result: 69.67% accuracy (300 trades, 16 days, agree=1)
- **Critical finding:** Test scripts are Monte Carlo simulations, NOT real backtests
- They randomly sample from expected accuracy rather than executing historical signals
- No real 30-day unattended test has been run

### Short-Duration Trading (Gray Room Round 7)
- Priority signals:
  1. FOGR reversion (METAR + diurnal curve)
  2. METAR dT/dt trend (1h/3h/6h windows)
  3. Pressure tendency leading indicator
- Data sources: HRRR (not collected yet), METAR (already streaming), HGEFS ensemble (31 members, hourly)
- Entry: sequential trigger (Signal A → confirm with B → trail with C)
- Exit: time-decay + confidence decay
- HRRR collection scoped but not started

### Production Trading Dashboard
- Currently have: `core/dashboard.py` (Flask + Plotly, technical health dashboard)
- Currently have: `core/confidence_dashboard.py` (Monte Carlo simulation, rolling metrics)
- **Missing:** Portfolio view, position management, alert feed, performance analytics, risk dashboard
- All Phase 4 dashboard components were built, but as technical health dashboards, not trader-facing

### Gray Room R7 Synthesis
- Expert 1 (Meteorologist): HRRR bias-corrected → 80-85% at 1-3h
- Expert 2 (Market Micro): Tiered confirmation entry, time-decay + confidence decay exits
- Expert 3 (Signal Fusion): ESDR, FOGR, METAR dT/dt trends
- Full document: `docs/plans/GRAY-ROOM-ROUND7-SYNTHESIS-2026-07-21.md`

### Current Roadmap
- Phase 15: Code review & file changelogs (COMPLETE)
- Phase 16: Gray Room functionality review (YOU ARE HERE)
- Phase 17: Production trading dashboard (QUEUED)
- After 17: HRRR integration / Short-duration pipeline

---

## Advisory Questions

### A. Software Architecture (for Expert 1)
1. **Codebase monolith:** Our 4 biggest files = 24% of the codebase. `paper_trading_engine.py` has signal extraction, position sizing, journaling, and alerting all in one file. What's the cleanest refactoring strategy? Where to split? Order of operations?
2. **Orphan files:** 4 files restored from git. Two are STALE (may conflict with current architecture). Keep, delete, or merge?
3. **Test methodology:** Phase 14 test scripts are Monte Carlo simulations. What's the correct way to run a proper deployment test? What needs to change?
4. **Code quality debt:** 87 div-by-zero risks, 49 naive datetimes, 18 bare excepts. Which are actually dangerous vs. which are just noise?

### B. Short-Duration Trading (for Expert 2)
1. **Build order:** Gray Room R7 identified FOGR, dT/dt, and pressure tendency as priority signals. What's the optimal build order? Any signals we should add or drop?
2. **HRRR timing:** HRRR collection was scoped but not started. Should we build short-duration signals with METAR-only first, or wait for HRRR?
3. **Entry/exit architecture:** Sequential trigger vs. majority-vote ensemble for sub-daily trades. Which is better for minutes-to-hours horizons?
4. **Confidence thresholds:** What confidence levels justify entry for 1h, 3h, 6h horizons?

### C. Production Dashboard (for Expert 3)
1. **Build order:** Portfolio view, position management, alert feed, performance analytics, risk dashboard. What's the priority?
2. **Architecture:** Extend existing Flask dashboards or rebuild from scratch?
3. **Data sources:** What's the data flow from paper trading engine to dashboard? Real-time or replay?
4. **Alert integration:** How should the existing alert pipeline connect to the dashboard?

---

## Expert Deliverable Requirements

Each expert must produce:

**1. ERRORS (10+):** Actual bugs, design flaws, missing features, or wrong assumptions in the current system relevant to their domain. Each must include:
- What the error is
- Where it manifests (file:line or process step)
- Why it's wrong
- Spec to fix

**2. IMPROVEMENTS (5+):** Changes that would make the existing system better. Each must include:
- What to change
- Why it's better
- Estimated effort (Low/Medium/High)
- Spec for implementation

**3. IDEAS (3+):** New approaches, features, or directions. Each must include:
- The idea
- Expected benefit
- Risk/uncertainty
- Spec for validation

**4. ELEPHANTS (2+):** Major architectural problems. Each must include:
- What the elephant is
- Why it matters
- What happens if we ignore it
- Spec for resolution

--- 

*Pre-read compiled 2026-07-22. Experts may reference Phase 15 review doc at `docs/plans/PHASE15-CODE-REVIEW-2026-07-21.md` and Gray Room R7 synthesis at `docs/plans/GRAY-ROOM-ROUND7-SYNTHESIS-2026-07-21.md`.*
