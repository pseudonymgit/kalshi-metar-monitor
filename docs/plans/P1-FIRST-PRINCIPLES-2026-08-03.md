# First-Principles Analysis — P1 Items (2026-08-03)

**Analysis:** Donna Paulsen
**Scope:** All P1 items across both active (GEFS cron) and Phase B (legacy alert-path) systems
**Method:** Strip away assumptions. What's the actual problem? What's the simplest approach?

---

## P1 Items That ALREADY HAD Gray Room Review

The following P1 items from the active GEFS pipeline were reviewed by Gray Room Rounds 13-14 (4 experts, 26 findings, 19 ADVANCE). They are validated and ready to build:

| Roadmap # | P1 Item | GR Coverage | Verdict |
|-----------|---------|-------------|---------|
| 1 | Run full backtest (90-365 days) | Implicit — S6 (accuracy CI) | **ADVANCE** |
| 2 | Station tradability audit | S3 (GPT 5.4) | **ADVANCE** |
| 3 | Wire Edge 20 (multi_model_ensemble) | I6 (NWP available now) | **ADVANCE** |
| 4 | Epoch-based Kelly schedule | I2 (GLM 5.2 full config) | **ADVANCE** |
| 5 | Urban heat island correction | I5 (DeepSeek V3.1) | **ADVANCE** |
| 6 | Per-station calibration curves | E5, S5 (GPT 5.4, GLM 5.2) | **ADVANCE** |

**These don't need additional first-principles treatment.** The Gray Room already did the fundamental analysis. Build them.

---

## P1 Items That DID NOT Have Gray Room Review

### Phase B Legacy System: Structural Edge Preservation (6 items)

These belong to the old alert-path infrastructure (METAR-based, pre-GEFS). They have never been reviewed by any Gray Room expert.

| # | Item | Original Purpose |
|---|------|-----------------|
| S1 | Goldilocks alert surfacing | Surface alert-level Goldilocks transitions |
| S2 | Replay verification for Goldilocks transitions | Verify deterministic replay |
| S3 | Detection auditability for advance-and-revert sequences | Trace detection events |
| S4 | Station-day visibility for structural events | Per-station event log |
| S5 | Missed-event detection with root-cause traceability | Detect missed events |
| S6 | Missed-event detection implementation | Implementation of S5 |

### First-Principles Analysis

**Q: What is the actual problem these items solve?**
Keeping the old alert-path deterministic and auditable. The system uses METAR → integer-cross detection → alert emissions.

**Q: Is this system still relevant?**
**NO.** The GEFS cron pipeline has replaced this system. The old alert-path is the Phase B production system that:
- Produces 0 trades against Kalshi data (confirmed dead Jul 31)
- Has 17/20 P0 items undone
- Is not wired into the active pipeline

**Q: What's the simplest path forward?**
**KILL.** These items are building audit infrastructure for a system that doesn't produce value. The GEFS pipeline is the only system that produces positive P&L against real Kalshi data. Every hour spent on Phase B items is an hour not spent on the GEFS pipeline.

**FIRST-PRINCIPLES VERDICT: KILL ALL 6 ITEMS.** Document the decision, archive the requirements, redirect to GEFS pipeline.

### Phase B Legacy System: Epoch Backfill Bootstrap (7 items)

| # | Item | Original Purpose |
|---|------|-----------------|
| E1 | Historical METAR acquisition utility | Backfill METAR data |
| E2 | Deterministic replay ingestion runner | Replay historical data |
| E3 | Backfill >= 90 historical days per city | Historical coverage |
| E4 | Accept reduced-resolution snapshots | Pragmatic backfill |
| E5 | Persist reconstructed settlement epochs | Store reconstructions |
| E6 | Record backfill provenance metadata | Trace data origins |
| E7 | Validate replay equivalence after backfill | Verify correctness |

### First-Principles Analysis

**Q: What is the actual problem?**
The old system needs more historical METAR data to improve its alert models.

**Q: Is this still the right approach?**
**NO.** The GEFS pipeline already has:
- 363,440 rows of GEFS ensemble data (2021-01-02 to 2026-07-30)
- 40,760 rows of ERA5 ground truth (2021-01-01 to 2026-07-31)
- 6,070 Kalshi settlements (2021-08-19 to 2026-08-01)
- 85 paper trades at 67% accuracy

The METAR-based backfill approach is solving a problem that's already been solved by the GEFS archive + ERA5 ground truth.

**Q: What's the simplest path forward?**
**KILL.** The GEFS archive already provides what the epoch backfill was trying to build — historical data for backtesting. The data is better (ensemble NWP vs METAR), more complete (2,036 dates vs partial), and already calibrated against Kalshi settlements.

**FIRST-PRINCIPLES VERDICT: KILL ALL 7 ITEMS.** The GEFS archive + ERA5 ground truth is the canonical data path. Phase B epoch backfill is dead code.

---

## First-Principles Ground Truth on the Active Pipeline

Since we're stripping assumptions, let me also apply first principles to the 6 active P1 items:

### P1.1 — Run full backtest (90-365 days)

**First-principles question:** Why 90-365 days? The cron is running every 4 hours on 7 days of data. The simplest thing: just run it on all available data.

**Verdict:** Do it. No further analysis needed. Run `--days 365 --start 2025-08-03`. The GEFS archive has 2,036 dates — use it all.

### P1.2 — Station tradability audit

**First-principles question:** Why audit after the backtest? Because the backtest is what produces the data to audit. But the backtest also produces the data to answer the question. No reason to sequence them.

**Verdict:** Build into the backtest script. The backtest should output per-station P&L, accuracy, Sharpe, and trade count. The audit is a summary of the backtest, not a separate task.

### P1.3 — Wire Edge 20 (multi_model_ensemble)

**First-principles question:** The NWP data is already backfilled (2,045 dates, 4 models). The GEFS data alone is producing 67% accuracy. Before adding complexity, **establish the baseline** — what does the current GEFS-only system look like at scale? If the full backtest shows 62%+ at 4,000+ trades, adding NWP is a secondary optimization.

**Verdict:** PARK until full backtest completes. If GEFS-alone produces 62%+ at scale, NWP is optimization, not foundation.

### P1.4 — Epoch-based Kelly schedule

**First-principles question:** The GLM 5.2 config is ready. But the Kelly formula was just fixed (Aug 2). Do we know the corrected Kelly works at all before adding epoch multipliers?

**Verdict:** ADVANCE but only after the full backtest runs with the corrected Kelly. Let the baseline establish before adding complexity.

### P1.5 — Urban heat island correction

**First-principles question:** The Gray Room identified KNYC, KLAX, KPHX, KDFW as having systematic warm bias. This is a known physical phenomenon. The fix is simple: compute per-station summer bias from GEFS-vs-actual and subtract.

**Verdict:** ADVANCE. This is a low-effort, high-impact correction. 2h to implement. Should be done alongside the full backtest.

### P1.6 — Per-station calibration curves

**First-principles question:** Replacing the heuristic confidence with empirical calibration. The Gray Room correctly identified this as necessary. But the heuristic was already replaced by ensemble fraction confidence (Aug 2). What's left?

**Verdict:** ADVANCE but defer to after P1.1. The ensemble fraction confidence is better than the heuristic. Empirical calibration is further optimization. Run the full backtest first, then calibrate.

---

## Revised P1 Priority (First-Principles Ordered)

| Order | Item | Effort | First-Principles Rationale |
|-------|------|--------|---------------------------|
| **1** | Run full backtest (all available data) | 1h | Establishes the baseline. Everything else is optimization until we know the baseline. |
| **2** | Urban heat island correction | 2h | Low effort, known physical effect, high impact on biased stations. |
| **3** | Station tradability audit | 1h | Built into backtest output. Per-station P&L + accuracy. |
| **4** | Epoch-based Kelly schedule | 6h | GLM 5.2 config ready. Wire after corrected Kelly baseline is verified. |
| **5** | Per-station calibration curves | 4h | After full backtest establishes baseline accuracy. |
| **6** | Wire Edge 20 (multi_model_ensemble) | 4h | **PARK** — establish GEFS-only baseline first. |

---

## Phase B Legacy Items: Disposition

| Item | Original | First-Principles | Action |
|------|----------|-----------------|--------|
| Structural Edge Preservation (6) | P1 | **KILL** — GEFS pipeline replaces this system | Archive requirements, redirect to GEFS |
| Epoch Backfill Bootstrap (7) | P1 | **KILL** — GEFS archive + ERA5 is canonical data path | Delete requirements, redirect to GEFS |
| **Total recovered effort:** | **13 items** | **→ 0 items** | **~40h freed** |

---

## Summary

The Gray Room Rounds 13-14 already did the first-principles work on the GEFS pipeline P1 items. The Phase B legacy P1 items (13 total) are building audit infrastructure for a dead system and should be killed.

The active P1 items need reordering: run the full backtest first, build everything else against the baseline.

**Next action:** B-mode loop to execute revised P1 items in priority order.