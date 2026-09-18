# Docs State Index — 2026-09-18

**Purpose:** Prevent progress loss while `temp-sprint-branch` carries a large untracked working tree. This index catalogs what exists, what's active, and what's scaffolding. Non-destructive — nothing deleted or committed here.

**Repo:** `prototypes/weather-engine-source` · **Branch:** `temp-sprint-branch` · **Status:** dirty (24 untracked plan docs + 25 graphify snapshot dirs + 40MB reports, mostly 9/17–9/18).

---

## Active work (9/17–9/18) — the progress we must not lose

| Doc | Purpose |
|---|---|
| `SETTLEMENT-EPOCHS-REWRITE-PLAN-2026-09-17.md` | Plan for repairing corrupt ground-truth labels |
| `SETTLEMENT-EPOCHS-REWRITE-EXECUTED-2026-09-17.md` | Executed rewrite: 625 corrupt → 0 residual |
| `SETTLEMENT-EPOCHS-RECOVERY-2026-09-17.md` | Recovery ledger for the label repair |
| `KILLED-SIGNAL-REVIEW-2026-09-18.md` | Re-review of killed signals against clean labels (3 NEEDS-RETEST / 3 KEEP-KILLED) |
| `CLEAN-LABEL-SIGNAL-EVAL-2026-09-18.md` | Clean-label signal evaluation |
| `SIGNAL-REGISTRY-RECONCILIATION-2026-09-17.md` | Registry vs taxonomy reconciliation (12 wired vs 48 tracked) |
| `M-CALIBRATION-EXECUTION-2026-09-17.md` | Calibration execution |
| `INTRADAY-PATH-CALIBRATION-2026-09-17.md` | Intraday calibration path |
| `S5-SOURCE-RECONCILIATION-2026-09-17.md` | Source reconciliation |
| `S6-GHCN-PROXY-DISPOSITION-2026-09-17.md` | GHCN proxy disposition |
| `MARKET-COVERAGE-Z8-FIX-2026-09-17.md` | Market coverage (Z8 cron) fix |
| `INGESTION-COVERAGE-MANIFEST-2026-09-17.md` | Ingestion coverage manifest |
| `WE-COMPLETENESS-AUDIT-2026-09-17.md` | Completeness audit |
| `RECOVERY-100PCT-LEDGER-2026-09-18.md` | 100% recovery ledger |
| `P-METRICS-AND-PHANTOM-SUCCESS-2026-09-17.md` | Metrics + phantom-success analysis |
| `H-MARKET-REALITY-2026-09-17.md` | Market reality check |
| `H1-H2-PREREGISTRATION-2026-09-17.md` | Preregistration |
| `P1-LINEAGE-FREEZE-2026-09-17.md` | Lineage freeze |
| `B2-PARAM-GRID-FREEZE-2026-09-17.md` | Parameter grid freeze |
| `E9-E10-SIGMA-TESTS-RECEIPT-2026-09-17.md` | Sigma tests receipt |
| `RUNTIME-HYGIENE-2026-09-17.md` | Runtime hygiene |
| `METAR-MAXTEMP-PARSER-FIX-2026-09-17.md` | METAR max_temp parser fix |

---

## Scaffolding (non-deliverable — safe to gitignore or archive, NOT product)

- `graphify-out-*/` and `graphify-snapshot*/` directories — **25 of them**, all graph/visualization snapshots. These are output artifacts, not source. Do not commit as deliverable; they bloat the tree and bury real work.
- `reports/` — 40MB of generated JSON backtest/report outputs. Generated artifacts; keep the source scripts, archive or gitignore the outputs.
- `audit-2026-09-02/`, `.graphify-snapshots/`, `snapshots/`, `artifacts/` — same category.

---

## Recommended disposition (for Dan's review — no action taken here)

1. **Commit the 9/17–9/18 plan docs** — they are real progress and currently untracked (at risk of loss).
2. **Gitignore** the graphify snapshot dirs + `reports/` generated outputs.
3. **Do NOT delete** any `docs/plans/*.md` — they are the record of the settlement-epochs rewrite and killed-signal review.

---

## Pending (in flight)

- **Gilfoyle:** inversion backtest (nwp_analog, spike_reversion, corrected_pressure_delta) + calibration cell-coverage audit → `docs/plans/INVERSION-BACKTEST-2026-09-18.md` (expected).
- **Dan decision gate:** any signal revival after clean-label retest. No revival without Dan sign-off.
