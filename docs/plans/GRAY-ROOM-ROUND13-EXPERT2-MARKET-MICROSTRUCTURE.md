# Gray Room Round 13 Expert 2: Market Microstructure — Intraday Trading Timing

**Status:** COMPLETE
**Output:** See raw subagent output above

## Key Recommendations

1. **Enter during Post-12Z window (08:00-09:30 ET)** — best information advantage. GEFS 12Z is the first fresh ensemble of the US trading day.
2. **Hold to settlement as default.** Only exit if 12Z GEFS reversal signal ≥60% confidence AND spatial coherence independently corroborates.
3. **Enter full position at once, don't scale.** No information gain between 12Z and 18Z. Scaling adds 1.5-3¢ cumulative spread cost.
4. **Cost of mid-day re-evaluation: 6-10¢ round-trip.** Only re-evaluate when signal materially changed.
5. **Time-dependent thresholds:** higher confidence (≥60%) for entries, ≥60% corroborated reversal for exits. Mid-day (09:30-14:00 ET) is best for adjustments — tightest spreads at 2-4¢.