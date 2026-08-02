# Polymarket WhaleWatch — DB & Cross-Platform Wiring

**Date:** 2026-08-02
**Components:** `core/polymarket_whale_db.py`, `core/polymarket_kalshi_feeder.py`, `scripts/polymarket_whale_collector.py`
**Related docs:** `docs/notes/WHALEWATCH-BUILD-NOTES.md`, `docs/plans/FP-MULTI-SIGNAL-FUSION.md`, `docs/plans/GOLDILOCKS-WHALE-WATCH.md`

---

## Architecture Overview

```
Polymarket Data API                Kalshi Weather Engine
┌──────────────────────┐          ┌──────────────────────────────┐
│ data-api.polymarket  │          │ core/whale_detector.py       │
│ .com/trades          │          │ (order book anomaly)         │
│ gamma-api.polymarket │          │                              │
│ .com/markets         │          │ core/polymarket_kalshi_      │
└─────────┬────────────┘          │ feeder.py                    │
          │                       │ (cross-platform conviction)  │
          ▼                       │                              │
┌──────────────────────┐          │      ┌──────────────────┐    │
│ core/polymarket_whale│          │      │polymarket_whale  │    │
│ _db.py               │◄─────────│──────│.db               │    │
│ (SQLite persistence) │          │      └──────────────────┘    │
└──────────┬───────────┘          │                              │
           │                      │      ┌──────────────────┐    │
           ▼                      │      │ get_polymarket_  │    │
┌──────────────────────┐          │      │ conviction()     │    │
│ polymarket_signals   │          │      │ → multiplier     │    │
│ _feed table          │          │      └──────────────────┘    │
│ (station, bucket,    │          │                              │
│  multiplier, status) │          │      FP-MULTI-SIGNAL-FUSION  │
└──────────────────────┘          │      Layer 3: Kelly sizing   │
                                  │      × conviction_multiplier │
                                  └──────────────────────────────┘
```

The system has two independent whale detection engines that feed the same conviction multiplier (Layer 3 in the FP fusion doc):

| Engine | Source | What it detects |
|--------|--------|-----------------|
| **Kalshi WhaleWatch** | Kalshi order book (bid/ask depth) | 8-signal anomaly fusion from order book microstructure |
| **Polymarket WhaleWatch** | Polymarket trade data (wallet-level) | Consensus detection from whale wallet positions |

Both produce a `conviction_multiplier` that feeds into the Kelly position sizer. They are independent and additive — either can fire without the other.

---

## Module Reference

### `core/polymarket_whale_db.py` (606 lines)

SQLite persistence layer. Creates and manages `data/polymarket_whale.db` with WAL mode.

**Tables:**

| Table | Purpose | Key Fields |
|-------|---------|------------|
| `polymarket_traders` | Known whale wallets | id, wallet, displayName, tier, hitRate, pnl, roi, copyScore |
| `polymarket_markets` | Polymarket markets | id, slug, title, category, status, volume, yesPrice, noPrice |
| `polymarket_positions` | Whale positions | id, traderId, marketId, side, size, avgPrice, notional, status |
| `polymarket_consensus_signals` | Whale agreement clusters | id, marketId, category, side, agreementLevel, whaleCount, weightedEdge |
| `polymarket_signals_feed` | Cross-platform signal output | id, signalId, kalshi_station, kalshi_bucket, conviction_multiplier, signal_direction, status |

**Key functions:**

```python
# Initialize/connect
conn = get_db("data/polymarket_whale.db")

# Store live data
inserted = store_traders(conn, [trader_dict, ...])       # upsert by id
inserted = store_markets(conn, [market_dict, ...])        # upsert by id
inserted = store_consensus_signals(conn, [signal_dict])   # upsert by (marketId, side)
inserted = store_signal_feed(conn, [feed_entry_dict])     # insert with timestamp

# Query
signal = get_active_signals(conn, station="KNYC", min_whales=2)
signals = get_recent_signals(conn, hours=24)
trader = get_trader(conn, "0xabc...")
traders = get_traders_by_tier(conn, tier="whale")
markets = get_markets_by_category(conn, category="weather")
```

**Schema design notes:**
- All timestamps stored as ISO-8601 strings (matching the TypeScript project's format)
- `signals_feed.status` is PENDING (recent, not yet applied) or EXPIRED (>24h old)
- WAL mode for concurrent read/write safety
- Foreign keys enforced via application logic (SQLite ignores FK constraints by default, but the schema defines them)

---

### `core/polymarket_kalshi_feeder.py` (722 lines)

Core integration layer. Polls Polymarket data, extracts weather consensus signals, maps to Kalshi station/bucket, computes conviction multipliers.

**Endpoints used:**
- `GET https://data-api.polymarket.com/trades?limit=300` — recent trades
- `GET https://gamma-api.polymarket.com/markets?limit=80` — active markets

**Station mapping:** Built-in `US_WEATHER_STATIONS` dictionary covers 61 cities mapped to Kalshi ICAO codes (KNYC, KLAX, etc.).

**Title parsing (most critical logic):**
Polymarket market titles follow patterns like:
- "Will the temperature in New York exceed 85°F on August 15?"
- "Will the high in Chicago be at least 90°F on 2026-08-15?"
- "Does LAX record a low below 60°F tomorrow?"

Parsing extracts: city name, direction (exceed/above = UP, below = DOWN), threshold temperature, date.

**Conviction multiplier formula** (from FP-MULTI-SIGNAL-FUSION.md §5.2):

```python
def compute_conviction(signal, ensemble_direction=None):
    base = signal.agreementScore / 100  # normalize to 0-1
    
    if ensemble_direction is None or signal.direction == ensemble_direction:
        # Match: amplify conviction
        return max(1.0, min(1.5, 1.0 + base * 0.5))
    else:
        # Contradiction: reduce conviction
        return max(0.2, 1.0 - base * 0.5)
```

**Signal TTL:** 24 hours. After 24h without refresh, status → EXPIRED and multiplier → 1.0 (no effect).

**Key entry point:**

```python
def get_polymarket_conviction(station: str, bucket: int) -> Optional[float]:
    """
    Returns conviction multiplier for a Kalshi station/bucket.
    
    Args:
        station: Kalshi ICAO code (e.g. 'KNYC')
        bucket: Temperature bucket (e.g. 85)
    
    Returns:
        float 1.0-1.5 if matching active signal exists
        float 0.2-1.0 if contradicting active signal exists
        None if no active signal
    """
```

---

### `scripts/polymarket_whale_collector.py` (246 lines)

Daily collection cron. Intended to be run once per day to persist Polymarket whale data.

```
python3 scripts/polymarket_whale_collector.py              # normal run
python3 scripts/polymarket_whale_collector.py --dry-run    # print summary, no DB writes
python3 scripts/polymarket_whale_collector.py --json       # JSON output to stdout
```

**Output format (dry-run/JSON mode):**
```json
{
  "timestamp": "2026-08-02T00:00:00Z",
  "traders_found": 85,
  "markets_weather": 12,
  "consensus_signals": 5,
  "active_feed_entries": 3,
  "conviction_range": [1.0, 1.3]
}
```

---

## Integration Points

### Reading the feed from the weather engine

```python
from core.polymarket_kalshi_feeder import get_polymarket_conviction

# Later, in the Kelly position sizer:
conviction = get_polymarket_conviction("KNYC", 85)
if conviction:
    kelly_fraction *= conviction
```

This is the only integration point. The feeder handles all parsing, API calls, and DB persistence internally.

### Coordination with Kalshi WhaleWatch

Both systems produce `conviction_multiplier` values but at different layers:
- **Kalshi WhaleWatch** (order book): Intraday, 30s-30min latency, DETECTED→HIGH_CONVICTION tiers
- **Polymarket WhaleWatch**: Daily, 1-6h latency, continuous score (1.0-1.5)

If both fire simultaneously, the maximum multiplier applies (conservative: don't double-count conviction):

```python
kw = kalshi_whale_multiplier     # 1.0-1.5
pm = polymarket_conviction       # 1.0-1.5 or 0.2-1.0
final = max(kw, pm) if pm else kw  # choose the more confident, or use Kalshi alone
```

---

## Constraints & Known Limitations

1. **Polymarket weather liquidity is thin** — most Kalshi weather markets have no Polymarket equivalent. The feeder returns `None` for most station/bucket combos.
2. **Title parsing is heuristic** — Polymarket titles vary widely. Parse failures log a warning and skip silently.
3. **API rate limits** — Polymarket data API allows ~10 req/min. The feeder respects this with a 6s delay between calls.
4. **No real-time feed** — Currently polling-based (daily collection cron). WebSocket support is future work.
5. **Wallet pseudonymity** — Polymarket doesn't expose real trader identities. "Whale" = wallet with consistent large positions, not a known entity.
6. **Not wired into trading** — Pipeline is data-only. Integration with the Kelly sizer requires the trading loop to call `get_polymarket_conviction()`.