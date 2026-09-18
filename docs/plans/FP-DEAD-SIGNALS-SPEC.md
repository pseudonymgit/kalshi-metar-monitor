# FP-DEAD-SIGNALS-SPEC.md

## Item 4: Fix Dead Signals — Three Categories

### Problem Statement
Based on `docs/weather-engine/NON_FIRING_SIGNALS_DISPOSITION.md`, 25 signals don't fire:
- **7 signals** need data sources
- **1 signal** has easy bug fix
- **10 signals** actually don't work and need algorithm changes

### Proposed Solution

#### 4a: Needs Data Source (7 signals)

| Signal | Data Source | Build Plan | Effort | Priority |
|--------|-------------|------------|--------|----------|
| ai_composite | ai_forecasts table | Build collection pipeline | 16h | P3 |
| esdr | member_index column | Query rewrite + backfill | 8h | P2 |
| metar_nowcast | Wrong DB path | Easy path fix | 2h | P1 |
| hrrr_bias_corrected | Live HRRR API | API integration | 12h | P3 |
| temperature_advection | Live GFS API | API integration | 12h | P3 |
| nwp_direct | Station context | Schema addition | 6h | P2 |
| nwp_dtdt_fusion | NWP delta data | Data pipeline | 10h | P2 |

**Backfill vs Operational**: metar_nowcast, esdr, nwp_direct worth backfilling; others operational-only

#### 4b: Easy Bug Fix (1 signal)

**frontal_passage_nowcast** — evaluate() signature mismatch (takes `context: dict` instead of `idx, days`)

**Spec**: 10-line wrapper fix:
```python
def evaluate_fixed(idx, days):
    return frontal_passage_nowcast.evaluate({'idx': idx, 'days': days})
```

**Effort**: 1 hour
**Priority**: P0 (trivial fix)

#### 4c: Actually Doesn't Work (10 signals)

| Signal | Fix Approach | Effort | Priority | Worth Fixing? |
|--------|--------------|--------|----------|---------------|
| cloud_cover_index | Algorithm rewrite | 8h | P3 | Maybe |
| cross_model_divergence | Data schema change | 6h | P3 | No |
| eighty_two_member_ensemble | Already replaced | 0h | - | No |
| feels_like_delta | New data source | 8h | P3 | Maybe |
| fogr_reversion | Algorithm rewrite | 8h | P2 | Yes |
| intraday_metar_confirmation | Data schema change | 6h | P2 | Yes |
| metar_dtdt | Algorithm rewrite | 8h | P3 | Maybe |
| pressure_tendency | Algorithm fix | 6h | P2 | Yes |
| regime | Algorithm rewrite | 10h | P1 | Yes |
| volume_momentum | Kill signal | 0h | - | No |
| settlement_arbitrage | Kill signal | 0h | - | No |

**Priority Fixes**: regime (P1), fogr_reversion (P2), pressure_tendency (P2), intraday_metar_confirmation (P2)

### Data Needed
- Source data availability assessment for each signal
- Historical performance metrics for algorithm validation

### Code Needed
- Data pipeline modules for missing sources
- Algorithm rewrites for non-working signals
- Signal registry updates

### Build Effort
- **Data sources**: 66 hours total
- **Bug fixes**: 1 hour
- **Algorithm rewrites**: 60 hours
- **Total**: 127 hours (15.9 days)

### Risk Level
**HIGH** — Data pipeline work has integration risks

### Priority
**Mixed** — P0 for trivial fix, P1-P3 for others

### Dependencies
- Data source availability confirmation
- Performance validation for algorithm changes

### Improvement Suggestion
**Focus on regime signal rewrite** — The regime signal has highest potential impact (P1). Fixing regime detection should improve accuracy by +5-10pp by better handling seasonal performance differences, especially recovering JJA performance from 47.2% to 55%+.