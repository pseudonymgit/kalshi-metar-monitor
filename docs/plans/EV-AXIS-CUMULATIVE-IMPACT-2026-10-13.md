# EV-Axis Cumulative Impact — 2026-10-13

**Scope:** verification of the four "already-coded, needs-wiring" items (F1, F7, F3,
F4) on the SBOX intraday EV bridge. Read-only on the live book; no `trades` writes.
**Register:** `docs/plans/WE-COMPLETE-REGISTER-2026-10-13.md`
**Prior art:** `docs/plans/BRIER-IMPROVEMENT-CUMULATIVE-2026-10-13.md` (forecast axis, net 0.0 pp)

---

## 0. Bottom line

All four items were **already coded and wired**. This sprint **verified the wiring,
proved it on the live book, and locked it with tests** — it did not need to build
code. The one real behavioural finding is the **ceiling × ladder interaction**:
on a correctly-calibrated book the honest edge ceiling (0.75) caps the sizing
ladder at $5-10, so the $25 "sure-thing" tier is now structurally almost
unreachable — which is the intended consequence of killing saturation.

**Brier impact of F1/F3/F7/F4 is 0.0 pp by construction.** These are the *selection
and execution* axis: they change which trades are booked, how big, and when they
exit — not the accuracy of the probability. The forecast axis (Brier) was closed in
the prior sprint at net 0.0 pp; the only verified forecast lever remains the om_gfs
NWP substitution (already built behind `USE_OM_GFS_NWP`).

---

## 1. Item-by-item verification

### F1 — slippage in EV ✅ WIRED (verified)
- `core/strike_selection_ev.select_strike(..., cost_model=...)` nets EV of
  `_entry_exec_cost` = live half-spread + slippage, fallback `EXECUTION_COST_RATE`
  (0.0205), `fail_closed=True`.
- `scripts/sbox_intraday_ev_bridge.run()` builds `_station_spread_model(ladder)`
  (probes the highest-liquidity strike once per station; measured-mean fallback)
  and threads it into `select_strike`. **`get_dynamic_spread` is now CALLED.**
- Live dry-run: clean, fail-closed path exercised.
- Tests: `tests/test_slippage_in_ev.py` (arithmetic) + `tests/test_f1_slippage_wired_bridge.py` (wiring). Commit `076fd8bcc`.

### F7 — grades as size caps ✅ WIRED (verified)
- `run()` grades the booked side on `calibrated_p` vs price paid
  (`compute_trade_grade` → `core.alert_builder.compute_opportunity_grade`), **rejects
  F**, caps size (S=$25, A=$15, B=$10, C=$5, D/F=$0), persists `grade_entered` +
  `grade_cap_usd` on the companion table (idempotent ALTER).
- Live evidence: the 4 open 2026-10-13 rows all carry `grade_entered='S'`,
  `grade_cap_usd=25.0`.
- Tests: `tests/test_grade_gate_f7.py` + `tests/test_f7_grade_wired_bridge.py`. Commit `13c9e8602`.

### F3 — exits wired to real holdings ✅ WIRED (verified)
- `run_exit_pass()` loads `status='open'` holdings (not this-cycle entries), prices
  from the snapshot DB, calls `evaluate_exits`, and **refuses to fabricate a
  mid-based PnL for an already-settled row** (routes it to reconciliation).
- Live dry-run on the real book — 7 open rows: **3 flagged
  `already_settled_pending_reconciliation`** (ids 268 KMSP / 269 KNYC / 270 KBOS,
  2026-10-12), 4 `no_exit_rule_triggered`; **0 closed, 0 written**.
- Default rules = `{auto_close_h1}` only (safest-first); take-profit/stop-loss/
  signal-flip are opt-in env flags.
- Tests: `tests/test_sbox_exit_pass.py` + `tests/test_f3_exit_wired_bridge.py`. Commit `0cc3f4e47`.

### F4 — EV gate on calibrated_p ✅ WIRED (verified)
- `fuse()` returns `calibrated_p` (ceiling-clamped) and stamps `meta["calibrated_p"]`;
  `fuse_strike` packs `{"p_fused": round(float(pf),4), **meta}` so **the selection
  key `p_fused` IS the calibrated probability**; `build_fused_p_fn.p_fn` returns
  `_calc(x)["p_fused"]` → `select_strike` prices EV on the calibrated value.
- Verified empirically: `fuse_strike(...)["p_fused"] == ["calibrated_p"]` (0.7519, not 0.99).
- Retrospective (read-only, 79 closed): ceiling is **uniform 0.7519** on this
  archived cohort → calibrated_p does **not** re-shape entry there (76/79 still
  enter at edge>0.03). The gate bites on the **new** book, whose rows already store
  per-trade `calibrated_p` (e.g. 268 KMSP 0.7928→0.7519, 271-274 0.2481).
- Tests: `tests/test_fusion_calibration.py` + `tests/test_f4_calibration_gate.py`. Commit `9283bc9aa`.

---

## 2. Cumulative table

Costs/impacts measured on the archived 79-closed book (24,041 contracts) unless noted.

| Fix | Brier impact (pp) | P&L impact (est $/100 trades) | Confidence | Status |
|---|---|---|---|---|
| **F1** slippage in EV | **0.0** | **+$1,248 accounting** (round-trip drag = $985.68 = 5.2% of realized PnL); rejects 0 of 79 archived (ceiling dominates) | High (code) / Med ($) | ✅ WIRED |
| **F7** grades→size caps | **0.0** | Sizing only: binds on 3/79 archived (75 are S, 2 F, 1 C, 1 D); reduces variance, not EV direction; F-rejects low-edge rows | High | ✅ WIRED |
| **F3** exits on real holdings | **0.0** | h-1 auto-close captures mid instead of riding to 0; 3/7 open rows correctly routed to reconciliation rather than a fabricated PnL | Med (rule) / Low ($ on this cohort) | ✅ WIRED |
| **F4** EV gate on calibrated_p | **0.0** (selection axis) | Ceiling 0.7519 uniform on archive → 0 re-shape; on new book rejects saturated-probability entries. Cuts the $25 sure-thing tier: conf>0.85 needs p_side>0.925, impossible under ce=0.7519 → ladder caps at **$10** | High (code) / Med ($) | ✅ WIRED |
| _(F2 saturation, background)_ | −3.5 (synthetic) | Re-shaped the probability it feeds | High | ✅ landed |

**Brier is 0.0 pp for every EV-axis item — by construction.** Selection fixes do not
change forecast accuracy; they change trade selection, size, and exit. Quoting a
Brier delta here would be dishonest.

---

## 3. The one behavioural finding worth flagging

**Ceiling × ladder interaction (new).** With the F2 calibrated ceiling at
`CALIBRATION_FLOOR = 0.75`, the maximum honest `our_prob` is 0.75, so max confidence
`= |0.75 − 0.5| × 2 = 0.50`, so `_position_size_usd` returns **$5** — and a booked
side at the ceiling is structurally capped at $5-10, never $25. The $25 "sure-thing"
tier is now effectively **vestigial** on a saturated-free book. This is the intended
result (saturation was what produced the $25 rows), but it should be a conscious
decision whether we want an explicit "very-high-confidence" tier at all, or whether
the ladder should be re-anchored to the calibrated confidence range.

**F1 is inert on the archived cohort** (0 flips in 79) because `calibrated_p = 0.75`
makes every deep-OTM strike look hugely +EV — a 2¢ execution cost cannot move a
0.70 "edge". F1 is not useless; it is a prerequisite for a *correctly-calibrated*
book where real edges are small. On the current archive it is a no-op, and the
register's "−2.5%" is a one-way drag estimate (the round-trip accounting is 5.2%).

---

## 4. Status vs the register

| Register item | Register status | Now |
|---|---|---|
| F1 CRIT slippage | 🔴 never called | ✅ wired + verified + tested |
| F3 CRIT exits | 🔴 no production caller | ✅ wired + verified + tested |
| F7 grades | 🔴 not wired | ✅ wired + verified + tested |
| F4 calibration gate | 🔴 selects on raw p_fused | ✅ verified: selects on calibrated_p |
| F2 saturation | 🔴 | ✅ landed (prior work) |

All four "already-coded" items are now **verified and test-locked**. No new code was
required beyond verification tests; the sprint's value is the proof plus the
ceiling×ladder finding.

## 5. Recommended next

1. **Decide the ladder vs ceiling**: re-anchor `_position_size_usd` to the calibrated
   confidence range, or drop the $25 tier explicitly.
2. **Execute the gated reconciliations** (11 stale rows; 3 pending 268/269/270) once
   truth lands — needs Dan's go.
3. **Sprint gate**: run `scripts/bullshit_crawl.py` on the NEW book once >50 trades
   accrue (<50% flagged = pass).
