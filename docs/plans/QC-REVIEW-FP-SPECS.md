# QC Review: First Principles Specs Consistency Check

**Date:** 2026-08-10
**Reviewer:** Gilfoyle Subagent
**Context:** Design discussion from `memory/2026-08-10-sweep-fix-specs-design-discussion.md`

## Executive Summary

Reviewed all 7 FP spec files against the new sequential narrowing framework. Found **major inconsistencies** in 2 specs (FP-WEIGHTED-ENSEMBLE-SPEC.md and FP-TRAJECTORY-MULTI-SIGNAL-SPEC.md) that require complete rewrite. Other specs are generally aligned but need minor updates.

## Individual Spec Reviews

### FP-WEIGHTED-ENSEMBLE-SPEC.md
**Status:** MAJOR MISALIGNMENT
**Issue:** Misinterprets "+0.25" as quarter-Kelly cap and variance normalization
**Design Correction:** The "+0.25" is about **agreement consensus boost** — base probability + increase when agreement threshold met
**Required Changes:**
- Remove quarter-Kelly interpretation (kelly_fraction=0.25 is correct but separate)
- Remove variance normalization interpretation (0.25 as floor)
- Focus on consensus bonus mechanism for 4-signal ensemble
- Recalibrate boost value (was tuned for 82-member, now 4-signal)
- Update agreement threshold semantics (N/4 independent signals)

### FP-TRAJECTORY-MULTI-SIGNAL-SPEC.md
**Status:** MAJOR MISALIGNMENT
**Issue:** Designed for market microstructure, not meteorological pattern matching
**Design Correction:** Trajectory lane is now **sequential narrowing live probability tracker**
**Required Changes:**
- Complete rewrite as live probability tracker
- Integrate market price (edge = P - price)
- Integrate WhaleWatch conviction multiplier
- Integrate +0.25 consensus bonus
- Focus on range narrowing over day (not single prediction)
- Build as `core/live_probability_tracker.py`

### FP-WALK-FORWARD-2-BUCKET-SPEC.md
**Status:** ALIGNED
**Notes:** Correctly identifies DJF/JJA regime differences and 2-bucket sizing
**Minor Updates:** Add reference to regime-switching HMM for dynamic confidence modulation

### FP-DEAD-SIGNALS-SPEC.md
**Status:** ALIGNED
**Notes:** Correct categorization of dead signals
**Minor Updates:** Add regime signal as P1 priority (from design discussion)

### FP-FORECAST-DISAGREEMENT-SPEC.md
**Status:** ALIGNED
**Notes:** Station normalization approach correct
**Minor Updates:** None needed

### FP-GOLDILOCKS-LANE-SPEC.md
**Status:** ALIGNED
**Notes:** Correctly identifies goldilocks as meta-lane
**Minor Updates:** Add pressure_tendency confirmation (from design discussion)

### FP-OPTIONS-EXPLORATION-SPEC.md
**Status:** ALIGNED
**Notes:** Correct priority ranking
**Minor Updates:** Regime-HMM is now P0 (from design discussion)

## Critical Action Items

1. **Rewrite FP-WEIGHTED-ENSEMBLE-SPEC.md** — Remove quarter-Kelly misinterpretation, focus on consensus boost
2. **Rewrite FP-TRAJECTORY-MULTI-SIGNAL-SPEC.md** — Complete redesign as sequential narrowing framework
3. **Update all specs** with references to new sequential narrowing architecture
4. **Create FP-SYSTEM-ARCHITECTURE.md** — Comprehensive three-layer cascade specification

## Design Consistency Matrix

| Spec | Sequential Narrowing | Market Price Integration | Consensus Boost | Regime Awareness |
|------|:-------------------:|:------------------------:|:---------------:|:----------------:|
| FP-WEIGHTED-ENSEMBLE | ❌ | ❌ | ❌ (misinterpreted) | ❌ |
| FP-TRAJECTORY | ❌ | ❌ | ❌ | ❌ |
| FP-WALK-FORWARD | ✅ | ❌ | ❌ | ✅ |
| FP-DEAD-SIGNALS | N/A | N/A | N/A | N/A |
| FP-FORECAST-DISAGREEMENT | N/A | N/A | N/A | N/A |
| FP-GOLDILOCKS | ✅ | ❌ | ❌ | ✅ |
| FP-OPTIONS | ✅ | ❌ | ❌ | ✅ |

## Next Steps

1. Create comprehensive FP-SYSTEM-ARCHITECTURE.md
2. Rewrite misaligned specs
3. Update all specs with cross-references
4. Execute Items 1-3 with Design→QC→Build→QC→Confirm loop