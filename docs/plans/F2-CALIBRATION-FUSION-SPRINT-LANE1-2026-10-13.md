# F2 — Calibrated `p_fused` + Weighted Fusion + Market Component

**Date:** 2026-10-13
**Lane:** Sprint Lane 1 — Forecast (Signal Quality + Fusion)
**Owner:** Gilfoyle (technical lane)
**Scope:** read-only on the live book; DB writes additive only (new tables/columns;
`trades` never mutated).
**Spec:** `docs/plans/WEIGHTED-FUSION-SPEC-2026-10-13.md`
**Root cause:** `docs/plans/EXPERT-METHODOLOGY-REVIEW-2026-10-13.md` §0/§2.3 (E2 saturation)

---

## 0. Bottom line

`p_fused` saturation is **fixed at the mechanism level**. Replaying every stored
companion row through the new `fuse()` drops the `p_fused >= 0.99` population from
**49.7% → 0.0%** (and `>= 0.90` from **62.9% → 0.0%**), by (a) neutralizing the
trajectory `x1.05` CONFIRM amplifier once `p_core >= 0.90` and (b) clamping to a
per-station ceiling derived from the holdout Brier **resolution** (`0.0004` →
ceiling **0.752**).

One finding is surfaced rather than hidden: the spec's **`composite_conf`**
(weighted sum of component confidences, spec §2.3) **still saturates at 0.90+ for
73/143 rows**, because the *components themselves* saturate (`p_gefs`/`p_analog`
hit `1.0`; the analog CDF returns a hard `1.0` when `strike <= high_so_far`). The
F2 ceiling caps the *fused* value but does not de-saturate the *inputs*. Grading
from `composite_conf` as written would re-admit the same trades the ceiling is
meant to demote. **The clean de-saturation path is a calibrated component
`p` (EMOS/BMA), not a post-hoc cap on the fused value** — see §6.

The market component **is** wired (nearest mid to the 13:30 station-local pin) but
**does not dominate** (mean `|p_market - 0.5| = 0.19` vs component 0.65), so it
adds bounded information: equal-weight 4-component composite shifts the grade of
**35/82 trades (42.7%)** but moves the mean composite confidence only
`0.8237 → 0.8280`.

---

## 1. F2 fix — what changed

`core/intraday/multi_forecaster_fusion.py`:

1. **Amplifier guard.** `p_core >= CLIP_ZONE (0.90)` neutralizes `traj_mult = 1.05`
   → `1.0`. The amplifier can only push toward an unjustified ceiling, never add
   information. 85/143 stored rows hit this path.
2. **Calibrated ceiling.** New `calibrated_ceiling(resolution)` maps the holdout
   Brier resolution term to a per-station cap:

   ```
   frac      = min(1, resolution / RESOLUTION_FULL_SKILL)   # 0.05
   ceiling   = 0.75 + (0.99 - 0.75) * frac                  # floor 0.75, max 0.99
   ```

   With `holdout_metrics.brier_decomp_calibrated.resolution = 0.0004` the ceiling
   is **0.7519**. `p_fused` is clamped to `[1 - ceiling, ceiling]`.
   `ceiling=` is an override hook for a future per-station refit.
3. **New `calibrated_p` output.** `fuse()` now returns the clamped `calibrated_p`
   (the former hard clip at 0.99 is retained as `p_fused_raw` in the companion).
4. **Rule id bumped** to `fuse_v2:...;clamp<=calibrated_ceiling(resolution)`.

### Verification (task gate: ≤10% of trades at `p_fused >= 0.99`)

| Population | n | `>= 0.99` | `>= 0.90` |
|---|---:|---:|---:|
| Before (raw `p_fused`) | 143 | 71 (**49.7%**) | 90 (62.9%) |
| After (calibrated `p_fused`) | 143 | **0 (0.0%)** | **0 (0.0%)** |

**PASS** (0.0% ≤ 10%).

---

## 2. Weighted fusion — per-station weight store

New table `station_weights` (`PRIMARY KEY(station)`) exactly per spec §4, seeded
**equal (0.25 each)** for all 18 booked stations (`ensure_station_weights_defaults`).
`rolling_n` starts at 0; per spec §3.3 every station is below the `n >= 30` floor,
so equal weights **are** the shipping configuration until a refit runs.

`fuse(..., weights=..., use_weights=True, p_market=...)` computes:

```
p_core = sum_i w_i * p_i  /  sum_i w_i     over the components present
metric = |p_i - 0.5| * 2                   (component confidence, spec §2.3)
```

`p_market` is optional; when absent the market weight is renormalized out.
`disagree` keeps its explicit 0.5x shrink (the weighted path would otherwise not
reproduce the disagreement penalty).

**Two paths, deliberately distinct:**

- The **migration backfill** replays stored components through `fuse()` with the
  **legacy core blend** (`use_weights=False`), so `calibrated_p` differs from the
  stored `p_fused` *only* by the ceiling clamp + amplifier neutralization. That
  isolates the F2 calibration effect from the weighting change.
- The **live bridge** uses `use_weights=True` (equal weights), which replaces the
  agree-band `0.55/0.45` blend with the equal-weight mean over the components
  present (`0.25` each over 4, or renormalized over 3 without a market mid). This
  **is** a behaviour change at equal weights, not a no-op — the weights are the
  new mechanism. The legacy `0.55/0.45` agree blend is reproduced only on the
  `use_weights=False` path.

`load_weights(conn, station)` is the read path; the bridge consults it per station
(defaults to equal when no row exists).

---

## 3. Market mid as a fusion component

- Source: `data/kalshi_market_snapshots.db::market_snapshots.mid_price` (read-only).
- Pin: **13:30 station-local** (`DECISION_PIN_LOCAL_HHMM`), converted to UTC via
  `core.station_time` (e.g. KSEA → 20:30Z, KNYC → 17:30Z). This matches the
  canonical H3 pin (`scripts/b2_real_price_sweep.DEFAULT_ENTRY_UTC = "13:30:00"`,
  `GR5-WIRING-RECEIPT`).
- `market_mid_prob(ticker, pin)` = YES mid at the snapshot nearest the pin
  (`ORDER BY ABS(julianday(snap) - julianday(pin))`), fail-open (None → component
  drops out). The market's implied P(YES) is the mid; the NO side uses `1 - mid`
  via the existing `our_prob` leg convention.

### Does it change the composite?

Analysis: `scripts/analyze_market_fusion_component_2026-10-13.py` over all 82
booked `HIGH` trades (100% have a resolvable market mid).

| Metric | 3-component | 4-component (+ market) |
|---|---:|---:|
| mean composite confidence | 0.8237 | 0.8280 |
| grade S | 39 | 36 |
| grade A | 8 | 8 |
| grade B | 11 | 17 |
| grade C | 12 | 12 |
| grade F | 12 | 9 |
| **re-graded** | — | **35 / 82 = 42.7%** |

Mean component confidence is **0.65** (GEFS/analog/traj) vs **0.19** (market), so
the market vote is a soft prior. It re-grades ~43% of trades at the *composite*
level (mostly S→B, a few F→C) but does not flip the aggregate: the composite is
still component-dominated. Reading: as wired at equal weight the market is a mild
de-saturator that cannot, on its own, undo component saturation.

---

## 4. Schema + migration

All additive (`ensure_schema` in the fusion module; never mutates `trades`):

| Change | Table | Notes |
|---|---|---|
| `calibrated_p`, `p_market`, `composite_conf`, `grade`, `ceiling`, `weights_applied` | `trade_forecaster_probs` | per-trade calibration/provenance (F2 certificate on companion) |
| `station_weights(station PK, weight_gefs, weight_analog, weight_traj, weight_market, composite_conf, rolling_n, updated_at_utc)` | new | per-station weight store (spec §4) |
| `trade_calibration(...)` | new | independent calibration companion table (F2's "or a new calibration table") |

Migration **as a script, not a manual step**:
`scripts/migrate_fusion_calibration_2026-10-13.py`

```
python3 scripts/migrate_fusion_calibration_2026-10-13.py --report-only   # no write
python3 scripts/migrate_fusion_calibration_2026-10-13.py                # apply
```

It is idempotent and reverts nothing: it seeds equal weights, replays the stored
components through `fuse()`, and backfills `calibrated_p`/`composite_conf`/`grade`/
`ceiling` + `trade_calibration` (143 rows). `--report-only` opens the DB
`mode=ro` (zero writes, not even WAL mode).

**Post-migration invariants verified:** `trades` = 82 rows, 37 cols, `p_fused >=
0.99` count unchanged at 39 (untouched); `station_weights` = 18 rows;
`trade_calibration` = 143 rows.

---

## 5. Grade distribution — three candidate ladders

| Grade | raw `p_fused` (booked today) | calibrated `p_fused` | `composite_conf` (spec §2.3) |
|---|---:|---:|---:|
| S | 90 | 0 | 73 |
| A | 23 | 0 | 12 |
| B | 10 | 123 | 20 |
| C | 1 | 1 | 18 |
| F | 19 | 19 | 20 |

- Raw `p_fused`: 90 **S** grades that are not earned (the saturation bug).
- Calibrated `p_fused`: caps everything at B (ceiling 0.752) — no trade can be
  graded above B on this data. Honest but coarse.
- `composite_conf`: **still 73 S** ⇒ confirms §0: component saturation flows
  straight through the spec's composite-confidence formula.

This is the actionable finding: **do not grade on `composite_conf` until the
component probabilities are calibrated.** The ceiling is the right *stop*, but the
*grade* needs a calibrated input, not a capped sum of saturated parts.

---

## 6. Recommendation / next

1. **Ship the ceiling + amplifier guard** (done). It directly enforces the F2
   gate (`<=10%` at 0.99 → 0.0%).
2. **Keep the market component at equal weight** but treat `p_market` as a
   *benchmark* first: it re-grades 43% at the margin without adding a dominant
   vote. Revisit weight only after a pre-gate candidate population exists (spec §3
   open Q2 — that table does not exist; tuning on booked trades is censored).
3. **De-saturate the components.** The remaining saturation is upstream
   (`p_gefs`/`p_analog` → 1.0). EMOS/BMA (`core/bma_emos.py`, exists, unused) or a
   Platt fit on the pre-gate population is the fix. Until then `composite_conf`
   is not a usable grade input.
4. **Refit hook is live:** pass a per-station `resolution` (or `ceiling=`) to
   `calibrated_ceiling`/`fuse` once component calibration lands.

---

## 7. Artifacts

- `core/intraday/multi_forecaster_fusion.py` — F2 + weighted fusion + schema
- `scripts/sbox_intraday_ev_bridge.py` — pin, market component, per-station
  weights, companion wiring
- `scripts/migrate_fusion_calibration_2026-10-13.py` — additive migration/backfill
- `scripts/analyze_market_fusion_component_2026-10-13.py` — market-component effect
- `tests/test_fusion_calibration.py` — 16 unit tests (ceiling, amplifier guard,
  weighted blend, composite confidence, grade ladder)
