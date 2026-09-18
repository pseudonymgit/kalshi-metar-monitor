# Goldilocks Architecture — Two-Entity Structure

**Author:** Gilfoyle
**Date:** 2026-08-11
**Purpose:** Document the two distinct entities sharing the "Goldilocks" name — the daily-level signal (in SignalRegistry) and the real-time microstructure lane (outside SignalRegistry). This resolves the communication gap identified in FRONTAL-COMMUNICATION-SPEC.md.

---

## 1. Overview

The original "Goldilocks" concept — detecting transient temperature spikes near Kalshi bucket boundaries — evolved into **two fundamentally different modules** that share a name but operate on different timescales with different architectures:

| Aspect | GoldilocksSignal (SignalRegistry) | Goldilocks Lane (Microstructure) |
|--------|----------------------------------|----------------------------------|
| **Code location** | `core/signals/goldilocks_signal.py` | `core/lane_goldilocks.py` |
| **Class** | `GoldilocksSignal(BaseSignal)` | `GoldilocksLane` |
| **Architecture** | Daily signal (voting member) | Intraday lane (posterior modifier) |
| **Granularity** | Daily (day-over-day high diff) | Intraday (30-90 min event-driven) |
| **Interface** | `evaluate(idx, days)` → `(dir, conf)` | `evaluate(station, obs_temp, obs_time, bucket_boundary, direction)` |
| **Registry** | In `SignalRegistry` as `'goldilocks'` | NOT in SignalRegistry (lane-only) |
| **Gray Room status** | **KILL** (R7 A2) — should be removed | **ADVANCE** (R7 A1, A4, A6) — the real edge |
| **Confidence model** | Asymmetric base (0.40 up / 0.25 down) | R7-A1 corrected: `running_max_delta < 0.3°F → transient` |
| **Fire rate** | 58.5% | ~15-20% of days (30-90 min events) |
| **Threshold** | 2.0°F day-over-day change | Transient spike detection window (~300s) |
| **Sweepable** | ✅ Yes (binary param in combinatorial sweep) | ❌ No (intraday granularity destroyed by daily sweep) |
| **Ensemble role** | Voting member (weighted by confidence) | Posterior modifier (probability-mass shift) |
| **Current status** | Still active (R7 ADVANCE not yet implemented) | Built, pipeline integration in progress |

---

## 2. Entity 1: GoldilocksSignal (Daily Signal)

### 2.1 Location
`core/signals/goldilocks_signal.py`

### 2.2 Interface
```python
class GoldilocksSignal(BaseSignal):
    def evaluate(self, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
```

### 2.3 Logic
- Compares `days[idx-1].high` vs `days[idx-2].high` (yesterday vs day before)
- Threshold: 2.0°F absolute change
- Spike UP → predicts 'down' (reversion down): base confidence 0.40
- Spike DOWN → predicts 'up' (reversion up): base confidence 0.25, multiplied by 0.85
- Operates at daily granularity using daily high/low values

### 2.4 Gray Room R7 A2 Disposition: KILL
All 4 experts concurred: this is a bad mean-reversion signal, not true Goldilocks microstructure detection. It should be removed from the signal list. **This has NOT been implemented** — the signal is still in SignalRegistry.

### 2.5 naming
Per R7 A3, this should be renamed to `MeanReversionSignal` before being killed, but the rename was never done either.

---

## 3. Entity 2: GoldilocksLane (Intraday Microstructure Lane)

### 3.1 Code Location
`core/lane_goldilocks.py` — the standalone lane module
`core/metar_monitor.py` — the real-time temperature event processing pipeline (`_process_temperature_event`, `_SIGNAL_MICROSTRUCTURE_SPIKE_TRACKER`)
`core/lane_manager_v2.py` — orchestrates Main + Goldilocks + Trajectory lane blending

### 3.2 Interface
```python
class GoldilocksLane:
    def evaluate(self, station, obs_temp, obs_time, bucket_boundary, direction) -> Tuple[bool, float, str, dict]
    def evaluate_day(self, station, date_str) -> list[EpochDetectionResult]
```

### 3.3 Logic Pipeline
1. **Load METAR observations** for current day
2. **Gate filters** (all must pass):
   - Sensor sanity (`_gate_sensor_sanity`)
   - Feed freshness (`_gate_feed_freshness`)
   - Oscillation filter (`_gate_oscillation_filter`)
   - Multi-variable confirmation (`_gate_multivariable`)
   - Market executability (`_gate_market_executability`)
3. **Check Goldilocks zone** — is temp within 1°F of a bucket boundary?
4. **DualHypothesisEngine** — classify transient (H1) vs structural (H2) per R7 A4
5. **Confidence** — `_compute_microstructure_spike_confidence()` in `metar_monitor.py` implements R7-A1 fix:
   - `running_max_delta < 0.3°F` → transient spike → base=0.50
   - `running_max_delta >= 0.3°F` → structural (set new max) → base=0.10
6. **Settlement execution gate** (R7 A6 — `core/settlement_execution_gate.py`)
7. **Return** `(should_trade, confidence, hypothesis, details)`

### 3.4 Supporting Modules
| Module | File | Purpose |
|--------|------|---------|
| DualHypothesisEngine | `core/dual_hypothesis_engine.py` | H1 transient vs H2 structural classification |
| METARQualityParser | `core/metar_qc_parser.py` | QC flag filtering (R7 A5) |
| SettlementExecutionGate | `core/settlement_execution_gate.py` | Gate before trade execution (R7 A6) |
| StationRankSelective | `core/station_rank_selective.py` | Per-station accuracy filter (R7 A8) |
| RollingCalibration | `core/rolling_calibration.py` | 30-day rolling window (R7 A9) |
| LaneManagerV2 | `core/lane_manager_v2.py` | Multi-lane orchestration |

### 3.5 Alert Pipeline
Real-time alerts are delivered via the `metar_monitor.py` pipeline:
- `_SIGNAL_MICROSTRUCTURE_SPIKE_TRACKER` — in-memory tracker for spike epochs
- `_compute_microstructure_spike_confidence()` — confidence scorer (the core R7-A1 fix)
- `_alert_integrity_evaluation_window_seconds()` — alert gate timing
- Webhook-based alert delivery via `_queue_alert_for_delivery()`

### 3.6 Gray Room Status: ADVANCE (all items)
- A1: Confidence inversion fix ✅ (inverted, running_max_delta < 0.3°F)
- A4: Dual-hypothesis engine ✅ (H1 transient vs H2 structural)
- A5: METAR QC flag parsing ✅
- A6: Settlement execution gate ✅
- A8: Station-rank-selective activation ✅
- A9: Rolling 30-day calibration ✅
- Pipeline integration: ⚠️ BROKEN (paper_trading_engine.py uses as voting signal, not posterior modifier)

---

## 4. Also Affiliated: `lane_goldilocks.py` (Archived Predecessor)

| File | Status | Notes |
|------|--------|-------|
| `core/archive/2026-08-11/lane2_goldilocks.py` | ARCHIVED | Predecessor — killed as ML signal, converted to pure deterministic |
| `core/archive/2026-08-11/goldilocks_predictive.py` | ARCHIVED | ML-based predictive features |

---

## 5. Alert Lane in metar_monitor.py

The real-time Goldilocks alert pipeline lives in `core/metar_monitor.py`:
- Line 51: `from core.alert_schema import` — alert delivery infrastructure
- Line 69: `log_near_miss_if_epoch_alert_emitted` — near-miss logging
- Line 117-196: `_compute_microstructure_spike_confidence()` — the R7 A1 fix implementation
- Line 244: `_SIGNAL_MICROSTRUCTURE_SPIKE_TRACKER` — in-memory epoch tracking dict
- Line 247: `_SIGNAL_GOLDILOCKS_EPOCH_TRACKER` — backward-compat alias
- Line 1845: `_process_temperature_event()` — main temperature event processing
- Line 2464: Cleanup of stale trackers

This pipeline is **event-driven** (fires on new METAR observation), not daily-polling. It's distinct from both the daily signal and the Goldilocks lane in that it's embedded in the METAR ingestion path rather than being a standalone module.

---

## 6. Lane Orchestration (LaneManagerV2)

`core/lane_manager_v2.py` manages three lanes:

| Lane | Component | Weight | Enabled By |
|------|-----------|--------|------------|
| Main | SignalRegistry ensemble | 1.0 (baseline) | Always |
| Goldilocks | `lane_goldilocks.py` (GoldilocksLane) | 0.15 (light informant) | Config: `goldilocks_lane_enabled` |
| Trajectory | `core/trajectory_lane.py` (TrajectoryLane) | Configurable [0.05, 0.30] | Config: `trajectory_lane_enabled` |

Note: This is NOT the same as the Gray Room R7 C3 spec (`signal_lane_registry.py` with explicit 70/20/10 capital allocation). The LaneManagerV2 is a simpler implementation that blends lane outputs rather than allocating capital per lane.

---

## 7. Current Issues

1. **GoldilocksSignal still in SignalRegistry** — R7 A2 (KILL) was never executed. The signal is still registered as `'goldilocks'` with 58.5% fire rate. If the Gray Room consensus is followed, it should be disabled or moved to DISABLED section.
2. **No naming refactor (R7 A3)** — GoldilocksSignal should be renamed to MeanReversionSignal before disabling, and the lane references should be consolidated.
3. **Lane pipeline integration broken** — `paper_trading_engine.py` treats nowcasting as a voting signal instead of posterior modifier. Same issue applies to Goldilocks lane integration.
4. **No single reference doc existed before this one** — the two-entity structure was implicit knowledge.

---

## 8. Change Log

| Date | Version | Author | Changes |
|------|---------|--------|---------|
| 2026-08-11 | 1.0 | Gilfoyle | Initial reference doc — documents two-entity Goldilocks architecture |