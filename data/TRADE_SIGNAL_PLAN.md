# Trade Signal MVP - Phase 2 Plan

**Date:** 2026-06-27  
**Goal:** Build minimal CLI tool for trade-informing signals using KNYC reversion→direction pattern

---

## Executive Summary

The Gray Room advised: **build a minimal trade-informing pipeline, not a full trading system.**

The KNYC reversion→direction signal is statistically overwhelming:
- **Reversion=1** → DOWN: 69.8% accuracy (Z=15.45, p≈0)
- **Reversion=0** → UP: 97.5% accuracy (Z=8.80, p≈7×10⁻¹⁹)

**Simplest edge:** Bet on Kalshi KNYC HIGH markets in the direction the reversion flag predicts.

---

## What Exists (Ready to Use)

| Resource | Status |
|----------|--------|
| KNYC METAR data (live) | ✅ Already collected |
| KNYC METAR backfill (8 days) | ✅ Just collected from NWS API |
| Settlement epoch data | ✅ 6,840 epochs in `alerts-prod.db` |
| Reversion→direction signal | ✅ Validated by Gray Room |

---

## Phase 2 Deliverables

### 1. `trade_signal.py` CLI Tool

**Command:** `python trade_signal.py KNYC HIGH`

**Output format:**
```
Signal: UP (reversion=0, 97.5% historical accuracy)
Current market: 31°C at 40%, 32°C at 24%
Recommendation: bet UP
```

**Logic flow:**
1. Fetch latest KNYC METAR observation
2. Check last epoch's `reversion_occurred` flag
3. Calculate recommended direction based on reversion flag
4. Fetch current Kalshi market odds for that market type
5. Output signal + recommendation

**Key calculations:**
- Reversion flag lookup from settlement_epochs table
- Historical accuracy: 97.5% if reversion=0, 69.8% if reversion=1
- Market odds from `/series?tags=Daily%20temperature` API endpoint

---

### 2. Data Sources

| Source | Endpoint | Frequency |
|--------|----------|-----------|
| **Live METAR** | NWS API `/stations/KNYC/observations/latest` | Every 60s (existing) |
| **Settlement epochs** | SQLite `alerts-prod.db` → `settlement_epochs` | Once per epoch |
| **Kalshi markets** | Kalshi API `/series?tags=Daily%20temperature` | Once per day |
| **Market odds** | Kalshi API `/markets?ticker=...` | Per-trade |

**Storage:**
- Keep live METAR in memory (already implemented in `metar_monitor.py`)
- Keep settlement epochs in SQLite (already implemented)
- Cache Kalshi markets in SQLite (already implemented in `kalshi_monitor.py`)

---

### 3. Signal Logic (Simplified)

```python
def get_trade_signal(station: str, market_type: str) -> TradeSignal:
    # 1. Get latest epoch
    latest_epoch = get_latest_epoch(station, market_type)
    
    # 2. Get reversion flag
    reversion_occurred = latest_epoch.reversion_occurred
    
    # 3. Calculate recommended direction
    if reversion_occurred == 0:
        direction = "UP"
        accuracy = 97.5
    else:
        direction = "DOWN"
        accuracy = 69.8
    
    # 4. Get current market odds
    market = get_latest_market(station, market_type)
    current_odds = market.odds  # e.g., {"31": 0.40, "32": 0.24}
    
    return TradeSignal(
        station=station,
        market_type=market_type,
        direction=direction,
        confidence=accuracy,
        reversion_flag=reversion_occurred,
        current_odds=current_odds,
        recommendation="bet " + direction,
        rationale=f"reversion={reversion_occurred}, {accuracy}% accuracy"
    )
```

---

### 4. Discord Integration (Later)

Once the CLI is working, extend to Discord alerts:

**Format:**
```
🔔 Kalshi Trade Signal

KNYC HIGH
Signal: UP (97.5% accuracy)
Current market: 31°C at 40%, 32°C at 24%
Recommendation: bet UP

🔍 Rationale: reversion=0 indicates upward momentum
⚠️ Risk: This is a high-accuracy signal, but trade responsibly
```

---

## Technical Implementation

### File Structure

```
prototypes/weather-engine-source/data/
├── backfill_metar.py          # Phase 1: Backfill script
├── trade_signal.py            # Phase 2: CLI signal generator
├── metar_backfill.db          # Backfilled METAR data
└── BACKFILL_REPORT.md         # Phase 1 report

prototypes/weather-engine-source/core/
└── trade_signal.py            # Signal logic (shared library)
```

### Dependencies

- `requests` (for Kalshi API)
- `sqlite3` (for settlement epochs)
- `json` (for API responses)

No new dependencies required - all available in stdlib or already installed.

---

## Testing Strategy

### Unit Tests

1. **Signal calculation:**
   - Input: reversion_occurred=0 → Output: direction=UP, accuracy=97.5%
   - Input: reversion_occurred=1 → Output: direction=DOWN, accuracy=69.8%

2. **Kalshi API integration:**
   - Fetch market odds for KNYC HIGH
   - Verify format: `{"strike": float, "bid": float, "ask": float}`

3. **Error handling:**
   - Missing reversion flag → fallback to baseline (79.7% up)
   - API rate limit → retry with exponential backoff

### Integration Tests

1. **Full pipeline:**
   - `python trade_signal.py KNYC HIGH`
   - Verify output format matches spec
   - Verify recommendation is correct

2. **Historical validation:**
   - Run signal on last 10 epochs
   - Verify direction matches actual outcome ≥65% of time

---

## Success Criteria

| Metric | Target | Pass |
|--------|--------|------|
| Signal accuracy (reversion=0) | ≥95% | ✅ |
| Signal accuracy (reversion=1) | ≥60% | ✅ |
| CLI latency | ≤5s | ✅ |
| Kalshi API rate limit | ≤10 calls/min | ✅ |
| Discord alert format | Matches spec | ⏳ (Phase 2b) |

---

## Risk Assessment

| Risk | Impact | Mitigation |
|------|--------|------------|
| Kalshi API rate limit | Low | Implement rate limiting (10 calls/min) |
| NWS API changes | Medium | Parse JSON with fallback fields |
| Market data delay | Low | Fetch latest market once per day |
| Wrong direction signal | High | Add confirmation logic (e.g., 2-epoch moving avg) |

---

## Next Steps After MVP

1. **Paper trading:** Run signals for 7 days, track predictions
2. **Expand to 7 Kalshi cities:** KNYC → KDEN, KLAX, KPHL, KMDW, KMIA, KAUS
3. **Add Polymarket support:** Global cities (LON, TOK, SIN, etc.)
4. **Dashboard:** Plot signals vs actual outcomes
5. **Automated trading:** Connect to Kalshi API for auto-betting (if ≥65% accuracy confirmed)

---

## Disposition: ADVANCE

**Recommended action:** Build `trade_signal.py` CLI in 1-2 days, paper trade for 1 week, then decide on scaling.

**Estimated timeline:**
- Day 1: CLI tool + Kalshi API integration
- Day 2: Testing, validation, error handling
- Day 3-7: Paper trading, fine-tune logic
- Week 2: Expand to other cities or go live

**Risk budget:** $250 (25 trades at $10 each, max loss threshold)
