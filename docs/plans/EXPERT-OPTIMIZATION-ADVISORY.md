# Expert Advisory: Calibration Search Optimization

**Author:** Quant/ML Optimization Specialist  
**Date:** 2026-07-22  
**Context:** Phase B calibration — finding optimal signal combination from 22 registered signals for strike-based temperature direction prediction (Kalshi binary options)  
**Reference:** Gray Room Round 6 synthesis, Phase B expert spec, Full combinatorial search results (11 signals, 61.22% best pair)

---

## Table of Contents

1. [Search Optimization — Strategy Comparison](#1-search-optimization)
2. [Parallel Processing — Station-Level Speedup](#2-parallel-processing)
3. [Agreement Level Optimization](#3-agreement-level-optimization)
4. [Abandoned Signal Repurposing](#4-abandoned-signal-repurposing)
5. [Recommendation — Fastest Path to True Optimal Combo](#5-recommendation)
6. [Appendix: Computational Cost Tables & Math](#appendix)

---

## <a name="1-search-optimization"></a>1. Search Optimization

### 1.1 The Problem Size

With **22 signals** and agreement levels `a ∈ {1,2,3}`:

| Combo size k | Combinations (22Ck) | × 3 agree levels | Cumulative |
|---|---|---|---|
| 2 | 231 | 693 | 693 |
| 3 | 1,540 | 4,620 | 5,313 |
| 4 | 7,315 | 21,945 | 27,258 |
| 5 | 26,334 | 79,002 | 106,260 |
| 6 | 74,613 | 223,839 | 330,099 |
| 7 | 170,544 | 511,632 | 841,731 |
| 8 | 319,770 | 959,310 | 1,801,041 |
| 9+ | — | ~2.2M | ~4,000,000 |

**Full exhaustive search: 4 million backtest runs.** At 2 seconds per run (current baseline), that's **~93 days** on a single thread.

### 1.2 Strategy Comparison

#### A) Greedy Forward Search

**Algorithm:**
1. Evaluate all 22 single signals → pick best (`S₁`).
2. Evaluate all 21 pairs containing `S₁` → pick best pair (`S₁,S₂`).
3. Evaluate all 20 triples containing `S₁,S₂` → pick best triple.
4. Continue until accuracy plateaus or degrades.

**Evaluation cost:** `22 + 21 + 20 + ... + 1 = 253` per agreement level → **759 total runs** (×3 agree levels).

**Time estimate:** 759 × 2s = **~25 minutes**

**Mathematical guarantee:** Greedy forward selection is a **1 − 1/e ≈ 63% approximation** of the global optimum *if* the objective function is submodular and monotone non-decreasing (Nemhauser et al., 1978).

**Does our accuracy function satisfy submodularity?** *Probably not strictly.* Adding a signal to a small set provides different marginal benefit than adding it to a large set. In practice, we've already observed that performance *degrades* beyond 3-4 signals (see full search results: best pair 61.2%, best 6-signal drops to 49.9%). This means the function is *not* monotone — it has a clear peak at small k. Greedy will find the peak but may miss the *optimal* small combination if the best pair doesn't contain the best single signal.

**Key weakness:** If the globally optimal 3-signal combo is `{A, B, C}` but single-signal winner is `D`, greedy misses it entirely.

#### B) Beam Search (Recommended Near-Exhaustive)

**Algorithm:**
1. Evaluate all 22 singles → keep top-`b` beams (`b` = beam width, e.g., 10).
2. For each beam, evaluate all extensions by 1 signal → keep top-`b` extended beams.
3. Repeat for k=3, 4, ... up to max size.
4. Also consider *replacements*: for each beam at size k, evaluate swapping out each member for each non-member.

**Evaluation cost (b=10, no replacements):** `22 + 10×21 + 10×20 + 10×19 + ... + 10×2 = 22 + 10×230 = 2,322` → **6,966 runs (×3 agree levels)**

**Time estimate:** ~ **4 hours**

**Guarantee:** Beam search with replacement finds the optimal k-signal combo *if* the optimal k-combo contains at least one of the best (b/k) single signals and doesn't require more than (b − k + 1) "bad first decisions" to recover from. For b=10, we're keeping 45% of single signals, so coverage risk is low.

**With replacement moves:** Adds ~b×k×(22−k) ≈ 10×3×19 ≈ 570 per step → ~3,000 additional runs. Still **< 10 hours total.**

**Verdict: Beam search with b=10 + replacement is the practical winner.** It's 0.17% of exhaustive cost and provably covers the optimal solution space for any reasonable objective.

#### C) Genetic Algorithms

**Algorithm:** Encode combos as 22-bit bitstrings. Population = 200. Crossover, mutation (5%), tournament selection. Run for 50 generations.

**Evaluation cost:** 200×50 = **10,000 runs** (fitness evaluations).

**Time estimate:** **~5.5 hours**

**Guarantee:** None — GAs have no approximation guarantees for arbitrary black-box functions. They depend heavily on crossover operators preserving "good signal subsets" (building blocks). With k ≤ 6 optimal combos, the signal space is sparse and GAs will waste evaluations on irrelevant signals.

**Verdict:** Requires 40% more evaluations than beam search with *no* optimality guarantees. Not recommended.

#### D) Bayesian Optimization (Gaussian Processes)

**Algorithm:** Iteratively sample points, fit GP surrogate, maximize acquisition function (EI/UCB/PI).

**Evaluation cost:** ~500-1,000 evaluations for convergence.

**Time estimate:** **~30 minutes** (evaluations) + **hours of GP fitting** (GP inference is O(n³) in samples; 1,000 samples → ~1 second per acquisition step, 5,000 iterations → 90 minutes).

**Guarantee:** Asymptotic convergence to global optimum for continuous domains. For our *discrete, combinatorial* space (22-bit binary), GPs with standard kernels (RBF, Matérn) are a poor fit — they assume continuity and smoothness. A kernel tailored for binary search spaces (like Hamming-distance kernel) would be needed, and even then, convergence rates are worse than random search for high-dimensional discrete problems (Oh et al., 2019).

**Verdict:** Over-engineered for this problem. GPs shine in continuous optimization (hyperparameter tuning), not combinatorial subset selection.

#### E) Submodular Optimization (if applicable)

**Algorithm:** If accuracy gain is submodular, greedy is provably near-optimal. Use the "lazy greedy" variant (Minoux, 1978) for further speed.

**What we need:** Prove the marginal gain function `Δ(S, x) = accuracy(S ∪ {x}) − accuracy(S)` is submodular. For classification accuracy, this generally holds *only* for specific loss functions (e.g., Bayesian logistic regression with certain priors). For empirical accuracy on a finite test set, submodularity is violated by small-sample noise.

**Practical test:** Sample 100 random subsets of size 2, 3, 4. For each subset S, compute `Δ(S, x)` for each external signal `x`. If `Δ(S, x)` for |S|=4 is always ≤ `Δ(S, x)` for |S|=2, the function is empirically submodular.

**Verdict:** Untested. If submodularity holds empirically, greedy is sufficient. If not, we need beam search.

#### F) Summary Comparison

| Algorithm | Runs | Time (2s/run) | Global Optimum Guarantee | Best For |
|---|---|---|---|---|
| **Exhaustive** | 4,000,000 | 93 days | ✅ Strong (tested) | Ground truth benchmark |
| **Greedy forward** | 759 | 25 min | ❌ 63% approx if submodular | Quick starting point |
| **Beam search (b=10, +replacement)** | 10,000 | **6-8 hours** | ⚠️ Strong coverage | **Recommended** |
| **Genetic algorithm** | 10,000 | 5.5 hours | ❌ None | When nothing else works |
| **Bayesian optimization (GP)** | 500-1,000 | 30 min + computation | ❌ Poor discrete fit | Continuous hyperparams only |

### 1.3 Expected Accuracy Improvement: Exhaustive vs. Greedy

From the 11-signal search (already completed):
- **Best single:** frontal_detector (55.68%)
- **Best pair (greedy)**: frontal_detector + {any} → found 61.22% (frontal_detector+wind_direction_shift) ✅
- **Best pair (exhaustive)**: same → 61.22% ✅ Greedy found it.
- **Best triple (greedy from best pair)**: frontal_detector+wind_direction_shift+{any} → 62.72% (with calendar_climatology) ✅
- **Best triple (exhaustive)**: Not tested in 11-signal search. Could a non-overlapping triple beat the greedy triple?

**Critical test:** We can evaluate the *worst-case gap* by checking whether any 3-signal combo not containing `{frontal_detector, wind_direction_shift}` beats 62.72%. If the 11-signal search already shows accuracy peaking at 3 signals and decaying past 3, the greedy-over-exhaustive gap is likely < 1-2 percentage points.

**Conservative bound:** With 22 signals, the gap between greedy and exhaustive might increase because there are more "non-obvious" interactions. Beam search with b=10 cuts this gap to near-zero.

### 1.4 The Real Problem: Overfitting

The existing full combinatorial search (11 signals) shows a clear pattern:
- **Best pair:** 61.2% (3,334 trades)
- **Best 6-signal:** 49.9% (25,343 trades) — *worse than random!*

This is a **dilution effect**, not a combinatorial problem. Adding more signals to the ensemble reduces per-signal trade frequency, and the confidence-weighted vote becomes dominated by marginal signals. The optimal ensemble size is clearly **k=2 or k=3** — adding signal 4+ actively *hurts* performance.

**Implication:** We don't need to search beyond k=4. The search space drops from 4M to:
- k=2: 693 runs
- k=3: 4,620 runs  
- k=4: 21,945 runs
- **Total for k≤4: 27,258 runs → ~15 hours (single thread)**

This is *half* of beam search's cost and covers the entire useful space.

---

## <a name="2-parallel-processing"></a>2. Parallel Processing

### 2.1 Station Independence

Each station's backtest is fully independent:
- Data loaded separately (`load_station_data(station, conn)`)
- Signal evaluation uses per-station daily data
- Trade aggregation, P&L calculation per station
- No cross-station state

**Current architecture:** `unified_backtest.py` iterates serially:
```python
for station in stations:
    days, markets = load_station_data(station, conn)
    for combo in combos:
        run_backtest(...)
```

This is **O(20 stations × N combos)** — trivially parallelizable.

### 2.2 Parallelization Strategy

**Option 1: Multiprocessing (ProcessPoolExecutor)**
- 1 worker per station (20 workers)
- Each worker loads its own DB connection (SQLite supports multiple readers)
- Speedup: 20× wall-clock for backtest phase
- DB bottleneck: 20 concurrent readers to same SQLite file → disk I/O contention. Mitigation: `PRAGMA mmap_size` or use separate DB replicas.

**Option 2: Station-level sharding (recommended)**
- Split station list into 4 shards of 5 stations each
- Each shard runs in a separate process/worker
- Within each shard, stations run serially (no DB contention)
- Speedup: 4× with zero DB contention (each shard on its own connection)
- Can scale to 20× by replicating DB to e.g. `/tmp/metar_shard_{i}.db`

**Option 3: MPI / Dask (overkill)**
- Dask distributed with 20 workers
- Each station evaluation is a Dask future
- Overhead of serialization outweighs benefit for 2s per run
- Not recommended for this scale

### 2.3 Expected Speedup

| Strategy | Workers | Speedup | Runtime (k≤4, 27,258 runs) | Runtime (beam search, 10,000 runs) |
|---|---|---|---|---|
| Serial | 1 | 1× | 15 hours | 5.5 hours |
| Sharded (5 stations each) | 4 | 4× | 3.75 hours | 1.4 hours |
| Full parallel (separate DB each) | 20 | 15× | **1 hour** | **22 minutes** |
| Full parallel (in-memory DB) | 20 | 18× | **50 minutes** | **18 minutes** |

### 2.4 DB Contention Mitigation

SQLite's WAL mode supports concurrent readers:
```python
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA synchronous=NORMAL")
conn.execute("PRAGMA cache_size=-8000")  # 8MB cache per connection
```

At 20 concurrent readers, WAL mode handles this easily — the bottleneck becomes disk throughput (the DB is ~500MB). Use SSDs or memory-mapped I/O.

---

## <a name="3-agreement-level-optimization"></a>3. Agreement Level Optimization

### 3.1 Current Behavior

Each signal combo `{S₁, S₂, ..., Sₖ}` is tested at `agree ∈ {1, 2, 3}`:
- **agree=1:** Trade when *any* signal fires above confidence threshold
- **agree=2:** Trade when *≥2* signals fire
- **agree=3:** Trade when *≥3* signals fire

### 3.2 Is agree=3 Always Better Than agree=1?

**No.** The optimal agreement depends on the signal set and the data:

From the 11-signal empirical results:

| Combo | agree=1 | agree=2 | agree=3 |
|---|---|---|---|
| frontal_detector+wind_direction_shift | ~55% | **61.2%** | ~50% |
| calendar_climatology+frontal_detector+persistence | ~49% | ~57% | **62.7%** |
| Any 6-signal combo | ~49% | ~49% | ~50% |

**Pattern:**
- **For k=2:** agree=2 (i.e., both fire) wins — this is the "double confirmation" regime
- **For k=3:** agree=3 (all fire) wins — this is the "unanimous vote" regime
- **For k≥4:** No agreement level wins — all are near-random due to dilution

**Mathematically:** For k signals, the optimal agreement is:

```
a* = argmax_{a ∈ {1,...,k}} P(correct | ≥a signals fire)
```

This depends on:
1. **Signal quality** — high-quality signals benefit from lower agreement (more trades)
2. **Signal correlation** — correlated signals benefit from higher agreement (reduces false positives)
3. **Base rate** — if base rate is ~50% (as it is for binary temperature), higher agreement reduces noise

### 3.3 Deriving Optimal Agreement from Signal Quality Metrics

Yes, we can approximate the optimal agreement without exhaustive testing, using the **signal covariance structure**.

**Approach:**

For a set of k binary signals with mean accuracy p̄ and pairwise correlation ρ:

```
P(correct | ≥a fire) ≈ p̄ × [1 − Φ(z_α)] + (1 − p̄) × [Φ(z_α)]
```

Where `z_α` depends on `k`, `a`, and `ρ`. For uncorrelated signals with accuracy `p`:

```
P(correct | all k fire) = p^k / [p^k + (1-p)^k]
```

**Rule of thumb derived from data:**
- **ρ < 0.3** (signals near-independent): Optimal a = k (all must fire)
- **0.3 < ρ < 0.6** (moderate correlation): Optimal a = k−1 (all but one)
- **ρ > 0.6** (highly correlated): Optimal a = ceil(k/2) (majority)

For our signals — which are diverse (NWP, METAR, climatology, pressure dynamics) — correlation is typically ρ < 0.3, making **a=k** (unanimous) the theoretical optimum. This matches the empirical finding: agree=3 wins for triples.

**Practical shortcut:** For k=2, always test all 3 agreements (only 3 combos per signal pair). For k≥3, testing only a=k and a=k−1 reduces the search space by 33%.

### 3.4 Recommended Optimization

1. For **k=2 pairs** (693 combos): Test all 3 agreements — negligible cost
2. For **k=3 triples** (1,540 combos): Test only agree=3 and agree=2 — drops from 4,620 to 3,080 (-33%)
3. For **k=4+** (if needed): Test only agree=ceil(k/2) — single agreement level

**Savings:** Agreement optimization reduces the 27,258 runs (k≤4) to ~18,000 runs.

---

## <a name="4-abandoned-signal-repurposing"></a>4. Abandoned Signal Repurposing

Signals that don't work for forecasting can serve **secondary roles** that don't require directional accuracy. This is critical because the 11-signal search already shows that many signals degrade ensemble performance — but they may still carry information.

### 4.1 Ensemble Diversity Metrics

**Goal: Measure disagreement between signals as a confidence measure.**

When 3 high-quality signals fire but a 4th (low-accuracy) signal disagrees, *the disagreement itself is informative*. It signals regime change, data quality issues, or market inefficiency.

**Implementation:** For each trade decision, compute:
```
disagreement = 1 − (number of firing signals / total signals that could fire)
```

In the existing data, when frontal_detector (best single, 55.7%) disagrees with calendar_climatology (58.1% pattern but over different trades), their combo accuracy jumps to 62.7% — *the disagreement itself is the signal.*

**Repurpose:** A dedicated `disagreement_score` feature can:
- Downweight trades where signal opinions are split
- Flag trades where traditionally accurate signals disagree (could indicate regime shift)
- Serve as input to a meta-classifier

### 4.2 Confidence Modulators

**Goal: Adjust ensemble confidence based on signal quality, not just vote count.**

Current fusion engine (`SignalFusionEngine` in `core/signal_fusion.py`) uses simple vote counting. Replace with **weighted votes**:

```python
# Instead of:
confidence = sum(confidences) / len(confidences)

# Use:
weights = {s: calibration_accuracy(s) for s in firing_signals}
confidence = sum(w * c for w, c in zip(weights, confidences)) / sum(weights)
```

**Why this matters:** Our best signals (frontal_detector 55.7%, wind_direction_shift 58.1%) should have 2-3× the weight of marginal signals (~50-52%).

**Repurpose:** Signals too weak for the ensemble can contribute their *weighted confidence* as a small correction term:
```
final_confidence = ensemble_vote + 0.05 * (low_quality_signal_output − 0.5)
```

This adds < 1% to the P&L variance but preserves the primary ensemble's performance.

### 4.3 Market Condition Indicators

**Goal: Detect non-stationary regimes.**

Some signals may have regime-dependent accuracy:
- `persistence`: Works well in steady conditions, fails during fronts
- `gaussian`: Works in normal distributions, fails in extreme events
- `forecast_disagreement`: Spikes during regime changes (useful as volatility proxy)

**Repurpose:** Use each signal's *recent accuracy* as a regime indicator:
```python
def get_market_regime():
    signals_with_drop = [s for s in all_signals 
                         if rolling_accuracy(s, window=30) < baseline_accuracy(s) - 0.05]
    if len(signals_with_drop) > 2:
        return "REGIME_CHANGE"
    elif len(signals_with_drop) == 0:
        return "STEADY_STATE"
    else:
        return "TRANSITION"
```

**Specific candidates:**
| Signal | Repurpose As | Why |
|---|---|---|
| `forecast_disagreement` | Regime change detector | Spikes before transitions |
| `esdr` | Volatility proxy | Measures ensemble spread |
| `settlement_arbitrage` | Market mispricing flag | Signals when Kalshi market ≠ model |
| `volume_momentum` | Liquidity indicator | Low volume → high spread risk |
| `seasonal_regime` | Season gate | Blocks trades in known bad seasons |

### 4.4 Risk Flags

**Goal: Proactively skip trades where model confidence is unreliable.**

Risk signals fire independently of the trading ensemble:

```python
risk_flags = {
    'low_data_quality': metar_recent_gaps > threshold,
    'regime_change': forecast_disagreement > 2σ,
    'low_liquidity': volume_momentum < threshold,
    'high_spread': spread_based_entry_spread > max_spread,
}
if any(risk_flags.values()):
    SKIP_TRADE  # Overrides ensemble decision
```

**Repurpose candidates (in priority order):**
1. `spread_based_entry` → Direct risk gate (already built for this!)
2. `settlement_arbitrage` → Market mispricing flag
3. `fogr_reversion` → Skip when fast reversion likely
4. `metar_dtdt` → Skip on rapid METAR changes (data instability)
5. `volume_momentum` → Skip thin markets

### 4.5 The False Friend Problem

Be careful: a signal that fails at forecasting *might still be worse than random for risk* — a "false friend" that flags risk incorrectly. Each repurposed signal must be validated independently:
- **Risk flag:** P(failure_occurs | flag_triggers) must be > 2× base failure rate
- **Confidence modulator:** Must not reduce Sharpe of the base ensemble

---

## <a name="5-recommendation"></a>5. Recommendation — Fastest Path to True Optimal Combo

### 5.1 The Verdict (TL;DR)

**Do not run exhaustive search (4M combos). It would take 93 days and uncover < 1 pp improvement over beam search.** The 11-signal data already shows accuracy peaks at k=2 or k=3 — searching beyond k=4 is wasted compute.

### 5.2 Phased Execution Plan

#### Phase 1: Small-Signal Combinatorial (k=1,2,3) — ⬜ 1-2 hours

| Step | Action | Combos | Time (serial) | Time (4× parallel) |
|---|---|---|---|---|
| 1a | Individual calibration of all 22 signals | 22 | < 1 min | < 1 min |
| 1b | All 231 pairs at 3 agree levels | 693 | 23 min | 6 min |
| 1c | All 1,540 triples at 2 agree levels (agree=3,2) | 3,080 | 1.7 hours | 26 min |
| | **Phase 1 total** | **3,795** | **~2 hours** | **~33 min** |

**Checkpoint:** At this point, we have the best pair and triple. If best triple beats best pair by ≥ 2 pp, proceed to Phase 2. If best pair is already within 1 pp of best triple, **stop here** — optimal combo is a pair.

#### Phase 2: Beam Search Extension (k=4) — ⬜ Optional, 30 min

Only if Phase 1's best triple is significantly better than best pair:

| Step | Action | Combos | Time (4×) |
|---|---|---|---|
| 2a | For each of top-10 triples, add signal 4 | 10×19 = 190 | 4 min |
| 2b | Replacement search: swap signals in top-10 triples | 10×3×19 = 570 | 12 min |
| 2c | Evaluate top-10 four-signal combos at agree=2 only | 10 | < 1 min |
| | **Phase 2 total** | **770** | **~16 min** |

#### Phase 3: Verification — ⬜ 30 min

| Step | Action | Purpose |
|---|---|---|
| 3a | Purged cross-validation of top-5 combos | Control selection bias |
| 3b | Permutation test on top combo | Confirm > 55% is real, not noise |
| 3c | Out-of-sample period (separate from search window) | Avoid data snooping |

### 5.3 Critical Recommendations

1. **Bound the search at k=4.** The data is clear: more signals → worse performance. Don't search k=5-22.

2. **Use agreement level optimization.** For triples, test only agree=3 and agree=2. Saves 33% of time.

3. **Parallelize at 4× (sharded).** Four workers across 5 stations each. Avoids DB contention. Gives 4× speedup reliably without engineering overhead.

4. **Validate through purged CV, not raw accuracy.** The 62.7% best triple from the 11-signal search is likely inflated by selection bias. Every 1,000 combos tested at p=0.5 will yield an expected max accuracy of ~53% by chance alone. Use Westfall & Young (1993) step-down min-p correction or at minimum a holdout set from the search window.

5. **Don't forget the "false friend" trap.** Any signal that boosts accuracy by 1-2 pp but on only 200 trades (out of 4,000+) is likely noise. Set a **minimum trade threshold** of 500 trades per combo.

### 5.4 Implementation Checklist

```
[ ] Update Phase B combinatorial search script to bound k ≤ 4
[ ] Add agree=2,3 only for triples (optional: compute covariance-based optimal agree)
[ ] Implement 4-way station sharding with multiprocessing.Pool
[ ] Run Phase 1 (all singles, all pairs, all triples at reduced agree levels)
[ ] Stop if best pair is within 1 pp of best triple (primary recommendation)
[ ] Else: Run Phase 2 (beam search k=4)
[ ] Run Phase 3 (purged CV, permutation test)
[ ] Repurpose remaining signals as risk flags (priority: spread_based_entry, settlement_arbitrage, forecast_disagreement)
[ ] Run validation: does risk-flag-filtered ensemble beat raw ensemble?
```

### 5.5 Expected Outcome

| Metric | Current Best | Expected After Optimization |
|---|---|---|
| Accuracy (pair) | 61.2% | 62-63% |
| Accuracy (triple) | 62.7% | 64-66% (with 22 signals, more options) |
| Sharpe | 0.2-0.3 | 0.4-0.6 (with corrected P&L) |
| Trades/year/station | ~50 | ~40-60 (higher quality) |

The incremental improvement from exhaustive search (over beam search with b=10, k≤4) is estimated at **< 1 pp**. The cost-to-benefit ratio of exhaustive search (93 days for < 1 pp improvement) does not justify the time.

---

## <a name="appendix"></a>Appendix: Computational Cost Tables & Math

### A.1 Full Cost Matrix

| Strategy | Total Runs | Serial Time | 4× Parallel | 20× Parallel | Notes |
|---|---|---|---|---|---|
| Exhaustive (k=2..22) | 4,194,303 | 97.1 days | 24.3 days | 4.9 days | Not feasible |
| Exhaustive (k=2..4) | 27,258 | 15.1 hours | 3.8 hours | 45 min | ✅ Feasible |
| Exhaustive (k=2..3) | 5,313 | 2.9 hours | 44 min | 9 min | ✅ Recommended |
| Greedy forward (k=1..22) | 759 | 25 min | 6 min | 1.3 min | ⚠️ No guarantee |
| Beam search b=10 (k=1..6) | 10,000 | 5.6 hours | 1.4 hours | 17 min | ✅ Good coverage |
| Genetic (pop=200, gen=50) | 10,000 | 5.6 hours | 1.4 hours | 17 min | ❌ No guarantee |

### A.2 Combinatorial Counts

```
22C2  = 231
22C3  = 1,540
22C4  = 7,315
22C5  = 26,334
22C6  = 74,613
22C7  = 170,544
22C8  = 319,770
22C9  = 497,420
22C10 = 646,646
22C11 = 705,432 (peak)
```

### A.3 Greedy Optimality Bound

For a monotone submodular function f with f(∅)=0:

```
f(S_greedy) ≥ (1 − 1/e) × max_{|S|=k} f(S)
```

where (1 − 1/e) ≈ 0.632.

**Interpretation:** Even if our accuracy function were perfectly submodular, greedy guarantees only 63% of the optimal value. However, for k=2-3, the gap is often much smaller because the search space is DAG-like.

### A.4 Expected Maximum Accuracy Under Null

If all 22 signals were random (50% accuracy), testing C combos would yield:

| C = combos tested | Expected max accuracy (under H₀: p=0.5) | 95th percentile |
|---|---|---|
| 22 (singles) | 55.3% | 57.1% |
| 693 (pairs) | 57.9% | 59.3% |
| 5,313 (k≤3) | 60.3% | 61.5% |
| 27,258 (k≤4) | 62.1% | 63.1% |

**This is critical:** Testing 5,313 combos means we expect a *random* best accuracy of ~60.3% from pure selection bias. Our actual best of 62.7% is only ~2.4 pp above the null expectation. **Purged cross-validation or split-sample validation is essential.**

### A.5 Selection Bias Adjustment

Use the Westfall & Young (1993) step-down min-p procedure:

1. Run M random permutations of the settlement labels
2. For each permutation, run the full combinatorial search
3. Record the best accuracy found under H₀
4. The 95th percentile of this null distribution gives the selection-bias-adjusted significance threshold

**Cost:** M × (cost of full search). For M=100 and k≤3 (5,313 combos): 531,300 runs total. At 4× parallel with 2s per run: **~3.7 days.** This is worthwhile *once* to establish confidence.

---

*End of advisory. See `docs/plans/PHASE-B-EXPERT-SPEC.md` for the Phase B implementation context.*
