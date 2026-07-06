# KILLED: Late-Day Momentum Signal (Signal 4 / LDM)

**Date killed:** 2026-07-06  
**Reason:** 48.31% directional accuracy in comprehensive split backtest — below coin-flip (50%). Dead weight in the ensemble. Removal of LDM was tested and confirmed: no accuracy loss, slight improvement.

**Original implementation:** `core/late_day_momentum_hourly.py` (threshold=1.7°F/hr, 17:00–22:00 UTC window)  
**R4-1.4 tried:** DTR-scaled regime-adaptive threshold on this signal — still 48.31%, still dead.  
**Do not re-add to the ensemble.** Ignore suggestions to include it unless the approach is fundamentally different (e.g., using NWP forecast data instead of METAR observations, or a completely different signal construction).

The file `core/late_day_momentum_hourly.py` is retained for reference but is NOT imported or called by any active signal generation or ensemble code.

---

# Signal Audit History

| Signal | Last Accuracy | Status | Date |
|--------|--------------|--------|------|
| Regime (DTR-scaled) | 70.00% | ✅ Active | 2026-07-06 |
| Calendar climatology | 68.91% | ✅ Active | 2026-07-06 |
| Goldilocks (up/down split) | 68.01% | ✅ Active | 2026-07-06 |
| Gaussian (48d) | 66.91% | ✅ Active | 2026-07-06 |
| Reversion | 64.00% | ✅ Active | 2026-07-06 |
| Gaussian v2 (30d) | 64.00% | ✅ Active | 2026-07-06 |
| Pressure | 58.90% | ✅ Active (critical ensemble member — removal drops accuracy 2.79%) | 2026-07-06 |
| Late-day momentum | 48.31% | ❌ KILLED (below chance) | 2026-07-06 |
