# GR5 Workstream D — Measurement Quality Receipt

**Date:** 2026-10-10 UTC · **Owner:** Bertram Gilfoyle (CTIO) · **Repo:** `prototypes/weather-engine-source/`
**Base:** `c17d496b3` (main) · **Spec:** `docs/plans/GRAY-ROOM-5-SYNTHESIS-2026-10-09.md`
**Scope:** D1 TCR · D2 pre-registration · D3 canaries · pre-ingest gate composer (G0–G5).
**Constraint honored:** surgical; no fusion wiring; no live trading enabled.

---

## Commits (one per deliverable)

| Commit | Deliverable |
|---|---|
| `ea77e1648` | feat(D1): TradeCustodyRecord — append-only chain of custody (48 cols / 7 groups) |
| `5ebb633f5` | feat(D2): frozen pre-registration artifact + SHA-256 sidecar |
| `a2e78f1d0` | feat(D3): measurement canaries C1–C5 + CUSUM drift + halt wiring |
| `de852cef7` | feat(gate): finish pre-ingest composer G0–G5 (M6 core) |

Tests: `tests/test_trade_custody.py` (9), `tests/test_preregistration.py` (7),
`tests/test_canaries.py` (14), `tests/test_gate_composer.py` (11) → **41 passed**.
Structural gates unchanged: `verify_gate` exit 0; `substrate_gate` exit 4 (PROVISIONAL, pre-existing).

---

## D1 — TradeCustodyRecord (`core/trade_custody.py`)

**Before:** no chain of custody from snapshot → bridge → trade → settlement; a settled
P&L could not be traced to its inputs.
**After:** append-only `trade_custody`, exactly **48 columns in 7 groups**
(identity 9 / snapshot 7 / observation 5 / bridge 8 / decision 7 / settlement 7 / integrity 5).

- `BEFORE UPDATE` / `BEFORE DELETE` triggers `RAISE(ABORT)` → rows immutable.
- `idempotency_key = sha256(instance|market_ticker|decision_ts)` **UNIQUE**.
- `record_hash = sha256(prev_hash || canonical_json(payload))`; first row links to
  `GENESIS_HASH`; `verify_chain()` recomputes every link.
- `build_merkle_manifest()` → ndjson + manifest sidecar (Merkle root, `chain_ok`) + sha256s,
  for git commit **outside** the DB.
- `record_from_trade()` maps a recorded trade dict to a custody row. **Writer wiring is
  deliberately NOT done here** (touching the live bridge is out of scope for a
  measurement-only change); it is staged for a follow-up.

**Evidence:** insert row → hash 64 hex; UPDATE refused (`sqlite3.IntegrityError`);
DELETE refused; chain links (row2.prev == row1.hash); tamper → `verify_chain` reports
`record_hash mismatch`; manifest deterministic across two runs.

## D2 — Pre-registration artifact (`core/preregistration.py`)

**Before:** nothing fixed N, the stopping rule, or the metric before the first trade;
optional stopping was unguarded.
**After:** write-once `artifacts/preregistration/<experiment_id>.json` + `.sha256` sidecar.
Fields: repo git SHA, experiment id, freeze ts, cohort (N=640 / 20 stations / HIGH,LOW /
16-per-cell / 40 cells), `axis=strike_crossing`, canonical fee formula, primary + secondary
metrics & thresholds, cluster keys, bootstrap reps (10,000), **analysis-code hash**
(combined module-bytes hash), canary seeds (master + derived C1–C5, `α_canary=0.01`),
stopping rule (O'Brien–Fleming looks 200/400/640; `no_optional_stopping=true`), frozen
exclusions. `require_preregistration()` refuses analysis on a missing/tampered artifact.
`.gitignore` negated the broad `*.json` so the freeze is committable.

Frozen artifact shipped: `GR5-WE-001.json` sha256 `1c81997965f7eb87…`.

**Evidence:** freeze writes both files; second freeze raises (write-once); tamper `N`
640→100000 → verify raises `hash mismatch`; `require_preregistration` refuses.

## D3 — Canaries (`core/canary_harness.py`, `scripts/run_canaries.py`)

**Before:** no independent check that the pipeline measures signal, not noise.
**After:** 5 canaries on an **isolated code path** (no bridge imports, no scipy):

| Canary | Design | Result on real data |
|---|---|---|
| C1 random-signal | seeded coin flip, same stations/dates/quotes/fee | null (p=0.397) |
| C2 mis-axed | day-over-day axis | **REJECTED** (structurally disabled) |
| C3 climatology | static (station,month) floor, **leave-one-out** | null, n<min (thin cells) |
| C4 shuffled-label | settlement dates shuffled block-preserving | fires at n=29 → provisional |
| C5 fee-zero | same trades, fee=0 | fires at n=29 → provisional |

- One-sample z-test + two-sided **CUSUM on the independence unit** (per-settlement-date
  block means) — a per-row CUSUM accumulates on within-date correlation and false-halts.
- Halt triggers **HT1** (significant on the profitable side) / **HT2** (CUSUM > 5σ).
- `min_n` guard = first O'Brien–Fleming look (200): a sub-look firing is **PROVISIONAL**,
  never a premature HALT.

**Evidence:** real SBOX run (n=29) → exit **4 PROVISIONAL**, no premature halt; seeded
significant canary at n≥200 → exit **3 HALT**.

### Two real bugs found and fixed while building D3
1. **In-sample base-rate leakage (C3).** Computing the base rate on the same rows it
   scored made a singleton cell fit its own label and manufactured an edge. Fixed to a
   genuine leave-one-out estimate with a `min_prior=5` guard.
2. **CUSUM units.** Slack/threshold were tied to the standard error, so under a
   symmetric null the increments never got a negative drift and the monitor random-walked
   to a false halt (C4 hit 20σ on null data). Fixed to sample-sigma units on block means.

## Pre-ingest gate composer (`scripts/gate_composer.py`)

**Before:** G0–G5 existed as scattered modules; no single ordered admission filter.
**After:** one ordered filter, cheapest-first, **reusing** `core.fees`,
`core.observational_asof_gate`, and the frozen axis constant:
G0 suite-green (verify_gate + substrate_gate) · G1 provenance/version · G2 fee-canonical
(≤ $0.005) · G3 settlement-confirmed · G4 axis-correct · G5 as-of/no-lookahead.
Verdict `ADMITTED | REJECTED | HALT` → append-only `gate_ledger` (UPDATE/DELETE abort).
HALT reserved for window-level failures (G0, version mismatch).

**Evidence:** 11 tests; real G0 path runs verify_gate (0) + substrate_gate (4 PROVISIONAL,
allowed) → ADMITTED; self-check yields ADMITTED/REJECTED/REJECTED/REJECTED/HALT.

---

## Blockers / findings flagged

1. **Settlement truth is not confirmed in SBOX.** `trades.settlement_confirmed = 0` for
   all 33 rows. Independent truth from `kalshi_settlements` disagrees with the booked
   `settled_value` on **15 of 29** closed trades (> 3°F), and the 2026-10-08 rows resolve
   to implausible values (e.g. KBOS strike 75 → 57°F). G3 would **REJECT every** current
   SBOX row. This is a data/truth-source problem, not a code defect: the cohort cannot be
   measurement-quality until settlement truth is reconciled. **Recommend Gerri path:**
   reconcile `kalshi_settlements` vs booked values before the cohort counts.
2. **Canaries cannot run on this window.** At n=29 all firing canaries are provisional;
   C3 has n=0 (cells too thin). The canaries are correct but need N≥200 to certify — no
   measurement claim is made.
3. **TCR writer not wired.** `record_from_trade()` is ready but the bridge is untouched
   (deliberate, to keep this change measurement-only). Wiring `sbox_intraday_ev_bridge.py`
   to emit a custody row per trade is the next step; it is a live-path edit and should be
   its own reviewed change.
