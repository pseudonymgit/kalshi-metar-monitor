# Weather Engine — P-Track Metrics & Phantom-Success Prevention (P3/P4/P5/R6)

**Date:** 2026-09-17 UTC
**Owner:** Ray Donovan (metrics provenance + runtime integrity)
**Repo:** `weather-engine-source`, branch `temp-sprint-branch` @ `04be98a04`
**Scope:** Documentation only. No code, config, data, or DB mutation performed. Read-only inspection of repo + control-register docs.
**Status:** FINDINGS RECORDED — this is a plan/spec doc; execution of P2/P4 metrics run and R4→R6 cron conversion remain gated (see stop conditions).

---

## 0. Summary

This document closes the *documentation* half of four P/R-track items that are "READY — can start documentation" per the control register (`WEATHER-ENGINE-ROADMAP-CONTROL-REGISTER-2026-09-11.md`):

| ID | Item | Register status | What this doc delivers |
|---|---|---|---|
| P3 | Separate paper/Kalshi/proxy result accounting | READY | §1 — the three result sources and the exact provenance mechanism that already exists in code |
| P4 | Full metrics report structure | READY | §2 — the required contents of a complete calibrated 90-day report |
| P5 | Reconcile legacy 0.449 Sharpe | READY | §3 — root-cause reconciliation against the LOOP 71% baselines |
| R6 | Phantom-success prevention | READY | §4 — the deterministic receipt pattern (exit code + machine-checkable artifact) |

These are *specification* closures, not runtime closures. The actual 90-day calibrated run (P2) and the cron conversions (R4/R6 execution) are still gated on runtime durability (R1–R3) and daemon signal coverage (M5/M7), exactly as the register holds them BLOCKED.

---

## 1. P3 — Separate paper / Kalshi / proxy result accounting

### 1.1 The three result sources are distinct and must never be pooled

The engine produces accuracy/P&L numbers from three **different ground-truth sources**. They answer different questions and have different confidence. They must be reported in separate buckets, never blended into one "accuracy" figure.

| Source | What it is | What it answers | Confidence |
|---|---|---|---|
| **Paper** (prediction-only) | Directional prediction vs. *assumed* settlement at trade time; includes unrealized MTM of open positions | "Did the model call the direction?" | Highest count, lowest trust — not confirmed against any external settlement |
| **Kalshi** (authoritative) | Prediction vs. **Kalshi finalized settlement** value | "Did we win the actual market?" | Authoritative but sparse (~17.58% coverage in the 09-06 sweep) |
| **Proxy** (settlement_epochs / GHCN) | Prediction vs. a *non-Kalshi* proxy for the settlement temperature (METAR-derived `settlement_epochs`, or GHCN daily summary) | "Did the model call the weather?" | Fills coverage gap, but is **not** the market; carries systematic bias risk |

### 1.2 The provenance mechanism already exists in code

The three-way source tagging is already implemented as `core/additive/historical_adapter.py` (the S4 "explicit proxy labeling" work). Every truth record carries a provenance tag:

```python
# core/additive/historical_adapter.py
PROV_KALSHI = "kalshi"
PROV_EPOCH  = "settlement_epochs"
PROV_GHCN   = "ghcn"

class TruthRecord:  # __slots__ = ("station", "date", "temp_f", "provenance")
    ...

# Load precedence: Kalshi > settlement_epochs > GHCN.
# Once a (station, date) pair is assigned, it is not overwritten.
def provenance_summary(truth) -> {"kalshi": N, "settlement_epochs": M, "ghcn": K}
```

**Precedence rule:** for any `(station, date)` pair, the highest-priority available source wins — Kalshi finalized settlements first, then METAR-derived settlement epochs, then GHCN. This means a pair is *never* double-counted across sources; the tag tells you exactly which source produced each number.

### 1.3 How each source is tracked distinctly (accounting contract)

1. **Paper P&L** — `core/pnl_tracking.py::daily_reconciliation()` writes **`paper_pnl`** (unrealized MTM + unsettled positions) as a *separate column* in `daily_balances`. It is explicitly commented as "PAPER P&L — unconfirmed against settlement data."
2. **Settlement-confirmed P&L** — the same function writes **`settlement_confirmed_pnl`** (only realized P&L from trades that actually settled against Kalshi). Also a distinct column.
3. **Settlement accuracy** — `core/paper_test_controller.py` (D3) maintains a dedicated `settlement_accuracy` table (`was_correct` against settlement outcome) with a `get_settlement_accuracy()` query, cleanly separated from pre-settlement prediction accuracy.
4. **Proxy truth** — `core/additive/historical_adapter.py` tags every `(station, date)` truth record with `kalshi` / `settlement_epochs` / `ghcn`, and `provenance_summary()` reports the per-source counts.

**Gap to close at execution time (P2 run):** the 09-06 sweep reported "71.82% paper" and "71.46% settlement" but did **not** publish a proxy-sourced (settlement_epochs/GHCN) accuracy bucket as a third line. The calibrated 90-day rerun must emit all three buckets side-by-side (§2.3), with the `provenance_summary` counts appended so the reader can see what fraction of each number rests on authoritative vs. proxy truth.

### 1.4 Anti-pattern (explicitly prohibited)

- **Never** report a single blended "accuracy" that mixes paper, Kalshi, and proxy rows.
- **Never** let a proxy-sourced row silently inherit the "settlement accuracy" label. Proxy ≠ settlement. The register rule 4 already encodes this: *"Historical/proxy evidence is never silently represented as live/Kalshi evidence."*

---

## 2. P4 — Full metrics report structure (complete calibrated 90-day report)

A complete, calibrated, 90-day report must contain every block below. Each block is mandatory; omitting one means the report is not "full."

### 2.1 Header / provenance block
- Run ID, code commit SHA, branch, data-cutoff date, calibration cutoff date, run timestamp.
- Frozen parameter grid reference (B2.3 preregistered grid).
- Lineage reference: which code/config/data lineage was frozen (P1 `P1-LINEAGE-FREEZE-2026-09-17.md`).
- Leakage statement: walk-forward split boundary, no train/test overlap.

### 2.2 Three-source accuracy block (P3 — mandatory, separate rows)
| Metric | Paper | Kalshi-settled | Proxy (settlement_epochs) | Proxy (GHCN) |
|---|---|---|---|---|
| Directional accuracy % | … | … | … | … |
| Trade count | … | … | … | … |
| Coverage % of eligible days | … | … | … | … |
| Mean confidence | … | … | … | … |

Plus a `provenance_summary` line: total `(station,date)` pairs per source.

### 2.3 Risk-adjusted performance block
- **Sharpe** — with explicit formula and annualization basis (see §3.1; must state whether `sqrt(252)` daily or per-trade, and the risk-free assumption).
- Sortino, max drawdown (%), Calmar (if applicable).
- Per-station Sharpe (identify tradable vs. zero-skill stations — register P1.5/Master-task P1.2).

### 2.4 Calibration block
- Brier score.
- ECE (expected calibration error) — the 09-06 value was 0.225 (overconfident).
- Reliability curve summary / per-confidence-bin counts.
- Calibrated-vs-raw A/B (does calibration lift realized Sharpe?).

### 2.5 Signal-level block
- Per-signal accuracy, trade count, coverage, confidence — top and bottom signals named (e.g. cooling_failure 76.25% vs. rdae_mos 58.64% from the 09-06 sweep).

### 2.6 Cost / execution block
- Fees (round-trip), slippage assumptions, spread cost.
- Cost-adjusted P&L (the `cost_adjusted_pnl` column) vs. raw P&L.

### 2.7 Baselines block (for skill vs. luck)
- ERA5 persistence baseline (56.6% per roadmap P0.2) — the LOOP must beat this.
- Kalshi noise floor (22.3%).
- Binomial significance (z / p-value) for the accuracy edge.
- Nested CV / walk-forward stability across folds.

### 2.8 Go/park/kill gate block (P6 input)
- A single disposition line per signal/station: ADVANCE / PARK / KILL, with the metric that drives it.
- Sharpe ≥ 1.0 binding-gate status (Gray Room threshold), or a justified alternative.

---

## 3. P5 — Reconciling the legacy 0.449 Sharpe

### 3.1 The two numbers are measuring different things

The apparent "contradiction" — *71.82% paper / 71.46% settlement accuracy but only a 0.449 Sharpe* — is **not** a contradiction. They are two different quantities:

- **71.82% / 71.46%** is **raw directional accuracy** — the fraction of predictions that pointed the correct direction.
- **0.449** is an **annualized, risk-adjusted return ratio** — it is *not* accuracy and is *not* on a 0–100 scale.

The `core/multi_model_ensemble.py` backtest computes a "Sharpe-like" metric from **win/loss direction** rather than P&L:

```python
# core/multi_model_ensemble.py
wins = [1 if p == a else 0 for p, a, c in all_results]
mean_w = sum(wins) / len(wins)
std_w  = sqrt(var_w) if var_w > 0 else 0.01
sharpe = (mean_w - 0.5) / std_w * math.sqrt(252)   # annualize by sqrt(252)
```

`core/pnl_tracking.py::compute_sharpe()` uses a different, **per-trade P&L** definition (return = `pnl / position_size`, mean/std, no annualization). These two implementations are not the same metric, which is itself a provenance hazard — the report must state which formula produced any quoted Sharpe.

### 3.2 Root-cause of the low figure (four compounding factors)

1. **A 71% win rate is a thin edge, not a big Sharpe.** At binary ~even-money odds, a 71.5% win rate corresponds to a per-trade expected return on the order of ~40% gross, but the *variance* of a binomial bet is large. Sharpe ≈ `(p − 0.5) / sqrt(p(1−p))` annualized — for p≈0.715 this is roughly **0.5 in per-trade terms before costs and confidence weighting**, not a 1.0+ number. High accuracy does not automatically imply high Sharpe.

2. **Annualization on a 30-day window is unstable.** The 09-06 sweep was a **30-day** walk-forward. `sqrt(252)` annualizes ~30 daily observations, which amplifies sampling noise. The roadmap itself notes "a 90-day window would stabilize the metric." This is explicitly called a *data-volume* issue, not a code bug.

3. **Overconfidence corrupts the confidence-weighted P&L.** ECE = 0.225 and Brier = 0.266 mean the model's reported confidence exceeds its realized accuracy. Any P&L or Kelly sizing that multiplies by confidence overweights losing bets, dragging realized risk-adjusted return below what raw accuracy suggests.

4. **Fees/spread drag.** Real Sharpe is net of round-trip fees and spread (`ROUND_TRIP_FEE` in `core/market_cost_model.py`); the accuracy figure is gross of costs. Cost-adjusted P&L is strictly lower than raw.

### 3.3 Reconciliation statement (authoritative)

> **The 0.449 Sharpe is NOT in conflict with 71.82% paper / 71.46% settlement accuracy.** 71.x% is raw directional accuracy; 0.449 is an annualized risk-adjusted return. A 71% win rate at binary odds is a real but *thin* edge (≈0.5 per-trade Sharpe before costs), and the 30-day window + ECE 0.225 overconfidence + fee drag compress it to 0.449 when annualized by `sqrt(252)`. The figure is the **binding** Gray Room gate (Sharpe ≥ 1.0) and is currently **below** it, which is why paper-trading authorization (Ω7/Ω11) remains BLOCKED.

### 3.4 Path to resolution (what a calibrated 90-day rerun must show)
- Recompute Sharpe on the **90-day** window (stabilizes annualization).
- Apply calibration (reduce ECE from 0.225) — the roadmap's stated lever for lifting realized Sharpe.
- Report Sharpe **net of fees/spread**, and state the formula (annualized-vs-per-trade) explicitly.
- Do **not** treat the accuracy numbers as the gate; the gate is risk-adjusted return.

---

## 4. R6 — Phantom-success prevention (deterministic receipt pattern)

### 4.1 The failure mode being prevented

From `WEATHER-ENGINE-WIPE-RECOVERY-2026-09-09.md`: operational scripts were deleted while their crons survived, and **LLM agent-turn crons reported "successful turn" even when the underlying script was missing or failed.** "Success" was a model's narrative, not a real exit code.

### 4.2 The deterministic receipt pattern (the contract)

Every script-backed cron must produce a **machine-checkable success receipt**, and the receipt — not any narrative — is the success signal. The pattern has four mandatory parts:

1. **Pre-run dependency check (fail the job).** The script's first action verifies every declared dependency (input files, DB tables, upstream artifacts) exists and is within its freshness window. On failure → **exit non-zero**, core work does not run. (Control C2.)

2. **Core work + post-run artifact receipt.** After the core work, emit a machine-checkable artifact: row-count written, last-write timestamp, output artifact path/checksum, and exit code. (Control C3.)

3. **Fail-closed exit semantics.** Non-zero exit = failure, propagated to the scheduler. No cron may mask a failed/missing script behind an "agent turn succeeded" result. (Control C5.)

4. **Git-tracked script + supervisor unit.** The script is committed; no cron references an untracked script. (Control C4.)

The **success signal is the exit code + the receipt artifact**, never an LLM's prose. Per the cron policy (`WEATHER-ENGINE-CRON-POLICY-2026-09-14.md` §2.1 corollary 4): *"'Report whether the script succeeded' is never a valid reason to use an LLM."*

### 4.3 What R4 already started proving

R4 (convert pure-script crons from agentTurn) is the *mechanism*; R6 is the *verification* of that mechanism. The policy already mandates the exact proof R6 requires:

> "Verification of each conversion: the cron must demonstrate, on its next run, **(a) a real non-zero exit when a dependency is removed**, and **(b) a machine-checkable success receipt when healthy.** Both receipts are recorded per R6."

So the R6 acceptance test is two deterministic receipts per converted cron:
- **(a) Negative receipt** — remove a declared dependency, observe a genuine non-zero exit (proof the check is not a no-op).
- **(b) Positive receipt** — restore the dependency, observe exit 0 **plus** the artifact receipt (row-count/checksum/timestamp).

### 4.4 What R6 completion still requires (execution, not documentation)

| Requirement | State |
|---|---|
| Dependency/freshness manifest (C1) | R5 — RECEIPT-ONLY |
| Convert `D`-classified crons to scripts (C2–C5) | R4 — READY (gated on runtime stability) |
| Negative + positive receipt per cron | R6 — READY |
| Weekly cron-vs-filesystem reconciliation (C6) | E5 — READY |

The `D`-classified cron inventory (z8, z5-asos, kalshi-settlement-upkeep, auth-verification, GEFS, tigge, NWP z6/z7, weatherapi) is enumerated in the cron policy §2.3 with an ordered conversion plan §5. R6 is **complete only when every converted cron has produced both the negative and positive receipt**, and the reconciliation job has run clean.

---

## 5. Stop conditions & verification

**Write scope honored:** only this doc under `docs/plans/` was created. No code, config, data, or DB files were modified. No backtests, no paper/live trading, no daemon restart, no DB writes were performed.

**Still BLOCKED / gated (unchanged from register):**
- P2 (leakage-safe 90-day walk-forward) — BLOCKED on calibration/truth.
- P4 full metrics run — READY, after P2/P3 execution.
- P5 numeric resolution — requires the 90-day calibrated Sharpe, not just this reconciliation.
- R4/R6 cron conversion — READY, after runtime stability (R1–R3).

**Next action:** Ray holds this as the P-track/R6 documentation closure; execution proceeds only after R1–R3 runtime receipts and M5/M7 daemon signal coverage are evidenced. Gerri/Gilfoyle/Dan retain approval authority over any metric that feeds a go/park/kill or paper-trading gate.

---

## 6. Key files referenced

| File | Role |
|---|---|
| `core/additive/historical_adapter.py` | Three-source provenance tagging (`kalshi`/`settlement_epochs`/`ghcn`) — S4/P3 foundation |
| `core/pnl_tracking.py` | Separate `paper_pnl` vs `settlement_confirmed_pnl` columns; `compute_sharpe()` |
| `core/paper_test_controller.py` | Settlement-confirmed accuracy (D3) `settlement_accuracy` table |
| `core/multi_model_ensemble.py` | "Sharpe-like" formula `(mean_w − 0.5)/std_w × sqrt(252)` |
| `core/paper_trader.py` | `get_sharpe()` wrapper |
| `docs/plans/ALL-INCLUSIVE-ROADMAP-100PCT-2026-09-06.md` | Source of 71.82% / 71.46% / Sharpe 0.449 / ECE 0.225 / Brier 0.266 |
| `docs/plans/WEATHER-ENGINE-ROADMAP-CONTROL-REGISTER-2026-09-11.md` | P3–P6, R4–R6 definitions + statuses |
| `docs/plans/WEATHER-ENGINE-CRON-POLICY-2026-09-14.md` | R6 receipt pattern + C1–C6 controls |
