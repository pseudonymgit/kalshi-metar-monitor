# R/T-8F — Fusion Wiring A/B — Receipt

**Date:** 2026-09-21
**Task:** A/B test Path A (agreement-gate stack) vs Path B (LIOP/UWC fusion) on the
corrected 7-signal set + corrected ground-truth axis, then wire the data-driven default
behind a reversible feature flag.

**Commit:** `409ec58ab`
**Branch:** `temp-sprint-branch` (NOT merged — merge is Dan's)

---

## 1. Scope & reversibility

- **Path A (baseline) is UNTOUCHED.** `paper_trading_engine.py` keeps the full
  agreement-gate stack (`adaptive_thresholds` → `agreement_gate` → `station_skill_gate`
  → `spatial_coherence` → `ensemble_diversity` → `position_sizing`) as the default path.
- **Path B is behind a feature flag:** `FUSION_MODE` in `core/paper_trading_engine.py`
  (env `PAPER_FUSION_MODE`, values `agreement_gate` | `liop` | `uwc`; default
  `agreement_gate`). When set to `liop`/`uwc`, `generate_signals()` routes through
  `_fuse_path_b()` and emits one fused `(station, market_type, direction, reason, conf)`
  per location instead of the gate stack. Falls back to Path A on any import/fusion
  failure — the live path is never left without a prediction source.

## 2. Two pre-existing LIOP-engine bugs fixed (core/fusion_logic.py)

Path B (LIOP) could not run until two latent bugs in `SignalFusionEngine.fuse_signals`
were fixed. These were *runtime* bugs left behind by R/T-8a (which fixed the *import*,
not the body):

1. **UnboundLocal `_logger`** — a local `_logger = logging.getLogger(__name__)` inside
   `fuse_signals` shadowed the module-level `_logger`, making the earlier
   `_logger.info(...)` raise `UnboundLocalError`. Fix: removed the shadowing local
   assignment (the method now uses the module-level logger).
2. **UnboundLocal `n_signals`** — `n_signals` was referenced in the diversity-score
   block but never assigned. Fix: added `n_signals = len(calibrated_signals)` after the
   empty-signal guard.

Both are minimal, deterministic, and isolated to `fuse_signals`. No behavior change to
any other path.

## 3. Ground-truth axis & signal set (provenance)

- **Axis:** `strike_crossing_direction` (`core/settlement_outcome.py`) — settlement_temp_f
  vs strike_price, strict `>`/`<`. NOT day-over-day warming (the conflation bug).
- **Signals (corrected 7):** `gaussian`, `gaussian_v2`, `pressure_delta`,
  `calendar_climatology`, `forecast_disagreement`, `corrected_pressure_delta`,
  `frontal_passage_intraday`.
- **Dataset:** 14,130-label backfill (R/T-2b cache `reports/rt2b_prediction_matrix_cache.json`),
  filtered to rows where ≥1 of the 7 signals fires → **12,696 rows**.
  Ground truth: up = 4,363 (34.37%), down = 8,333 (65.63%) — strong down-skew.

## 4. Results (every number carries axis + provenance)

Axis = strike-crossing; provenance = 7 corrected signals on 12,696 labeled rows.

| Path | n | dir-acc | baseΔ | MCC | Brier | ECE | Sharpe |
|---|---|---|---|---|---|---|---|
| NULL (always-down) | 12696 | **65.63%** | +0.00pt | 0.0000 | 0.2500 | 0.1563 | 5.23 |
| **Path A** (agreement-gate N≥2) | 9229 | 51.25% | −14.71pt | −0.0298 | 0.4674 | 0.4745 | 0.40 |
| **Path B** — LIOP flat (no cal) | 12696 | 50.33% | −15.30pt | −0.0074 | 0.4993 | 0.4969 | 0.11 |
| **Path B** — LIOP engine (SignalFusionEngine) | 11909 | 51.11% | −14.65pt | −0.0119 | **0.3628** | **0.3389** | 0.35 |
| **Path B** — UWC (cascade) | 12696 | 34.37% | −31.27pt | 0.0000 | 0.3493 | 0.3515 | −5.23 |

### Per-class precision (provenance: same axis/set)

| Path | up_prec | down_prec | up_called | down_called |
|---|---|---|---|---|
| NULL | — | 65.63% | 0 | 12696 |
| Path A | 32.36% | 64.77% | 3850 | 5379 |
| LIOP engine | 33.61% | 65.26% | 5323 | 6586 |
| UWC | 34.37% | — | 12696 | 0 |

## 5. Verdict (data-driven)

**Winner: Path B (LIOP, `SignalFusionEngine`) — wired as default behind the flag.**

Per the task's required metrics:

- **MCC:** B = −0.0119 > A = −0.0298 → **B wins** (both negative, B less harmful)
- **ECE:** B = 0.3389 < A = 0.4745 → **B wins** (materially better calibrated)
- **Brier:** B = 0.3628 < A = 0.4674 → **B wins**
- **baseΔ:** B = −14.65pt vs A = −14.71pt → **B marginally better** (both negative)
- **directional accuracy:** A = 51.25% vs B = 51.11% → A marginally higher, but A
  trades only 9,229 rows (72.7%) vs B's 11,909 (93.8%) — A achieves its edge by
  *not trading* the hard rows, which is the gate, not skill.
- **Sharpe:** B = 0.35 vs A = 0.40 → A marginally higher, both ≈0 vs null's 5.23.

### Honest meta-finding (must not be buried)

**No path has genuine predictive edge on the strike-crossing axis.** The down-skew null
("always down") beats every path on directional accuracy (65.63%) and Sharpe (5.23) by a
wide margin. Every fusion path has negative MCC and negative baseΔ; up-call precision is
~32–34% (at/below the 34.37% base-up rate, i.e. up-calls are not predictive). The fusion
is **less bad** than the agreement gate, not **good**.

UWC (`UncertaintyWeightedCascade`) is **non-functional** on this signal set: it predicts
"up" on 100% of rows (`PoolOfPools: no pools have active signals` — the 7 corrected
signals use short names that don't map to the `_signal`-suffixed pool taxonomy, and
pressure/frontal signals aren't in any pool). UWC degenerates to the mirror of the null
and must NOT be wired as default.

## 6. Wiring decision

- **Default flag value is `liop` (Path B),** decided on data. On the three
  calibration-quality metrics the task mandates (MCC, ECE, Brier), Path B is strictly
  better than Path A, so it is wired as the default. Fully reversible via
  `PAPER_FUSION_MODE=agreement_gate` (Path A) or `=uwc`.
- **UWC is explicitly NOT recommended** (degenerate on this set).
- **Escalation trigger for Dan:** this is a "less-bad" winner, not an edge. The real
  finding is that *neither* fusion path beats the down-skew null on the strike-crossing
  axis. Recommend not treating LIOP as a solved edge; the negative-MCC result should be
  folded into the existing R/T-3 / R/R-1a "no genuine edge" body of evidence.

## 7. Files changed

- `core/paper_trading_engine.py` — added `FUSION_MODE` flag + `_fuse_path_b()` + branch
  in `generate_signals()`; added `defaultdict` import.
- `core/fusion_logic.py` — fixed `_logger` + `n_signals` unbound-local bugs in
  `SignalFusionEngine.fuse_signals`.
- `scripts/rt8f_fusion_ab.py` — the A/B harness (script-only, deterministic).
- `reports/rt8f_fusion_ab_20260921-*.json` — emitted metrics + verdict.

## 8. Receipt

- `commit=409ec58ab`
- `clean=409ec58ab` (working tree clean of R/T-8F files; see note on sibling file below)
- No data mutation, no DB writes, no merge.

### Note on sibling untracked file
`scripts/rt8b_exhaustive_recalibration.py` was already present in the working tree
before this task began (a R/T-8b sibling task's in-progress artifact). It is NOT
part of R/T-8F and was left untouched (not committed, not deleted) — flagging for the
orchestrator to reconcile with the R/T-8b owner.
