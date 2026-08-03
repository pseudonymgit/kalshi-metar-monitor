# Gray Room Session — Panel Synthesis & Priority Table

**Date:** 2026-08-03
**Experts:** E1 (Bayesian), E2 (Fusion), E3 (Microstructure), E4 (Pattern), E5 (Implementation), E6 (Meteorology), E7 (Adversarial)
**Total findings:** 66 — 57 ADVANCE, 7 PARK, 2 KILL

---

## Priority Table

| Order | Phase | Item | Effort | Disposition | Blocked On |
|-------|-------|------|--------|-------------|------------|
| **P0** | **Validation** | **CLI settlement verification** — verify 66.2% baseline against NWS CLI data. Without this, everything is built on potentially inflated metrics. | 1-2 days | ADVANCE | — |
| **P1** | **Goldilocks** | Extract Goldilocks lane from metar_monitor.py. Sensor self-heating model. `instant_cross_revert` detection. Separate product pathway. | 2-3 days | ADVANCE | P0 (CLI validation) |
| **P2** | **Cascade v1** | Discard flat LLOP in signal_fusion.py. Adopt E1's uncertainty-weighted cascade. Beta-binomial Layer 1 with moment-matched posteriors. fusion_logic.py has 80% built. | 2-3 weeks | ADVANCE | P0 (CLI validation) |
| **P3** | **82-Member** | Pool-of-pools approach. Measure ρ_within_GEFS, ρ_within_ECMWF, ρ_cross from existing archive. If ρ_cross < 0.85, add ECMWF as second pool. | 2-4 days + 1 week measurement | ADVANCE | ECMWF backfill complete, P0 |
| **P4** | **Trajectory** | Research spike: DTW epoch-sequence matching. 5 features (T, Td, P, WS, WD). Climate-zone pooling. 30-day shadow before wiring. | 8h + 30-day shadow | ADVANCE | P0, P2 |
| **PARK** | **Deterministic models** | Adding GFS/IFS/ICON/GEM as ensemble members. E2's ERR-4 was rejected by panel — E7.5 showed forecast aggregation agreement score overlaps with ensemble fraction. | — | PARK | Need ρ measurement first |
| **PARK** | **WhaleWatch conviction** | Temporal decay on conviction multiplier. Bayes-factor lookup table for contradiction. | — | PARK | Shadow-mode data needed |
| **KILL** | **Agreement boost** | E7.5 — the +0.05 forecast aggregation boost is redundant with ensemble fraction. | — | KILL | — |
| **KILL** | **Full Bayesian trajectory** | I5 — Bayesian trajectory modeling is overkill at this data scale. | — | KILL | — |

---

## Key Disagreements Resolved

### Disagreement 1: Goldilocks Viability
- **Resolution:** ADVANCE as separate product pathway. The "fleeting tick" is real (sensor self-heating, not temperature). The old alert-path system it was designed for is dead, but the GEFS pipeline can use it as a microstructure overlay at f024. Requires: sensor self-heating validation, precision > 0.70 / recall > 0.50 on backtest.

### Disagreement 2: Cascade Architecture
- **Resolution:** ADVANCE E1's uncertainty-weighted cascade (Beta-binomial Layer 1, moment-matched posteriors). Discard flat LLOP. Build in Phase 2 (after CLI validation). E5 confirmed fusion_logic.py has 80% of the math.

### Disagreement 3: 82-Member Value
- **Resolution:** ADVANCE pool-of-pools. Measure ρ before pooling. At ρ=0.85, effective members = 1-4, not 82. But ECMWF still adds fault tolerance. E2's deterministic model inclusion (add 4 as members) was REJECTED — redundant with ensemble fraction.

### Disagreement 4: Trajectory Lane
- **Resolution:** ADVANCE as research spike (8h + 30-day shadow). E5's 4-6 week estimate was based on the wrong system (p3_trajectory_tracer, which matches settlement epochs, not weather). E4's 17h estimate using DTW on climate-zone pooled data is correct. Don't wire into trade selection until shadow passes criteria.

### Disagreement 5: Priority Order
- **Resolution:** P0 = CLI validation. Everything else is conditional on that. The structural truth: the 66.2% baseline has a ±10pp 95% CI on 85 trades and is unverified against NWS CLI settlement data.

---

## Disposition Summary

| Category | Total | ADVANCE | PARK | KILL |
|----------|-------|---------|------|------|
| Errors | 14 | 10 | 3 | 1 |
| Ideas | 18 | 14 | 2 | 2 |
| Specs | 22 | 22 | 0 | 0 |
| Elephants | 12 | 11 | 1 | 0 |
| **Total** | **66** | **57** | **6** | **3** |

---

## What to Do Next

| Order | Item | Who | Effort |
|-------|------|-----|--------|
| 1 | CLI settlement verification | Gilfoyle | 1-2 days |
| 2 | Goldilocks lane extraction | Gilfoyle | 2-3 days |
| 3 | Cascade v1 (uncertainty-weighted) | Gilfoyle | 2-3 weeks |
| 4 | 82-member ρ measurement | Script | 2-4 days |
| 5 | Trajectory research spike | Gilfoyle | 8h + 30d shadow |