# KILLED: Late-Day Momentum Signal (Signal 4 / LDM)

**Date killed:** 2026-07-06  
**Reason:** 48.31% directional accuracy in comprehensive split backtest — below coin-flip (50%). Dead weight in the ensemble. Removal of LDM was tested and confirmed: no accuracy loss, slight improvement.

**Original implementation:** `core/late_day_momentum_hourly.py` (threshold=1.7°F/hr, 17:00–22:00 UTC window)  
**R4-1.4 tried:** DTR-scaled regime-adaptive threshold on this signal — still 48.31%, still dead.  
**Do not re-add to the ensemble.** Ignore suggestions to include it unless the approach is fundamentally different (e.g., using NWP forecast data instead of METAR observations, or a completely different signal construction).

The file `core/late_day_momentum_hourly.py` is retained for reference but is NOT imported or called by any active signal generation or ensemble code.

---

# NEW SIGNALS (Phase 10-14)

## SpikeReversionSignal (formerly GoldilocksSignal)

**Introduced:** Phase 10 (2026-07-24)
**Origin:** Renamed from GoldilocksSignal after fixing confidence inversion

### What changed
- Old `GoldilocksSignal` had inverted confidence: `is_daily_high` was treated as a mean-reversion signal when it actually indicated a transient spike
- Fixed by renaming semantics to `SpikeReversionSignal`, where confidence correctly maps to `is_transient_spike`
- The underlying detection logic is preserved — only the interpretation and naming were corrected

### Status
✅ Active — core ensemble member

---

## FrontalPassageIntradaySignal

**Introduced:** Phase 10 (2026-07-24)
**Origin:** Gray Room Round 5 A16, Round 7 C10

### Detection conditions (3-condition)
1. Rapid temperature change ≥ threshold (per-hour delta)
2. Pressure change correlated with temperature change
3. Wind direction shift consistent with frontal passage

### Status
✅ Active — shadow mode, contributing to ensemble

---

## MicrostructureSpikeDetector

**Introduced:** Phase 10 (2026-07-24)
**Origin:** Refactored from mean-reversion component of old GoldilocksSignal

### Purpose
Real-time detection of microstructure noise spikes — transient extreme observations that do not represent the underlying temperature regime. Operates on sub-minute timescales.

### Relationship to SpikeReversionSignal
- `SpikeReversionSignal`: settlement-aware, detects meaningful spike-then-reversion structural events
- `MicrostructureSpikeDetector`: real-time, detects transient noise spikes during observation ingest

### Status
✅ Active — real-time detection path

---

# DEAD SIGNALS REMOVED (as of Phase 9, 2026-07-23)

| Signal | Removal Date | Reason |
|--------|-------------|--------|
| `persistence` | 2026-07-23 | Dead — no predictive value, always lagging |
| `metar_dtdt` | 2026-07-23 | Dead — derived attribute with no directional accuracy |
| `pressure_tendency` | 2026-07-23 | Dead — unstable, unreliable signal |
| `seasonal_regime` | 2026-07-23 | Dead — interface mismatch (returns regime names, not predictions); not fixable within current architecture |
| `esdr` | 2026-07-23 | Dead — never had accuracy baseline, removed as part of signal cleanup (Phase 9 C4) |
| `nwp_direct` | 2026-07-23 | Dead — NWP was not used directly in deterministic pipeline; removed for clarity |

None of these signals should be re-added to the ensemble. Their files remain in the repo for reference only but are NOT imported or called.

---

# UPDATED: KILLED: Late-Day Momentum Signal (Signal 4 / LDM)

---

# Signal Audit History (UPDATED 2026-07-25)

| Signal | Last Accuracy | Status | Date | Notes |
|--------|--------------|--------|------|-------|
| Regime (DTR-scaled) | 70.00% | ✅ Active | 2026-07-06 | |
| Calendar climatology | 68.91% | ✅ Active | 2026-07-06 | |
| SpikeReversionSignal | 68.01% | ✅ Active | 2026-07-24 | Renamed from GoldilocksSignal, confidence fixed |
| Gaussian (48d) | 66.91% | ✅ Active | 2026-07-06 | |
| Reversion | 64.00% | ✅ Active | 2026-07-06 | Legacy reversion signal |
| Gaussian v2 (30d) | 64.00% | ✅ Active | 2026-07-06 | |
| Pressure | 58.90% | ✅ Active | 2026-07-06 | Critical ensemble member |
| FrontalPassageIntradaySignal | — | ✅ Active (shadow) | 2026-07-24 | 3-condition detection, no accuracy baseline yet |
| MicrostructureSpikeDetector | — | ✅ Active (real-time) | 2026-07-24 | Real-time noise detection path |
| Late-day momentum | 48.31% | ❌ KILLED | 2026-07-06 | Below chance |
| persistence | — | ❌ KILLED | 2026-07-23 | Dead — always lagging, Phase 9 C4 |
| metar_dtdt | — | ❌ KILLED | 2026-07-23 | Dead — no directional accuracy, Phase 9 C4 |
| pressure_tendency | — | ❌ KILLED | 2026-07-23 | Dead — unstable, Phase 9 C4 |
| seasonal_regime | — | ❌ KILLED | 2026-07-23 | Interface mismatch, Phase 9 C7 |
| esdr | — | ❌ KILLED | 2026-07-23 | No baseline, Phase 9 C4 |
| nwp_direct | — | ❌ KILLED | 2026-07-23 | Not part of deterministic pipeline, Phase 9 C4 |
