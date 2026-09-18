# Lane vs Signal — Architecture Reference

**Purpose:** Define what makes a module a Signal versus a Lane, and provide a single source of truth for where each lives.

**Source authority:** Gray Room Round 13 Nowcasting→Trajectory Integration (2026-08-08), R7 Synthesis (three-lane architecture C3)

---

## 1. Definitions

### Signal

A module that:
- Inherits from `BaseSignal`
- Implements `evaluate(idx: int, days: list) -> Tuple[Optional[str], float]`
- Returns `(direction, confidence)` — direction is "up"/"down"/None
- Registered in `SignalRegistry` as a named key
- Participates in **ensemble voting** — each signal casts a weighted vote
- Sweepable — tested as binary 0/1 parameter in combinatorial sweep
- Operates at **daily granularity** (called once per day per station)
- Contributes directly to position sizing via vote aggregation

**Rule:** If it lives in `SignalRegistry.signals`, it's a signal.

### Lane

A module that:
- Lives **outside** `SignalRegistry` — never registered, never swept
- Does NOT implement `evaluate(idx, days)` — if it inherits BaseSignal, `evaluate()` returns `(None, 0.0)` intentionally
- Operates at **intraday / event-driven** granularity
- Does NOT participate in ensemble voting
- Modifies the **posterior probability distribution** AFTER the ensemble vote
- Applies as **multiplicative probability-mass update**: `P_post(bucket) ∝ P_prior(bucket) × (1 + shift[bucket])`
- Not sweepable — sweep's daily granularity destroys intraday edge
- Has its own module with custom interface (e.g., `NowcastingLane.get_prob_shift()`)

**Rule:** If it's documented as "LANE ONLY" in `core/signals/__init__.py` NOT_FIRING section, it's a lane.

---

## 2. Comparison Table

| Property | Signal | Lane |
|----------|--------|------|
| Base class | `BaseSignal` | Any (no constraint) |
| Interface | `evaluate(idx, days)` | Custom (event-driven) |
| Registry | `SignalRegistry` (dict key) | None (self-contained module) |
| Output | `(direction, confidence)` | Probability-mass shift dict |
| Ensemble vote | ✅ Yes — weighted in aggregation | ❌ No — applied after vote |
| Sweepable | ✅ Yes — binary 0/1 param | ❌ No — daily sweep destroys edge |
| Granularity | Daily (1 call/day/station) | Intraday / event-driven |
| Confidence | Scalar 0.0–1.0 | Vector (shift per bucket) |
| Fire condition | Most days (varies) | Event-based (15-20% of days) |
| Pipeline position | Signal loop (pre-vote) | Posterior modifier (post-vote) |
| Sizing impact | Direct (via vote weight) | Indirect (via modified posterior → EV → Kelly) |

---

## 3. Registry — What's a Signal vs What's a Lane

### Signals (13 active in `SignalRegistry`)

| Key | Class | Fire Rate | Notes |
|-----|-------|-----------|-------|
| `gaussian` | `GaussianSignal` | 44.1% | Core forecasting |
| `gaussian_v2` | `GaussianV2Signal` | 69.5% | Core forecasting |
| `pressure_delta` | `PressureDeltaSignal` | 57.8% | Core forecasting |
| `forecast_disagreement` | `ForecastDisagreementSignal` | 41.6% | Core forecasting |
| `calendar_climatology` | `CalendarClimatologySignal` | 24.2% | Core forecasting |
| `corrected_pressure_delta` | `CorrectedPressureDeltaSignal` | 58.6% | Orthogonal |
| `seasonal_regime` | `SeasonalRegimeClassifier` | 59.6% | Orthogonal |
| `ecmwf_bias_corrected` | `ECMWFBiasCorrectedSignal` | 100% | NWP source |
| `metar_trend` | `MetarTrendSignal` | 100% | NWP source |
| `wind_direction_shift` | `WindDirectionShiftSignal` | 15.6% | Orthogonal |
| `spike_reversion` | `SpikeReversionSignal` | 29.5% | Down only |
| `goldilocks` | `GoldilocksSignal` | 58.5% | Daily signal — NOT the real-time alert lane |
| `radiational_cooling` | `RadiationalCoolingSignal` | 4.4% | LOW only |

### Lanes (outside `SignalRegistry`, posterior modifiers)

| Lane Module | File | Granularity | Mechanism | Status |
|-------------|------|-------------|-----------|--------|
| **Nowcasting** | `core/nowcasting_lane.py` | Intraday (30-90 min) | Probability-mass shift on posterior | ✅ Code + 57 tests, pipeline integration BROKEN |
| **Goldilocks real-time** | `metar_monitor.py` (embedded) | Real-time (seconds) | Fleeting spike detection → alert | ⚠️ Exists but undocumented |

---

## 4. Pipeline Position

```
┌─────────────────────────────────────────────┐
│  GEFS Ensemble (Prior — 66.17% baseline)    │
│  P(bucket) ← distribution                   │
└────────────┬────────────────────────────────┘
             │
             ▼
┌─────────────────────────────────────────────┐
│  SIGNAL LOOP (daily, per station)            │
│                                              │
│  13 signals in SignalRegistry vote:          │
│  gaussian, gaussian_v2, pressure_delta, ...  │
│                                              │
│  Each returns (direction, confidence)        │
│  → Ensemble vote → Posterior₁               │
└────────────┬────────────────────────────────┘
             │
             ▼
┌─────────────────────────────────────────────┐
│  LANE LAYER (intraday, event-driven)         │
│                                              │
│  NowcastingLane.get_prob_shift(station)      │
│  → If front detected: prob_shift dict        │
│  → Posterior₂ ∝ Post₁ × (1 + shift)         │
│                                              │
│  GoldilocksLane (future): transient spike    │
│  → alert → confidence modulation             │
└────────────┬────────────────────────────────┘
             │
             ▼
┌─────────────────────────────────────────────┐
│  Final EV: Σ P(bucket) × payoff(bucket)     │
│  → Kelly sizing → position                  │
└─────────────────────────────────────────────┘
```

**Key rule:** Signals compete horizontally in the vote layer. Lanes cascade vertically in the posterior layer. Never mix them.

---

## 5. Decision Flow — Is My Module a Signal or a Lane?

```
Does your module detect events intraday (not daily)?
├── YES → Does it predict direction ("up"/"down")?
│   ├── YES → Should it be a signal if granularity mismatch is acceptable
│   └── NO  → **LANE** — it modulates probability mass, not direction
│
└── NO → Does it fire on most days with direction output?
    ├── YES → **SIGNAL** — register in SignalRegistry, sweep, vote
    └── NO  → **LANE** or DEAD — if event-driven, it's a lane
```

**Smell test:** If you have to fight the `evaluate(idx, days)` interface (returning `(None, 0.0)` hacks, passing context dicts, breaking the type contract), your module is probably a lane, not a signal.

---

## 6. Historical Context

| Date | Event | Impact |
|------|-------|--------|
| 2026-07-24 | Gray Room R7 defines C10 as "build frontal passage detector" | C10 defined as Signal |
| 2026-07-24 | Phase 10 builds `frontal_passage_intraday_signal.py` | Signal registered, 10 tests pass |
| 2026-08-08 | Gray Room R13 Nowcasting Trajectory | **3 experts unanimous:** nowcasting is a LANE, not a signal |
| 2026-08-11 | Phase 1 Gray Room fixes applied to nowcasting | `frontal_passage_intraday_signal.py` archived; `frontal_passage_nowcast_signal.py` + `nowcasting_lane.py` built |
| 2026-08-11 | 57 tests pass | Lane architecture validated |
| 2026-08-11 | This reference doc created | Single source of truth |

---

## 7. Version History

| Date | Version | Author | Changes |
|------|---------|--------|---------|
| 2026-08-11 | 1.0 | Donna Paulsen | Initial reference — lane vs signal definitions, registry, pipeline diagram |