# Ladder Re-Anchor Proposal (Item 3)

**Date:** 2026-10-13 · **Agent:** Gilfoyle · **Repo:** `prototypes/weather-engine-source`
**Scope:** PROPOSAL ONLY. No change to `_position_size_usd`, `GRADE_LADDER`, or
`GRADE_POSITION_CAP_USD` is made here.

---

## The problem

There are **two** confidence→size/gate ladders in the booking path, and both are
anchored for a world where `p_fused` could reach 0.99:

1. **Size ladder** — `scripts/sbox_intraday_ev_bridge._position_size_usd(confidence)`:
   `>0.85 → $25`, `>0.70 → $10`, `>0.50 → $5`, else $0.
   `confidence = |our_prob − 0.5| × 2`, and `our_prob` is the **calibrated** (ceiling-clamped) p.
2. **Grade ladder** — `core/intraday/multi_forecaster_fusion.GRADE_LADDER`:
   `S≥0.90 → $25`, `A≥0.80 → $15`, `B≥0.70 → $10`, `C≥0.60 → $5`, `F → 0`.
3. **Bridge entry grade** (F7) — `compute_trade_grade` is **edge-based**
   (`edge = calibrated_p − market_price`; S≥0.12/A≥0.09/B≥0.06/C≥0.03), caps
   `S/A/B/C = 25/15/10/5`.

**Reachability.** The F2 ceiling clamps `calibrated_p ∈ [1−ce, ce]`, so
`confidence = |p−0.5|·2` is capped at `(ce−0.5)·2`:

| ceiling `ce` | calibrated_p range | max confidence | original bands reachable? |
|---|---|---:|---|
| 0.752 (current default) | [0.248, 0.752] | **0.504** | No — max < C(0.60) |
| 0.768 (bridge NWP member) | [0.232, 0.768] | 0.536 | No |
| **0.80 (Item 1 recommendation)** | [0.20, 0.80] | **0.600** | No — max < B(0.70) |
| 0.85 | [0.15, 0.85] | 0.700 | No |
| 0.90 | [0.10, 0.90] | 0.800 | No — max < S(0.90) |
| 0.95 | [0.05, 0.95] | 0.900 | barely (S exact) |
| 0.99 | [0.01, 0.99] | 0.980 | yes |

**Under ceiling ≤0.90 the entire current ladder is unreachable.** The size ladder
returns $0 (needs confidence >0.50, impossible at ce=0.752) and the grade ladder
never assigns above C.

**Live-book evidence (read-only).** The 86 booked SBOX rows still carry the old
regime: `p_fused` max **0.99**, mean 0.856, composite conf max 0.98 → 50 rows
graded **S** at $25, 48 sized at $25. Those were booked **before** the F2 clamp was
live. Once the clamp is on, no row can grade S — or enter at all — under the
current thresholds.

---

## Scenario A — ceiling stays ~0.80 (Item 1's recommendation)

Max reachable confidence ≈ **0.60**. Re-anchor bands inside `[0, 0.60]`, keeping the
caps (25/15/10/5) and spacing them as fractions of the reachable range:

| Grade | proposed confidence ≥ | equivalent calibrated_p ≥ | cap |
|---|---:|---:|---:|
| S | **0.585** | 0.7925 | $25 |
| A | **0.550** | 0.7750 | $15 |
| B | **0.500** | 0.7500 | $10 |
| C | **0.400** | 0.7000 | $5 |
| F | < 0.400 | — | $0 |

- S sits just under the 0.60 max so it is reachable only on the sharpest tail
  (a genuine "sure thing"), preserving the ladder's intent rather than inflating it.
- This is **not** the brief's `S≥0.65 / A≥0.60 / B≥0.55 / C≥0.50` sketch: `S≥0.65`
  and `A≥0.60` are **above** the 0.60 max at ce=0.80, so that sketch is still
  unreachable. The bands above are the reachable version.
- **Size ladder** (`_position_size_usd`) must move in lockstep: `>0.55 → $25`,
  `>0.48 → $10`, `>0.40 → $5`, else $0 (mirror of A/B/C above), so the two ladders
  agree instead of one silently zeroing the other.

## Scenario B — ceiling rises to ≥0.90 at some stations

At ce=0.90 max confidence 0.80; restore close-to-original bands, shifted down one
tier so they remain reachable:

| Grade | proposed confidence ≥ | equivalent calibrated_p ≥ | cap |
|---|---:|---:|---:|
| S | **0.780** | 0.890 | $25 |
| A | **0.700** | 0.850 | $15 |
| B | **0.600** | 0.800 | $10 |
| C | **0.500** | 0.750 | $5 |
| F | < 0.500 | — | $0 |

- These are the original bands (0.60/0.70/0.80/0.90) minus ~0.10–0.12, matching the
  ~0.10 the ceiling lost off the top.
- Per-station: if only some stations clear ce≥0.90, apply Scenario B **only** there
  and Scenario A elsewhere (bands are already looked up per station via
  `station_params`).

## Boundary — the F7 edge gate

The F7 entry grade is **edge-based**, not confidence-based, and is unaffected by
this re-anchor. But it interacts with the ceiling: `edge = calibrated_p − price`, so
at ce=0.80 the S edge (≥0.12) needs `price ≤ 0.68` at `p=0.80`. That is possible,
so F7 can still admit S-grade rows at ce=0.80 — but the *confidence* ladder cannot.
**Recommendation:** keep F7 as the entry gate and treat the confidence ladder purely
as the **size cap**, so a high-edge/low-price row can enter at a size the confidence
ladder allows.

---

## Bottom line

- **The ladder is unreachable, top to bottom, at any ceiling ≤0.90.** Re-anchoring
  is required the moment the F2 clamp goes live, or the book books nothing.
- **If ceiling stays ~0.80 (Item 1):** use Scenario A bands
  (`S0.585 / A0.55 / B0.50 / C0.40`) and mirror them in `_position_size_usd`.
- **If ceiling rises ≥0.90:** use Scenario B per station
  (`S0.78 / A0.70 / B0.60 / C0.50`).
- **Caps (25/15/10/5) are unchanged** throughout — only the thresholds move.
- **Proposal only; nothing is wired.** `_position_size_usd` and `GRADE_LADDER` are
  byte-unchanged in this commit.
