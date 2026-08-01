# WhaleWatch — Status Assessment

**Review Date:** 2026-08-01  
**Branch:** `bmode-whalewatch` → merged to `main`  
**Commit:** `0d4c4f8` — "WhaleWatch P0 — Order Book Microstructure Signal Detection"  
**Commit:** `eaa44dd` — merge to main

---

## What Was Built

### Core Modules (6 files)

| File | Purpose |
|---|---|
| `core/order_book_baseline.py` | Per-station, per-bucket, per-hour-of-day rolling baseline for order book metrics (bid/ask size, ratio, spread, volume). Computes short-term (10 min), diurnal, and long-window (1 trading day) baselines. |
| `core/order_book_collector.py` | Polls Kalshi public API (`GET /markets/{ticker}` and `GET /markets/{ticker}/orderbook`) at 30s intervals. Persists snapshots to SQLite. Handles rate limiting (10 req/60s). |
| `core/whale_detector.py` | Multi-signal anomaly detection fusing 8 signals (S1–S8): bid-side concentration Z-score >3.0, bid/ask ratio spike >95th percentile, volume acceleration >5× median, spread compression ≤1¢, stepwise accumulation over 3+ cycles, price gap discontinuity >5¢, OI drift >2σ, depth imbalance >3:1. Confirmation latch: 1 cycle → SUSPECTED, 2 consecutive → DETECTED (tradeable), 3+ with increasing strength → HIGH_CONVICTION. |
| `core/whale_signal_feeder.py` | Entry/exit logic. Entry: whale confirmed 2+ cycles, YES price 5–95¢, station volume >$500, no contradictory whale, >2h to settlement. Position sizing: 5% of 24h volume, Quarter-Kelly, max $500/signal. Exits: whale stops accumulating, temp confirms/contradicts bucket, reversal detected, max 6h hold, 50% stop-loss. |
| `core/whale_goldilocks_fusion.py` | Integrates WhaleWatch anomalies into Goldilocks Predictive Model (confidence boost +15–30%) and core GEFS ensemble (modulation +0.5–2.0pp). |
| `core/whale_watch_db.py` | SQLite schema + CRUD: `order_book_snap` (raw snapshots), `station_baseline` (rolling stats), `anomaly_events` (detections), `signal_journal` (entry/exit decisions). |

### Design Document

`docs/plans/GOLDILOCKS-WHALE-WATCH.md` (834 lines) — comprehensive design covering:

- Kalshi API endpoints & polling strategy (30s intervals, 10 req/60s rate limit)
- 8-signal anomaly detection algorithm with per-station normalization
- Goldilocks Predictive Model integration
- Core GEFS ensemble integration
- Estimated P&L contribution and implementation effort

---

## What WhaleWatch Detects

**Type:** Order book microstructure anomalies for Kalshi temperature markets.

**Target:** Informed traders ("whales") accumulating YES (or NO) contracts on specific temperature buckets before public METAR confirms the temperature. The whale has faster/better temperature data and reveals their conviction through order book placement.

**Detection method:** Aggregate signals only — Kalshi exposes no trader-level data. Uses 8 fused signals from public bid/ask depth:

- **S1** (Bid-side concentration, score 2.0): Strongest signal — abnormal accumulation at best bid
- **S2** (Bid/Ask ratio spike, score 1.5): Order book asymmetry
- **S3** (Volume acceleration, score 0.8): Sudden volume surge
- **S4** (Spread compression, score 1.5): Urgency signal — tight spread + large bid
- **S5** (Stepwise accumulation, score 1.0): Pattern recognition over 3+ cycles
- **S6** (Price gap discontinuity, score 0.5): Price level gaps >5¢
- **S7** (Open interest drift, score 0.5): OI accumulation
- **S8** (Depth imbalance, score 0.5): YES:NO book depth >3:1

**Fusion:** anomaly_score = sum(active_signal_scores) / 8. Score ≥2.0 → DETECTED, ≥1.0 → SUSPECTED.

---

## Confirmation Window

- **Polling interval:** 30 seconds per station-bucket combo
- **SUSPECTED:** After 1 anomalous cycle (30s)
- **DETECTED:** After 2 consecutive anomalous cycles (60s) — tradeable
- **HIGH_CONVICTION:** 3+ cycles with increasing strength (90s+)
- **Exit confirmation:** 3 cycles (90s) below threshold before closing

---

## Signals Fused

WhaleWatch anomalies are fused into two downstream models:

1. **Goldilocks Predictive Model:** `gpm_probability * (1.0 + 0.3 * whale_strength)` — 15-30% confidence boost on Goldilocks spike predictions for the specific bucket

2. **Core GEFS Ensemble:** `ensemble_probability +/- 0.5-2.0pp modulation` — shifts ensemble probability toward the whale-accumulated bucket to account for superior data the whale may have

---

## What's Needed for P1

Based on the design doc and code review, the following gaps exist from the P0 implementation:

1. **Market ticker discovery automation** — The collector needs to discover which KNYC/KLAX/etc. bucket markets are currently open (currently hardcoded). Should poll `GET /markets?series_ticker=KXHIGHNYC&status=open` periodically.

2. **Full orderbook depth parsing** — `core/order_book_collector.py` has the endpoint call but the `/orderbook` response isn't fully integrated into signals. Current code only uses top-of-book. Full depth would improve S6 (bucket gradient discontinuity) detection.

3. **WhaleWatch DB initialization cron** — No cron job exists yet to start the collector. Needs a cron entry (e.g., every 30s or heartbeat-driven) to begin populating order_book_snap.

4. **Goldilocks fusion integration test** — The `whale_goldilocks_fusion.py` module exists but needs a test harness confirming it plugs into the existing Goldilocks pipeline.

5. **Signal journal persistence review** — The `signal_journal` table handles write operations but the feeder's trade decision persistence to the paper trading engine needs verification.

6. **Historical baseline warmup** — The rolling baselines need ~1 trading day of data before anomalies can be detected. P1 should include a fast historical snapshot backfill from Kalshi's open markets to accelerate warmup.

7. **P&L attribution** — The `signal_journal` needs a P&L attribution column per signal to measure which signals (S1–S8) contribute most to performance.

---

## Files

| File | Lines | Status |
|---|---|---|
| `core/order_book_baseline.py` | ~180 | Complete |
| `core/order_book_collector.py` | ~380 | Complete (needs ticker discovery) |
| `core/whale_detector.py` | ~354 | Complete |
| `core/whale_signal_feeder.py` | ~316 | Complete |
| `core/whale_goldilocks_fusion.py` | ~191 | Complete (needs integration test) |
| `core/whale_watch_db.py` | ~412 | Complete |
| `docs/plans/GOLDILOCKS-WHALE-WATCH.md` | 834 | Complete |

---

## Risk Assessment

| Risk | Severity | Mitigation |
|---|---|---|
| Kalshi rate limiting (10 req/60s) | Medium | Polling at 30s, one station per cycle burst |
| Data free but API may change | Low | Order book data is public; can switch to alternative feeds |
| Whale detection false positives in thin markets | Medium | Volume_gate (min $500/24h) filters thin markets |
| Latency (30s poll) means we always follow the whale | Low | Not trying to beat whales to trades — follow pattern |
| No trader-level data limits signal resolution | Low | Aggregate patterns are sufficient for statistical edge |

---

*Assessment prepared during bmode-whalewatch integration review on 2026-08-01.*