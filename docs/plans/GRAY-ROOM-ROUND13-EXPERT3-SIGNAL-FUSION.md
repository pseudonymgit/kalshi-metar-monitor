# Gray Room Round 13 Expert 3: Signal Fusion Architect — Intraday Signal Blending

**Status:** COMPLETE (output truncated at ~6K tokens — see raw subagent output for full)

## Key Recommendations

1. **Signal hierarchy:** GEFS ensemble fraction is the PRIMARY signal. Intraday signals (frontal, dewpoint, spatial coherence) are MODIFIERS, not co-equal voting members. Only GEFS fraction + 1-2 verified NWP signals should enter the LLOP.

2. **Temporal Fusion Layer:** Add a freshness weight to each signal before fusion — time-decay signal inputs, not just weights. Half-lives: frontal 2h, METAR trend 1.5h, GEFS 6h. Expire stale signals.

3. **Spatial coherence vs adaptive threshold:** COMPLEMENTARY, not redundant. Adaptive threshold filters pre-fusion (signal gating). Spatial coherence modulates post-fusion (conviction adjustment). Add monitoring to detect double-penalty (both suppressing the same trade).

4. **Signal independence test:** Frontal passage nowcast likely adds independent information at short timescales (1-3h) that GEFS can't resolve with its 4-hour refresh. Test via correlation analysis: compute R² between frontal score and GEFS member spread — if R² < 0.4, signal is independent.

5. **Architecture:** Bayesian cascade > voting ensemble. Each signal should update a posterior probability sequentially as it fires, with temporal freshness weighting. Current voting treats all signals as simultaneous — incorrect for async intraday events.