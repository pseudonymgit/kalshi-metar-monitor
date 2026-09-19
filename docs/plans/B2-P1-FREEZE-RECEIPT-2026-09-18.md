# B2 + P1 — Freeze/Preregister Receipt & Register Update

**Date:** 2026-09-18 UTC
**Author:** Gilfoyle subagent (B2 market thresholds + P1 performance lineage freeze workstream)
**Repo:** `prototypes/weather-engine-source`, branch `temp-sprint-branch`
**Scope:** Read-only verification + register update. No code/config/data mutation, no commit, no live/paper trading. One new doc created (this file), staged for Dan's merge.
**Task:** Execute B2 (market thresholds) + P1 (performance lineage freeze) "freeze/preregister" work.

---

## 1. Outcome summary

Both freeze/preregister deliverables were **already authored and are confirmed durable**.
This workstream's job was to (a) verify they still hold against the current tree, and
(b) record the unblocked-vs-gated disposition for the remaining B2/P items.

| Deliverable | Status | Artifact |
|---|---|---|
| **B2.3 — parameter grid freeze** | ✅ FROZEN / PREREGISTERED | `docs/plans/B2-PARAM-GRID-FREEZE-2026-09-17.md` |
| **P1 — code/config/data lineage freeze** | ✅ FROZEN / PREREGISTERED | `docs/plans/P1-LINEAGE-FREEZE-2026-09-17.md` |

Neither freeze was re-authored here — they are already immutable preregistrations. This
receipt confirms they remain valid and records the gate status of everything downstream.

---

## 2. B2.3 — Parameter grid (VERIFIED FROZEN)

The B2.3 grid in `B2-PARAM-GRID-FREEZE-2026-09-17.md` is immutable per its own §2 contract.
Verification this pass:

- **Canonical source present:** `config/regime_config_matrix.json` → `default_config.config`
  (the harness-validated override matrix, 2026-08-10). Present and parseable.
- **No amendment:** no `B2-PARAM-GRID-FREEZE-*-REV2.md` exists → the grid has not been
  silently revised. Freeze discipline intact.
- **Entry/edge/gating/risk/cost/calibration/regime values** remain as transcribed (edge 0.05,
  confidence floor 0.60, entry price [0.08, 0.50], min analogs 3, agreement 2-of-7, etc.).

**Receipt:** parameter grid is frozen; reproducible backtests may proceed against it without
fear of post-hoc tuning.

---

## 3. P1 — Lineage freeze (VERIFIED FROZEN, with a drift flag)

The P1 freeze in `P1-LINEAGE-FREEZE-2026-09-17.md` pins code/config/data identity.

### 3.1 Config identity — STABLE ✅

All 8 frozen config hashes (7 tracked + 1 untracked `regime_config_matrix.json`) were
recomputed this pass and **match exactly** the values recorded in the freeze doc. No config
drift. (Receipt hashes reproduced in §6.)

### 3.2 Code identity — DRIFT FLAGGED ⚠️

| Field | Frozen value (P1 doc) | Current HEAD |
|---|---|---|
| Commit | `04be98a04` | `c4f7beddc` |
| Tree | `6cbf22904` | (advanced 2 commits) |
| Date | 2026-09-16 16:13 UTC | 2026-09-18 18:15 UTC |

`04be98a04` **is an ancestor** of current HEAD (same branch lineage), but HEAD has advanced
two commits since the freeze was captured:
- `2cee227d0` — fix(weather-engine): fee + Sharpe correctness + adaptive_thresholds import
- `c4f7beddc` — fix(R6): fail-closed exit semantics for collectors + phantom-success tests

**Impact:** any performance number produced *after* these two commits is NOT traceable to the
P1 freeze's code identity (`04be98a04`). To keep lineage reproducible, the freeze must either
(a) be re-captured at the current HEAD before B2 results are generated, or (b) B2 backtests
must run against a clean checkout of `04be98a04`. This is the single material gap in the
otherwise-complete P1 freeze.

### 3.3 Data identity — live, not frozen ⚠️

Per the P1 doc's own §3 warning, the `data/` assets are **live, daemon-written** and their
hashes are point-in-time, not stable. The true frozen-data tier (`z8`) has not yet been
established — see §4.

---

## 4. Remaining B2 items — unblocked vs still-gated

| Item | Definition (from lost register) | Disposition | Blocker |
|---|---|---|---|
| **B2.1** | (register lost) | **STILL GATED** | R3 `z8` frozen data tier does not exist (see below) |
| **B2.2** | real-price join | **STILL GATED** | requires frozen hash-pinned settlement source (`z8`) |
| **B2.3** | parameter grid freeze | ✅ **DONE** | — |
| **B2.4** | execution metrics | **STILL GATED** | depends on B2.2 real-price join output |
| **B2.5** | (register lost) | **STILL GATED** | downstream of B2.2/B2.4 |
| **B2.6** | (register lost) | **STILL GATED** | downstream of B2.2/B2.4 |

**Root blocker for B2.1/2.2/2.4/2.5/2.6:** the task brief asserts "R3 z8 data availability —
R3 is DONE since 9/14." This is **not substantiated on disk.** The repository's own
authoritative records state the opposite:

- `H1-H2-PREREGISTRATION-2026-09-17.md` §1: *"At the time of this preregistration, `z8` is
  **not yet a defined, verified artifact** in the repository — it is a forward reference to a
  stability/provenance tier."*
- `WE-COMPLETENESS-AUDIT-2026-09-17.md` §3c / §6.5: `z8` is an **"UNDEFINED FORWARD REF"** —
  a hash-pinned frozen provenance tier that has never been created; distinct from
  `scripts/z8_kalshi_market_cron.py` (a *live mutating* snapshot collector, not a frozen tier).

What **does** exist (real ground-truth progress, but not the frozen `z8` tier):
- Kalshi `finalized` settlements: **1,669 rows** (`data/kalshi_settlements.db`,
  `source_type='finalized'`) + 12,105 `historical_api` (proxy/reconstructed — NOT truth).
- Settlement-epochs corruption rewrite (2026-09-17): 625 → 46 corrupt rows, 28 explicit
  `unresolved`. (`SETTLEMENT-EPOCHS-REWRITE-EXECUTED-2026-09-17.md`)

**Conclusion:** R3's *ground-truth data* may be materially improved, but the **frozen,
hash-pinned, provenance-tagged `z8` tier** that B2.1/2.2 explicitly require has not been
established. Until a `z8` artifact is created, hashed, recorded, and tied to the P1 lineage
freeze, **B2.1/2.2/2.4/2.5/2.6 remain gated** — regardless of the "R3 done 9/14" claim.

---

## 5. Remaining P items — unblocked vs still-gated

| Item | Disposition | Blocker |
|---|---|---|
| **P1** lineage freeze | ✅ **DONE** (with §3.2 code-drift flag) | — |
| **P2** leakage-safe 90-day walk-forward | **STILL GATED** | calibration/truth (`z8`) not established |
| **P3** separate paper/Kalshi/proxy accounting | **UNBLOCKED (documentation)** — spec closed in `P-METRICS-AND-PHANTOM-SUCCESS-2026-09-17.md`; runtime execution still gated on P2 | P2 output |
| **P4** full metrics report structure | **UNBLOCKED (documentation)** — §2 spec closed; execution gated on P2/P3 | P2 output |
| **P5** reconcile legacy 0.449 Sharpe | **UNBLOCKED (documentation)** — §3 root-cause closed; numeric resolution needs the 90-day Sharpe | P2 output |
| **P6** go/park/kill gate input | **STILL GATED** | requires P4 metrics run |

Per `P-METRICS-AND-PHANTOM-SUCCESS-2026-09-17.md` §5, P3/P4/P5 documentation closures are
complete; their *runtime* halves are gated on P2 (90-day calibrated walk-forward), which is
itself gated on calibration/truth (`z8`). Do not force.

---

## 6. Receipts

### 6.1 P1 config hashes (recomputed 2026-09-18, all match freeze doc)

```
a92c5e1a2b7e3cb145c3442da863ac52773e9d6b7330b62c6c69f4514a0d4e57  config.json
21040dc65d02dff4fe01290b2e3e81154c6c5cab56de556208f74bb5f2d4bd94  core/instance_config.py
c67f92b8002d5810608c2966853a10819ab4c57f12fbf0c2ac0b32e7bc89e4fa  core/instance_config_fixed.py
1af7dfc22ae0a0e5cd6c1156c1c8bf4dc27b1dd57357a417a0b85d0e4741efd0  core/phase1_config.py
fa9e8cc71183c718bc3249fd7c8b27f45df14ef3416591f97e0ffd04ff288029  core/signal_config.py
602650d19e2e644ad702214c7216899a86fdb6975bbbc05501160a10d2109425  render.yaml
b713d79e2d9c40f310178f630cdf2d22d28df99b933ff53bb496f34a9494239c  scripts/sweep/config.py
d04b7d500ad7b121d3c45025a822395537d34c6b871a44b1f22230ca49db4c12  config/regime_config_matrix.json   (untracked)
```

### 6.2 Ground-truth counts (current, not frozen)

```
kalshi_settlements.source_type='finalized'  = 1,669 rows
kalshi_settlements.source_type='historical_api' = 12,105 rows (proxy — NOT truth)
settlement_epochs total preserved = 117,517 (post 2026-09-17 rewrite)
```

### 6.3 Git identity

```
Frozen code baseline (P1):  04be98a04
Current HEAD:               c4f7beddc  (2 commits ahead; ancestor relation confirmed)
```

---

## 7. Items dependent on P0 ground-truth (re-run after P0)

Flagged for re-derivation once P0 ground-truth is finalized:

1. **P1 code identity (§3.2)** — re-capture the lineage freeze at (or pin B2 backtests to) the
   post-P0 HEAD, so results trace to a single commit.
2. **B2.1/2.2 real-price join** — must run against the frozen `z8` tier, which itself depends
   on P0 ground-truth (settlement-source precedence + sanity-band + provenance tagging).
3. **P2 90-day walk-forward + P4/P5 runtime halves** — all consume the P0-derived `z8` truth.

---

## 8. Boundaries honored

- ✅ No live trading, no paper trading, no daemon touch, no DB write.
- ✅ No commit (staged-for-review: this single doc is untracked, awaiting Dan's merge).
- ✅ Dirty tree preserved (24+ untracked plan docs, graphify/report artifacts left untouched).
- ✅ No destructive cleanup.
- ⚠️ The authoritative `WEATHER-ENGINE-ROADMAP-CONTROL-REGISTER-2026-09-11.md` is **absent from
  disk** (lost in the 2026-09 wipe — see `RECOVERY-100PCT-LEDGER-2026-09-18.md`). B2.1/B2.5/B2.6
  item definitions were reconstructed from surviving references; their exact register text
  should be re-confirmed once the register is recovered or re-authored.

---

## 9. Recommendation (next action)

1. **Establish the frozen `z8` tier** — the single root blocker. Hash-pin + provenance-tag the
   finalized settlement source (Kalshi `finalized` primary, GHCN fallback), apply the sanity
   band, and record it against P1 lineage. This unblocks B2.1/2.2 and cascades to B2.4/2.5/2.6
   and P2.
2. **Re-freeze P1 code identity at post-P0 HEAD** (or pin B2 to `04be98a04`) before any B2
   result is generated, to close the §3.2 drift gap.
3. Recover/re-author the lost control register so B2.1/B2.5/B2.6 have canonical definitions.
