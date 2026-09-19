# S5 — Source Reconciliation Re-Run (label-agnostic portions)

**Date:** 2026-09-18
**Owner:** Gilfoyle (architecture/QA lane)
**Item:** TASKS.md Workstream S5 — re-run reconciliation after P0 (date-tiered ladder + `/historical/markets` strike discovery)
**Scope:** Label-agnostic re-verification only — source inventory + coverage. No code/config/data mutation beyond S6 archive (separate receipt).
**Status:** PARTIAL COMPLETE — label-agnostic portions done; label-dependent comparison deferred to post-P0.

---

## 1. What changed since the 9/17 report (material deltas)

The 9/17 report (`S5-SOURCE-RECONCILIATION-2026-09-17.md`) established three problems (A: corruption rows, B: two bad dates, C: METAR −1 °F offset) and a source ladder. P0 is **still in flight**, so this re-run covers only what is label-agnostic (inventory + coverage). Four material deltas observed since 9/17:

| Signal | 9/17 value | 9/18 value | Delta |
|---|---|---|---|
| Kalshi `finalized` rows | 1,641 | **1,669** | +28 (coverage advanced to 2026-09-16) |
| Kalshi `finalized` stations | (unspecified) | **20** | KBOS/KMSY now included |
| `settlement_epochs` stations | 21 (implicit) | **31** | +10 new (KBNA, KCLT, KDTW, KJFK, KORD, KPDX, KSLC, KTPA, PHNL, TEST) |
| `settlement_epochs` schema | no strike fields | **+`strike_price`, +`strike_type`** | P0 strike-discovery write-path landing |
| WeatherAPI tier | (not in 9/17 scope) | **20 stations, 2010→2026-09-18** | new independent ground-truth tier confirmed |
| `kalshi_market_snapshots` strike_temp | (n/a) | **0.15% populated (1,008 rows / 17 stns)** | strike discovery largely incomplete |

---

## 2. Source inventory (re-verified 2026-09-18)

| Source | DB / table | Rows | Stations | Range |
|---|---|---|---|---|
| Kalshi finalized | `kalshi_settlements.db` (`source_type='finalized'`) | 1,669 | 20 | 2026-05-28 → 2026-09-16 |
| Kalshi historical | same (`source_type='historical_api'`) | 12,105 | 21 | 2021-08-06 → 2026-08-30 |
| GHCN daily | `ghcn_settlements.db` | 619,428 | 21 | 1869-01-01 → 2026-09-12 |
| WeatherAPI daily | `weatherapi_archive.db` → `daily` | 121,080 | 20 | 2010-01-01 → 2026-09-18 |
| market_snapshots | `kalshi_market_snapshots.db` | 669,608 | 20 | 2026-09-07 → 2026-09-18 |
| settlement_epochs | `metar_backfill.db` (`epoch_status='closed'`) | 117,557 | 31 | 2021-01-01 → 2026-09-18 |

**Station-set alignment (label-agnostic, no truth claim):**

- The 20-station core (`KATL KAUS KBOS KDCA KDEN KDFW KHOU KLAS KLAX KMDW KMIA KMSP KMSY KNYC KOKC KPHL KPHX KSAT KSEA KSFO`) is **fully shared** across Kalshi-finalized, WeatherAPI, and market_snapshots, and is a 20-of-21 subset of GHCN.
- GHCN is the only source with `KDAL` (Dallas-Love) that the Kalshi/WeatherAPI tiers lack.
- `settlement_epochs` has **10 stations not in any of the other tiers** (`KBNA KCLT KDTW KJFK KORD KPDX KSLC KTPA PHNL TEST`). These are METAR-epoch-only; `TEST` is a synthetic/placeholder station that must be excluded from any reconciliation (not a real city).
- `Kalshi historical` carries a `TEST` station too — same synthetic flag.

---

## 3. Coverage re-verification (the new `/historical/markets` + WeatherAPI tier)

### 3.1 WeatherAPI tier (new independent source)

- `weatherapi_archive.db` → `daily`: **121,080 rows, 20 stations, 2010-01-01 → 2026-09-18**, provenance `source='weatherapi_history'` (singular, clean).
- `hourly`: 2,406,407 rows (sub-daily, not yet folded into the daily-high reconciliation ladder).
- **Overlap:** WeatherAPI ∩ GHCN = 20 stations; WeatherAPI ∩ Kalshi-finalized = 20 stations. WeatherAPI has **zero** stations outside the shared core. It is a full-coverage daily-high proxy with a longer horizon than Kalshi (2010 → present) but shorter than GHCN (1869 → present).

### 3.2 `/historical/markets` strike discovery (`kalshi_market_snapshots.db`)

- 669,608 snapshot rows, 9,410 distinct market tickers, 20 stations, 2026-09-07 → 2026-09-18 (11-day live window).
- **`strike_temp` is only 0.15% populated (1,008 rows across 17 stations)** — the strike-discovery backfill is **far from complete**. 17 of 20 stations have at least one strike row (KATL/KBOS/KDCA/KDFW/KHOU/KLAS/KMSP/KMSY/KPHX/KSEA/KSFO = 72 each; KAUS/KDEN/KLAX/KMDW/KMIA/KNYC = 36 each); KOKC/KPHL/KSAT show zero strike rows so far.
- This confirms the re-run's strike-dependent comparison **cannot be completed until P0 lands** — the strike discovery surface is only ~0.15% materialized.

### 3.3 `settlement_epochs` strike fields (P0 write-path)

- New columns `strike_price` (56 populated) and `strike_type` (56 populated) out of 117,557 rows confirm the P0 date-tiered strike-discovery schema is landing, but is **effectively empty** (0.05%). Awaiting the backfill.

---

## 4. What still needs re-run AFTER P0 lands (deferred)

These are label-dependent and cannot be meaningfully re-run while the strike-discovery surface is 0.15% populated and P0's date-tiered ladder is uncommitted:

1. **Re-derive the source ladder** with `/historical/markets` strikes + WeatherAPI inserted. Expected question: does WeatherAPI daily-high sit *between* GHCN and settlement_epochs, and does the Kalshi `/historical/markets` strike (when populated) displace `historical_api` as a cleaner reconstructed-proxy tier?
2. **Re-quantify Disagreement B (05-28 / 08-10 epoch-boundary)** against the new strike fields — confirm whether the ~20 °F Kalshi-finalized gap is explained by a strike/settlement-window mismatch now that strike metadata is present.
3. **Re-check Disagreement A (424 garbage rows)** against the widened `settlement_epochs` station set (now 31 incl. `TEST` + 9 new METAR-only stations) — the corruption count is still 424 but the sane-band denominator has grown to 117,133; re-verify per-station garbage distribution for the new stations.
4. **Confirm Disagreement C (−1 °F rounding bias)** still holds on the newly-added stations and on the WeatherAPI tier (does WeatherAPI reproduce the same tenths-°C truncation bias, or is it independent?).
5. **`TEST` station policy:** both `settlement_epochs` and `kalshi_settlements` carry a synthetic `TEST` station — decide exclusion before any re-run comparison.

---

## 5. Receipt

- **Label-agnostic inventory + coverage re-verified** against live DBs (read-only, `mode=ro`).
- **No code/config/data mutation** in this step (S6 archive is a separate, non-destructive move — see `S6-GHCN-ARCHIVE-RECEIPT-2026-09-18.md`).
- **Deltas vs 9/17 report captured** in §1 — Kalshi-finalized +28 rows, settlement_epochs 21→31 stations, new strike fields landing, WeatherAPI tier confirmed, strike_temp 0.15% populated.
- **Deferred (post-P0):** the label-dependent ladder re-derivation, Disagreement B/C re-quantification on new tiers, and `TEST`-station policy. Flagged, not resolved (P0 still in flight).
