# GR5-WE-001 — Look-200 Measurement Battery (PROVISIONAL)

**Date:** 2026-10-12 UTC · **Owner:** Bertram Gilfoyle (CTIO) · **Repo:** `prototypes/weather-engine-source/`
**Contract:** `artifacts/preregistration/GR5-WE-001.json` sha256 `1c81997965f7eb87…` (frozen 2026-10-10T00:35:30Z)
**Run:** `scripts/look200_measurement.py --json` (read-only on the book) · **Status:** **PROVISIONAL — N=68 < 200**

> This is the first pre-registered measurement checkpoint. **Nothing here is conclusive:
> n=68 sits below both the pre-registered look-200 and the primary `min_trades=100`.**
> Per the pre-registration (fixed-N, `no_optional_stopping=true`, O'Brien–Fleming looks
> 200/400/640), a sub-look read-out **cannot** pass, cannot halt, and cannot be promoted.
> It is a trajectory reading against the frozen metric family, nothing more.

---

## 0) Prereg contract verification (done first)

| Check | Result |
|---|---|
| Artifact + SHA-256 sidecar present | ✅ `artifacts/preregistration/GR5-WE-001.json` + `.sha256` |
| Sidecar hash == artifact hash | ✅ `1c81997965f7eb8720d2bac59d58384f71b2f9ef311b9e8c7b4ecb74a21143b3` |
| `repo_git_sha` in artifact | `ea77e1648f6c66a0861defcaf8d4795e6f93bcc6` (base for the freeze) |
| Analysis-code combined hash matches live tree | ✅ `e68c74e1…b39fe` |
| All 4 frozen modules byte-identical | ✅ `core/unified_backtest.py`, `core/calibrate_all_signals.py`, `core/intraday/calibration.py`, `scripts/run_calibration_pipeline.py` |
| **Does the look-200 analysis code match the frozen hash?** | **YES — but the frozen hash does not cover the code that actually produces this battery.** See finding **F-3** below. |

**What the artifact pre-specifies (exact):**

- **Axis:** `strike_crossing`.
- **Primary metric:** `net_ev_per_accepted_trade`, thresholds `min_net_ev=0.0`, `min_accuracy=0.58`, `min_confidence=0.65`, `min_trades=100`, `min_sharpe=1.0`.
- **Secondary:** `directional_accuracy ≥0.58`, `brier_skill_score >0.0`, `sharpe ≥1.0`, `expectancy_per_trade >0.0`, `calibration_ece ≤0.05`.
- **Cohort:** N=640 (20 stations, HIGH/LOW); pooled claim needs `n_eff≥320`; cell claim needs 48 raw + `n_eff≥24` + ≥3 dates.
- **Stopping:** O'Brien–Fleming looks **200/400/640** at z `3.00/2.60/2.20`; `no_optional_stopping=true`.
- **Canaries:** C1–C5, `master_seed=20261010`, `α_canary=0.01`, `cusum_halt_sigma=5.0`.
- **Bootstrap:** 10,000 reps; cluster keys `(station, market_type, settlement_date)`.
- **Fee:** `ceil(0.07·P·(1−P)·100)/100` per contract.
- **Frozen exclusions:** `settlement_confirmed!=1`, bad outcome label, `label_axis!=strike_crossing`, as-of (G5) violation, duplicate `(station,market_type,settlement_date)`, fail-open 0.5 row.
- **Falsification/stopping (workspace `scripts/stopping_record.py`):** `A1` falsified · `A2` unmeasurable · **`A3` hurdle = market-relative upper 95% bound on net-of-cost EV ≤ 0 over ≥200 events** · `A4` governance breach. Only `ACTIVE-BOUNDED` permits a run; root sunset 2026-12-31.

**Existence caveat:** `GR5-WE-001.json` freezes the cohort, thresholds, analysis-code hash, and seeds — but it contains **no `looks` analysis definition, no East/West field, and no explicit alpha-on-the-live-metric**. The look-200 analysis definition is the *stated stopping rule + metric family*; the canary detail is the **GR5 synthesis spec** (`docs/plans/GRAY-ROOM-5-SYNTHESIS-2026-10-09.md` §3–4). That is a gap in "the prereg pre-specifies the look-200 analysis definition," not a mismatch.

---

## 1) Measurement battery — n=68 usable settled trades

Cohort = `settlement_confirmed=1 AND canonical_outcome IS NOT NULL`, 68 rows, 47,427 contracts, 24 distinct settlement dates.

### 1a. Accuracy on the strike-crossing axis

- **Accuracy = 0.676 (46/68)**, Wilson 95% CI **[0.558, 0.776]**.
- Long-YES rows (67/68): **0.672 (45/67)**, CI **[0.552, 0.772]** — the honest primary.
- Raw strict-`>` axis cross-check: **68/68 agree** with `canonical_outcome` (the one NO-side row is handled direction-aware: DOWN vs strike 87 → ITM DOWN → `correct`; `realized_pnl=+qty·price` is the legitimate winning-NO payoff). **No anomalies.**
- `min_accuracy=0.58` **cleared on point estimate only**; the CI lower bound 0.558 dips below 0.58 → not yet distinguishable from the floor.

### 1b. Sharpe (per-trade — NOT bankroll-compounded)

| Basis | Sharpe | Note |
|---|---|---|
| Per **contract** | **1.034** | `min_sharpe=1.0` barely cleared |
| Per **trade (USD)** | **0.807** | fails `min_sharpe=1.0`; per-trade USD weights the $50 fixed sizing |

**Both are un-compounded per-row Sharpe (μ/σ over rows).** The per-trade figure is dominated by position-size variance and is the wrong basis for the threshold; the **per-contract** figure is the fair read and is **1.03 — marginal**.

### 1c. Net-EV net of the canonical fee

| | Value |
|---|---|
| Gross P&L (book `realized_pnl`) | **$33,811.80** |
| Canonical fee charged | **−$485.22** (1.4% of gross — fees are small at these far-ITM prices) |
| Net P&L | **$33,326.58** |
| Mean **gross** /contract | **$0.625** |
| Mean **net** /contract (net-EV) | **$0.538** |
| Mean net /trade | **$490.10** (std $607.56) |

`min_net_ev=0.0` cleared **gross and net**.

### 1d. Block-bootstrap CI on mean per-trade net P&L (blocks = settlement date, 10,000 reps)

- **Mean net /trade $490.10, 95% CI [$314.79, $664.06]** — entirely >0, but wide (±$175).
- Mean net /contract $0.538, 95% CI **[$0.403, $0.664]**.
- **Caveat:** block resampling over 24 dates is barely bootstrappable; one dominant date (KATL `2026-08-02`) can move the mean. CI width is the honest signal here.

### 1e. A3 hurdle read-out

- Metric `net_ev_per_accepted_trade`; events **68**; `min_events=200`.
- **`HURDLE_MIN_EVENTS=200` → A3 cannot fire at n=68** (guarded in `stopping_record.py`: only evaluates when `events≥200`).
- Trajectory only: one-sided 95% upper bound on mean net-EV/contract = **+$0.641** (current point estimate 0.538). Would need ≥200 events for a real read.

---

## 2) Canary battery C1–C5 + CUSUM (frozen isolated harness)

Run via `scripts/run_canaries.py` **and** the measurement script (identical results after pinning row order — see F-4). `min_n=200`.

| Canary | n | mean net-EV | z | p | CUSUM | Verdict |
|---|---|---|---|---|---|---|
| **C1** random-signal | 68 | 0.159 | 1.78 | **0.076** | 0.62 | **null ✅** (coin-flip cannot beat fees) |
| **C2** mis-axed | 68 | — | — | 1.0 | — | **REJECTED** (structurally disabled) ✅ |
| **C3** climatology | 16 | 0.796 | 8.56 | ~0 | **26.23σ** | **SIG + CUSUM-HALT (PROVISIONAL, n=16)** ⚠️ |
| **C4** shuffled-label | 68 | 0.376 | 6.36 | ~0 | **12.57σ** | **SIG + CUSUM-HALT (PROVISIONAL, n=68)** ⚠️ |
| **C5** fee-zero | 68 | 0.550 | 8.74 | ~0 | **38.95σ** | **SIG + CUSUM-HALT (PROVISIONAL, n=68)** ⚠️ |

- **No HALT fires** (all firings are `PROVISIONAL`, n<200). The harness `min_n` guard does its job.
- **Which fire:** C3, C4, C5 — on **HT1** (significant on the profitable side) **and HT2** (CUSUM > 5σ).
- **C1 and C2 behave exactly as a correct pipeline demands** (null / rejected). This is real evidence the measurement path is not obviously broken.
- **C4 at 12.6σ and C5 at 38.9σ are not noise.** C5's ~40σ specifically flags fee-fragility; C4 says block-preserving label shuffling still "beats fees" — a lookahead/dedup-leak shape. Both must be read as **warnings whose confirmable form only arrives at n≥200**, where they would **HALT** (HT1) if they persist. Do not dismiss them as merely provisional.

---

## 3) Verdict vs the prereg criteria

**Prereg says look-200 must show** (to continue): `net_ev_per_accepted_trade` with **n≥200** and `min_trades=100`; O'Brien–Fleming z-bounds 3.00/2.60/2.20 at looks 200/400/640; canary nulls and |CUSUM|<5σ at real min_n; and (A3) a market-relative upper-95%-bound net-EV **>0 over ≥200 events**.

**Where we stand (n=68):**

| Criterion | Threshold | Current | Cleared? |
|---|---|---|---|
| n (events) | ≥200 to look | **68** | ❌ — **the look has not been reached** |
| min_trades | 100 | 68 | ❌ |
| accuracy | ≥0.58 | 0.676 (CI 0.558–0.776) | ~ (point ≥ floor; CI dips) |
| Sharpe (per-contract) | ≥1.0 | **1.034** | ~ marginal |
| net-EV/trade | >0 | +$0.538/contract (CI [0.403, 0.664]) | ✅ gross & net |
| canaries null @ n≥200 | none fire | C3/C4/C5 fire, **all PROVISIONAL** | ⏳ indeterminate |
| A3 hurdle (≥200 ev) | upper-95 ≤0 fails | cannot evaluate | ⏳ |

**Bottom line: no verdict is available.** We are 34% of the way to the first look. Accuracy and Sharpe point **toward** the thresholds, net-EV is positive with a fully-positive CI, and the two most fragile canaries are not yet formally meaningful — but **68 < 200 means every number above is a trajectory reading, not a result.** The pre-registration forbids treating it otherwise.

---

## 4) F1 follow-up: direction, East/West, per-station weakness

**Win rate by direction** (canonical outcome):

| Direction | n | correct | accuracy |
|---|---|---|---|
| UP (long YES) | 67 | 45 | 0.672 |
| DOWN (long NO) | 1 | 1 | 1.000 |

The book is a **one-direction sample** — the DOWN lane has exactly one trade (id 246). Directional attribution is not possible at this n.

**East/West accuracy split** (100th-meridian split; East = NE/SE/SC/MW, West = RW/PAC):

| Region | n | correct | accuracy | 95% Wilson CI |
|---|---|---|---|---|
| **East** | 49 | 40 | **0.816** | **[0.686, 0.900]** |
| **West** | 19 | 6 | **0.316** | **[0.154, 0.540]** |

**The regions are cleanly separated — the East CI [0.686, 0.900] and West CI [0.154, 0.540] do not overlap.** This is the single most notable substantive finding: the pooled 0.676 is an average of a **working East signal and a failing West signal**, and West (KDEN 0.30, KSEA 0.00, KLAX 0.00, KSFO 0.50, KPHX 0.50) sits **below the 0.58 floor**. With n_per_region = 49/19 these CIs are wide and provisional, but non-overlap at this n warrants a dedicated West post-mortem.

**Anomalously weak stations** (raw, small-n — flagged, not concluded):

| Station | Region | n | accuracy |
|---|---|---|---|
| KDEN | West | 10 | **0.30** |
| KSFO | West | 4 | 0.50 |
| KSEA | West | 2 | 0.00 |
| KNYC | East | 1 | 0.00 |
| KLAX | West | 1 | 0.00 |
| KMIA / KMSP / KPHX | mixed | 2 each | 0.50 |

- **KDEN (n=10, 0.30, 10 distinct dates)** is the weakest station with enough rows to read and should carry the first West investigation.
- **Strongest:** KBOS 1.00 (n=8), KATL 0.92 (n=12), KAUS/KDFW 0.80 (n=5 each).
- **Coverage caveat (F1 in the spec):** only **24 distinct settlement dates** over ~2 months (2026-08-02 → 2026-10-09). The spec wants ≥30 dates over ≥3 months for promotion. **The East edge may be episode-specific** — the same environmental regime, not independent evidence.

---

## 5) Material findings to carry forward (contract + code)

These are defects/divergences in the **measurement substrate**, distinct from the provisional numbers:

**F-1 — A3 hurdle *formula* is undefined; `stopping_record.hurdle.events` is hand-maintained.**
`scripts/stopping_record.py` only *evaluates* A3 (`events≥200` and `upper_95_bound_net_ev≤0`); nothing computes the market-relative upper-95 bound, and `hurdle.events` is a literal `0` in the committed record. Look-200 was run against a **frozen contract that has no executable A3 metric builder**. The pre-registration freezes the metric *name*, not its computation.

**F-2 — the committed `stopping-record.json` is stale.** `generated_at_utc=2026-10-10T02:22:08Z`, `hurdle.events=0`, `governance.events=[]`, `canonical_ledger.frozen=false` — it predates the current book and does not reflect 68 measured events.

**F-3 — the frozen analysis-code hash does not cover the code that runs this battery.** `ANALYSIS_CODE_MODULES` freezes `core/unified_backtest.py`, `core/calibrate_all_signals.py`, `core/intraday/calibration.py`, `scripts/run_calibration_pipeline.py` — **none of which is the look-200/look-canary measurement path**. The actual path (`core/canary_harness.py`, `scripts/run_canaries.py`, `scripts/look200_measurement.py`) is **unhashed** and therefore **post-freeze mutable**. The compliance form of the question ("does the code match the frozen hash?") passes; the substantive form ("is the measured analysis frozen?") **does not**.

**F-4 — canary seeds are derived two different ways.** The freeze (`core/preregistration.canary_seeds`) uses `sha256("20261010:C1")`; the harness (`core.canary_harness.derive_seed`) uses `sha256("20261010:C1:")` (trailing `:`). For C1/C4 this produces **different RNG streams**: e.g. C1 mean 0.159 (harness) vs 0.233 (artifact seed) at n=68. Seeding must be reconciled to one derivation.

**F-5 — a canary path uses the wrong truth source.** `core/canary_harness.run_c2_misaxed`, `_base_rate_by_station_month`, and `run_c4_shuffled` key settlement on **`settlement_date`** and treat a date as a single settlement temp (`by_date[d][0].settlement_temp_f`), but a settlement date spans **many stations with different temps** (20 of 24 dates have >1 distinct temp). **C4's "block-preserving" shuffle is therefore not really block-preserving** — it shuffles per-date first-temps across stations. Any C4 conclusion is suspect until the block key is defined as `(station, settlement_date)`.

**F-6 — running order is not pinned in the frozen runner.** `scripts/run_canaries.build_canary_trades` has no `ORDER BY`, so the seeded C1/C4 RNG stream depends on SQLite row-return order (it returned a different order than `ORDER BY id`, changing C1/C4). Determinism requires an explicit sort.

---

## 6) Artifacts & reproduction

- Script: `scripts/look200_measurement.py` (read-only; no temp DBs).
- Machine-readable result: `artifacts/look200/GR5-WE-001-look200-2026-10-12.json`.
- Frozen canary runner cross-check: `python3 scripts/run_canaries.py --experiment-id GR5-WE-001`.
- Constraints honored: **no writes** to `trades`, `gate_ledger`, `trade_custody`, or the prereg artifact; nothing wired into live trading; `/tmp` scratch removed.

## 7) Recommended next actions

1. **Do not promote or halt.** 68 < 200; the look has not been reached. Continue collecting to n≥200 under the frozen contract.
2. **West post-mortem** (KDEN first): is the 0.30 West accuracy a signal defect, a settlement-truth defect, or a regime episode? Owner: Gilfoyle → Gerri if it is methodology.
3. **Repair the measurement substrate before the real look-200:** define/compute the A3 hurdle metric (F-1), refresh `stopping-record.json` (F-2), extend the frozen analysis hash to the actual measurement path (F-3), reconcile canary seed derivation (F-4), fix the C4 block key (F-5), pin runner ordering (F-6). A new experiment id (or an explicit amendment) is required for any change to frozen fields.
