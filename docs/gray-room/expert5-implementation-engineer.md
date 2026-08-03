# Gray Room — Expert 5: Implementation Engineer

**Model:** luna-pro
**Date:** 2026-08-03
**Status:** COMPLETE

---

## 1. ERRORS

**E1. FUSION-LANES-FRAMING packet not found**  
File `docs/plans/GRAY-ROOM-FUSION-LANES-FRAMING.md` does not exist. Base architecture packet was referenced but unavailable. Reconstruction derived from codebase archaeology.  
→ **ADVANCE** — Have reconstructed from live codebase; recommend the architect re-publish the canonical framing.

**E2. `ensemble_fraction.py` dual-pool code is untestable in production**  
`compute_ensemble_fraction_both()` in `core/ensemble_fraction.py` contains ECMWF 82-member logic but NO production caller uses it. The main trading cron exclusively uses `compute_ensemble_signal()` from `gefs_paper_trading_cron.py`, which only reads `gefs_archive.db` (31-member).  
→ **ADVANCE** — Dead code that must be either wired in or removed before Q4 integration.

**E3. GEFS archive lacks `member_values` for most backfill rows**  
The `member_values BLOB` column in `gefs_archive` stores per-member int8 offsets. Without these, the cron falls back to a heuristic `min(0.99, 0.5 + temp_diff / 20.0)` for confidence — this is the old flawed formula.  
→ **ADVANCE** — Verify actual member fill rate; if <80% populated, ensemble-fraction signal is operating on degraded data. Run this query:
```sql
SELECT COUNT(*), SUM(CASE WHEN member_values IS NOT NULL THEN 1 ELSE 0 END) 
FROM gefs_archive WHERE step = 24;
```

**E4. Position sizer has dual, inconsistent edge-tier logic**  
`core/position_sizer.py` EDGE_TIERS and `gefs_paper_trading_cron.py` `EDGE_TIERS` are both hardcoded with slightly different thresholds. The cron applies epoch+tier multipliers directly to contract count; `position_sizer.py` applies them to Kelly fraction. These will diverge under live trading.  
→ **ADVANCE** — Consolidate into a single source of truth imported from a shared module.

**E5. `core/lane_manager.py` is a stub with no lane logic**  
Despite having a `lane_manager.py` module, it contains no routing, selection, or lane-dispatch logic. The phrase "lane" appears nowhere in the codebase beyond this filename.  
→ **ADVANCE** — Needs a full implementation before any lane architecture can function.

**E6. Data model conflict: `nwp_forecasts.db` stores single values per (date, station, model, variable); `gefs_archive.db` stores full 31-member ensemble BLOBs**  
The NWP database stores `value REAL` — a single scalar — making it inherently unable to represent ensemble member distributions. For ECMWF 82-member integration, this schema must either be extended or a parallel `ecmwf_archive.db` must be created.  
→ **ADVANCE** — Schema design decision needed before Q4 integration starts.

---

## 2. IDEAS

**I1. Single "Fusion Lanes Orchestrator" module**  
Create `core/lane_orchestrator.py` as the single entry point that:
- Routes each (station, date) to the appropriate lane
- Captures per-lane performance metrics
- Logs lane selection + outcome for post-hoc analysis
- Falls through lanes in priority order: Bayesian Cascade → Trajectory → Goldilocks → Baseline GEFS  
→ **ADVANCE** — This is the minimum viable architecture for three-lane operation. Prevents each lane becoming a silo.

**I2. Bayesian Cascade built on existing fusion stack**  
The existing `fusion_logic.py` has 4 layers (calibration, MI decorrelation, LLOP, Dempster-Shafer) and `compute_conviction_score`. This is 80% of a "Bayesian cascade." The gap is:
- No sequential Bayesian updating (current code fuses all signals at once)
- No Beta-Binomial state carried between trading days
- No Kalman-style filtering of signal drift  

A Bayesian Cascade lane would add sequential belief propagation on top of the existing fusion, reusing all 4 layers.  
→ **ADVANCE** — Low-risk; can reuse `BayesianBelief` from `position_sizer.py` and the `SignalFusionEngine` from `fusion_logic.py`.

**I3. Goldilocks lane as risk overlay rather than standalone signal**  
`goldilocks_predictive.py` computes P(Goldilocks condition) from METAR features. A Goldilocks day (low wind, clear skies, large diurnal range) is when the GEFS ensemble is most accurate (less chaotic atmospheric mixing). Rather than trading Goldilocks days separately, use the probability as a *confidence multiplier* on the baseline GEFS signal:

```
adjusted_confidence = raw_confidence × (0.7 + 0.6 × goldilocks_prob)
```

This gives a 0.7-1.3x multiplier, naturally downweighting non-Goldilocks days without needing a separate trading lane.  
→ **ADVANCE** — Simplest integration; requires 3 lines of code change in `compute_ensemble_signal()`.

**I4. Trajectory lane from `p3_trajectory_tracer.py` meteorology**  
`core/p3_trajectory_tracer.py` provides storm-track / weather-system trajectory logic. A Trajectory lane would:
- Identify when a midlatitude cyclone track passes near a station
- Estimate direction of temp change from storm quadrant (warm vs cold sector)
- Use only as override signal: "trajectory disagrees with GEFS = no trade"  

This is NOT a standalone trading signal — it's a veto gate.  
→ **ADVANCE** — Lower risk than treating it as a primary signal; reduces false positives in chaotic synoptic patterns.

**I5. ECMWF integration via parallel `ecmwf_archive.db`**  
Rather than modifying the existing GEFS schema (which has 31-member BLOB storage with int8 offsets), create a mirror `ecmwf_archive.db` with the same schema. This:
- Avoids breaking the current pipeline
- Allows independent backfill/validation
- Keeps fee-model and Kalshi settlement logic unchanged
- The fusion orchestrator simply reads both databases

SQL: `CREATE TABLE ecmwf_archive (target_date TEXT, station TEXT, step INTEGER, ensemble_mean REAL, ensemble_min REAL, ensemble_max REAL, member_values BLOB, n_members INTEGER, PRIMARY KEY (target_date, station, step))`  
→ **ADVANCE** — Cleanest path; 31+82=113 members compared. ECMWF is ~62% of combined pool.

---

## 3. IMPROVEMENTS / SPECS

**S1. Build path — Bayesian Cascade lane** (ship in <1 week)

**Minimum scope:**
1. Create `core/bayesian_cascade.py` — wraps existing `SignalFusionEngine`
2. Add sequential Beta-Binomial belief tracker (reuse `PositionSizer.BayesianBelief`)
3. Add day-over-day belief propagation: posterior today = prior × likelihood from yesterday's outcome
4. Integrate: 4 new files, ~200 lines total

```
belief_posterior = Beta(prior_alpha + wins, prior_beta + losses)
final_confidence = fusion_confidence × (belief_posterior_mean / 0.5)
```

**Full scope:**
- Kalman-filter-style drift detection on signal accuracy
- Adaptive fusion weights updated daily from posterior
- Regime-dependent prior: summer = higher prior alpha (more stable), winter = lower
- Add to trade DB schema: `prior_alpha`, `prior_beta`, `posterior_mean`

→ **ADVANCE**

**S2. Build path — Goldilocks lane** (ship in 2-3 days)

**Minimum scope:**
1. Add 6-line change to `gefs_paper_trading_cron.py::compute_ensemble_signal()`:

```python
# Goldilocks confidence modulation
if "use_goldilocks" in os.environ:
    try:
        from core.goldilocks_predictive import GoldilocksPredictor
        gl = GoldilocksPredictor(station).predict()
        gl_mod = 0.7 + 0.6 * gl.get("goldilocks_any_prob", 0.09)
        confidence = min(1.0, confidence * gl_mod)
    except Exception:
        pass
```

2. Add `--goldilocks` flag to cron CLI
3. Add METAR DB path to cron config
4. Log goldilocks modulation factor in trade DB

**No new infrastructure needed.** METAR data already collected by `metar_collect_live.py`. Model files should exist at `data/models/goldilocks_*_model.txt`.

**Full scope:**
- Per-station Goldilocks models (currently KNYC only)
- Real-time METAR freshness check (reject if obs > 2h old)
- Goldilocks-aware Kelly sizing multiplier

→ **ADVANCE**

**S3. Build path — Trajectory lane** (ship in 1-2 weeks)

**Minimum scope:**
1. Audit `p3_trajectory_tracer.py` for usable meteorology output
2. Create `core/trajectory_veto.py`:

```python
def should_veto(station, target_date, pred_direction):
    """Return True if trajectory analysis contradicts GEFS prediction."""
    # 1. Check if a synoptic-scale system passes near station on target_date
    # 2. Determine if station is in warm sector or cold sector
    # 3. Veto if trajectory temp-change direction != GEFS direction
    # (Implementation depends on p3_trajectory_tracer output format)
```

3. Integrate: one additional gate in the cron pipeline, after ensemble fraction, before Kelly sizing

**Full scope:**
- Storm track database (tracking cyclones day-over-day)
- Per-station trajectory climatology
- Confidence reduction rather than binary veto

→ **ADVANCE**

**S4. Test harness — Validation framework**

**Minimum scope:** One script that validates all 3 designs:

```python
scripts/validate_three_lanes.py --start 2025-08-03 --days 365
```

Each lane independently validates against the same settlement data:
- Bayesian Cascade: Compare fusion confidence vs actual outcome, check belief update convergence
- Goldilocks: Compare modulated vs raw confidence accuracy on Goldilocks vs non-Goldilocks days
- Trajectory: Compare veto rate vs actual accuracy improvement when trajectory contradicts GEFS

**Output per lane:** 
- Accuracy with/without lane
- P&L impact (simulated with 50¢ midpoint)
- False-positive rate (lane fires but hurts accuracy)
- 95% CI from bootstrap resampling (1000 resamples)

→ **ADVANCE**

**S5. Integration into GEFS cron pipeline**

Current pipeline flow:
```
nwp_collect.py (daily 06Z) → gefs_archive.db → gefs_paper_trading_cron.py (every 4h) → paper_trading_dev.db
```

Integration plan:
- **Goldilocks** can be inlined directly into cron (no pipeline change, ~6 LOC)
- **Bayesian Cascade** must be triggered AFTER cron runs (post-trade belief update). Add a `--update-belief` step at the end of cron or as a separate cron job running 15min after trading cron.
- **Trajectory** must run BEFORE the trading cron (trajectory data needs independent fetch). Add as `scripts/trajectory_fetch.sh` scheduled 30min before trading cron.
- **ECMWF** integration is orthogonal — add as additional data source before fusion, not a lane.

Proposed cron schedule:
```
04:00 UTC — trajectory_fetch.sh (if trajectory lane active)
04:30 UTC — gefs_paper_trading_cron.py (existing, unchanged)
04:45 UTC — bayesian_belief_update.py (new, after trades settled)
```

→ **ADVANCE**

---

## 4. ELEPHANTS

**🐘 ELEPHANT 1: The base packet framing doesn't exist, and none of the 3 "designs" have implementation-ready specs**

This is the biggest issue in the room. After thorough codebase audit:
- **Bayesian Cascade**: No sketches, no architecture doc, no code. The name is aspirational. The closest existing code is `fusion_logic.py` (4-layer LLOP fusion) and `PositionSizer.BayesianBelief` (Beta-Binomial tracker). Neither implements sequential Bayesian updating of ensemble predictions. This is 2+ weeks of design + implementation from a concrete spec.
- **Goldilocks lane**: Exists as `goldilocks_predictive.py` (LightGBM inference) but NOT integrated into any trading pipeline. The model was trained for NYC only (`KNYC` hardcoded). There is no per-station goldilocks model for the other 19 stations. The METAR DB has nationwide coverage, but the training pipeline only produced 1 model.
- **Trajectory lane**: `p3_trajectory_tracer.py` exists but its output format, data sources, and meteorological assumptions are undocumented. "Storm track" logic for binary temperature direction prediction at 20 specific points is a nontrivial meteorology problem. The file appears to be from a different experimental phase (P3) and was never productionized.

**Risk assessment:** If these three "lanes" are intended to ship in a single sprint:
- Goldilocks: 2-3 days (but only 1 of 20 stations)
- Bayesian Cascade: 2-4 weeks (needs specification first)
- Trajectory: 4-6 weeks (needs meteorologist input + engineering)

The framing implies these are existing designs ready for implementation. They are not.

→ **ADVANCE** — Recommendation: Frame this honestly. Goldilocks is the only quick win. Bayesian Cascade has the best ROI per engineering hour (reuses existing fusion infrastructure). Trajectory should be descoped to a research spike.

---

**🐘 ELEPHANT 2: ECMWF 82-member integration (Q4) — the existing codebase is not ready**

The current pipeline has multiple barriers to multi-model integration:

1. **Schema mismatch**: `gefs_archive.db` has `member_values BLOB` with int8 offsets from mean. ECMWF stores its own native format. No existing code reads ECMWF member-level data.
2. **No model-weighting infrastructure**: The codebase has NO concept of per-model calibration or weighting for multi-model fusion. `ensemble_fraction.py::compute_ensemble_fraction_both()` has a stub for `per_model_avg` vs `raw_pool` combination, but this is dead code.
3. **Fee/cost model duplication**: The cron has its own `kalshi_fee()`, position_sizer has its own fee model, and `market_cost_model.py` has a `ROUND_TRIP_FEE`. None are unified.
4. **Pipeline single-threaded**: The cron processes 20 stations sequentially. Adding 82 ECMWF members per station (4× data) will increase processing time proportionally unless the architecture is parallelized.
5. **No ECMWF data backfill**: ECMWF data exists in `nwp_forecasts.db` as daily max/min values only. No member-level ECMWF data exists for backtesting.

**Recommendation — additive, non-breaking integration path:**

```
Phase A — Feb 2027:
  - Create ecmwf_archive.db (same schema as gefs_archive)
  - Backfill ECMWF ensemble data (82 members, daily max/min)
  - Run parallel backtest: GEFS-only vs ECMWF-only vs combined
  - Publish comparison: accuracy, PnL, Sharpe deltas

Phase B — Mar 2027:
  - Build multi-model fusion layer:
    * Per-model calibration (separate curves for GEFS vs ECMWF)
    * Pooling strategies: raw_concat, per_model_avg, weighted_by_recency
    * Kalman-filter model-weight tracking (update daily from outcomes)
  - Wire into cron pipeline

Phase C — Apr 2027:
  - Watch the live pipeline for at least 30 days
  - Compare daily PnL vs GEFS-only counterfactual
  - If ECMWF degrades performance, keep as optional fallback
```

→ **ADVANCE**

---

**🐘 ELEPHANT 3: The cron job uses `openai/gpt-5.4-mini` model, but the pipeline is purely computational — no model needed**

The `gefs-paper-trading-cron` cron entry shows `model=openai/gpt-5.4-mini`. This is wasteful. The pipeline (`gefs_paper_trading_cron.py`) is a deterministic Python script with zero AI model calls. The cron needs `model=ollama/qwen2.5-coder:7b` (or remove the model field entirely for script-based cron jobs). Using GPT models for script runners burns ~$0.15-0.30 per run × 6 runs/day × 30 days = $27-54/month for nothing.

→ **ADVANCE** — Fix: Change cron model to `ollama/qwen2.5-coder:7b` or add `payload.model=""` for no-model execution.

---

## 5. SUMMARY TABLE

| Item | Type | Priority | Effort | Recommendation |
|------|------|----------|--------|----------------|
| E1: Missing framing doc | ERROR | Critical | — | Architect to re-publish |
| E2: Dead dual-pool code | ERROR | High | 1d | Wire or delete |
| E3: GEFS member fill rate | ERROR | High | 2h | Audit + backfill |
| E4: Dual edge-tier logic | ERROR | High | 1d | Consolidate into shared module |
| E5: Stub lane_manager | ERROR | High | 1w | Full implementation |
| E6: Schema conflict for multi-model | ERROR | Medium | — | Escalate to architect |
| I1: Fusion lanes orchestrator | IDEA | High | 3d | Create single entry point |
| I2: Bayesian cascade from fusion stack | IDEA | High | 2w | Reuse existing infrastructure |
| I3: Goldilocks as risk overlay | IDEA | Low | 2-3d | Easiest win |
| I4: Trajectory as veto gate | IDEA | Medium | 2w | Descope from primary signal |
| I5: Parallel ecmwf_archive.db | IDEA | Medium | 1w | Cleanest ECMWF path |
| S1: Bayesian cascade build | SPEC | High | 2w | Needs spec first |
| S2: Goldilocks build | SPEC | Low | 2-3d | Quickest to ship |
| S3: Trajectory build | SPEC | Medium | 4-6w | Needs meteorology input |
| S4: Test harness | SPEC | High | 3d | Gate before any production merge |
| S5: Cron integration | SPEC | Medium | 1d | Staggered cron schedule |
| 🐘 E1: No implementation-ready specs | ELEPHANT | Critical | — | Honest reframe needed |
| 🐘 E2: ECMWF barriers | ELEPHANT | Medium | Q4 2027 | Phased additive approach |
| 🐘 E3: GPT model on script cron ($54/mo) | ELEPHANT | Low | 5min | Fix model assignment |

---

**Bottom line for Dan:** Goldilocks lane can ship in 2-3 days (KNYC only). Bayesian Cascade needs a concrete spec but has 80% of the infrastructure. Trajectory is the riskiest — needs meteorology expertise before engineering begins. The three "designs" in the framing are at very different maturity levels and should NOT be treated as equal-scope deliverables.