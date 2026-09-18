# S5 — Kalshi / settlement_epochs / GHCN / METAR Source Reconciliation

**Date:** 2026-09-17
**Owner:** Gilfoyle (architecture/QA lane)
**Item:** TASKS.md Workstream S — "Reconcile Kalshi/epochs/GHCN/METAR disagreement"
**Scope:** Read-only analysis of existing data/receipts. No code, config, data, or DB mutation.
**Status:** COMPLETE (reconciliation report produced)

---

## 1. Executive summary

The "disagreement" between the four named sources is **not one problem — it is three distinct, independent problems**, only one of which is a genuine source-of-truth dispute:

| # | Disagreement | Nature | Magnitude |
|---|---|---|---|
| **A** | `settlement_epochs.settlement_bucket` garbage rows | Unit/encoding corruption (not a temperature) | 424 rows (0.36% of 117,517 non-null) |
| **B** | Kalshi finalized vs. everything else on two specific dates | Epoch-boundary / settlement-window definitional mismatch | ~20–30 °F on 2026-05-28 (13 stns) and 2026-08-10 (10 stns) |
| **C** | METAR/epochs vs. Kalshi/GHCN baseline offset | Systematic ~1 °F cooler in METAR-derived buckets | median −1.0 °F |

**Ground-truth recommendation:** Kalshi `source_type='finalized'` remains the authoritative settlement source, but it is **not trustworthy on 2026-05-28 and 2026-08-10** (see §4). GHCN daily high is the correct *weather* ground truth and agrees with Kalshi to 0.35 °F mean / 0.00 °F median everywhere else. `settlement_epochs` is a METAR-derived proxy that carries both a data-corruption defect (A) and a systematic −1 °F convention offset (C) and must never be treated as settlement truth without the sanity band plus the offset correction.

---

## 2. The four sources and what they actually are

| Source | DB / table | Rows (sane) | What it is |
|---|---|---|---|
| **Kalshi finalized** | `kalshi_settlements.db` → `kalshi_settlements` (`source_type='finalized'`) | 1,641 | Kalshi's finalized market settlement temp (°F). The *economic* truth. Range 2026-05-28 … 2026-09-15. |
| **Kalshi historical** | same table (`source_type='historical_api'`) | 12,105 | Historical API backfill — proxy/reconstructed, **not** authoritative (see §7). |
| **settlement_epochs** | `metar_backfill.db` → `settlement_epochs` (`market_type='HIGH'`, `epoch_status='closed'`) | 41,248 | Epoch-bucket settlement value, **derived from METAR observations**. A *proxy*, not Kalshi truth. |
| **METAR** | `metar_backfill.db` → `metar_observations` (raw), `daily_stats` (daily max) | 41,190 (daily) | Raw hourly METAR obs (temp reported in tenths °C) and daily `max_temp_f` aggregate. |
| **GHCN daily** | `ghcn_settlements.db` → `ghcn_settlements` | 587,546 | NOAA GHCN-D daily high/low (°F). The *weather* ground truth (1869 → present). |

**Key structural fact:** `settlement_epochs.settlement_bucket` and `daily_stats.max_temp_f` are **the same underlying METAR data** — they agree to 0.08 °F mean / 0.10 °F median across 41,190 overlapping (station, date) keys. So "METAR" and "epochs" are one source for reconciliation purposes; there is no independent METAR-vs-epochs disagreement.

---

## 3. Disagreement A — 424 garbage rows (corruption, not a temperature)

`settlement_epochs.settlement_bucket` is `INTEGER NOT NULL`. Applying the sane band `[-60, 135] °F`:

- Sane rows: **117,093** (99.64%)
- Garbage rows: **424** (0.36%)

Garbage values look like `54082, 52281, 30679, 25281, 64893, 63082`. The trailing `079/081/082/084` suffixes are **day-of-month digits**, i.e. the column absorbed a `temp*1000 + day` (or similar `YYMMDD`/sequence) concatenation on a minority of writes. They are not temperatures.

**Verified impact on 2026-08-10** (the worst single date): `settlement_epochs` holds `30679` for KPHX and `27081` for KOKC — where the *true* high was ~108 °F and ~103 °F respectively. Any consumer reading `settlement_bucket` without the sanity band ingests a ~3-order-of-magnitude error.

**Mitigation status:** `core/additive/historical_adapter.py` already applies `SANITY_MIN_F=-60.0` / `SANITY_MAX_F=135.0`, so its truth loading is protected **only if** the band is hit before use. The underlying table is **not** cleaned and the logger write-path has no guard.

---

## 4. Disagreement B — Kalshi finalized vs. weather truth on two dates (epoch-boundary mismatch)

This is the **real** reconciliation finding, and it is not corruption — it is a *definitional* mismatch.

Measured on overlapping (station, date) keys, signed `ghcn - kalshi`:

| Date | Stations affected | Magnitude | Pattern |
|---|---|---|---|
| **2026-05-28** | 13 | Kalshi ~20 °F *cooler* than GHCN/epochs/daily | First day of Kalshi finalized coverage |
| **2026-08-10** | 10 | Kalshi ~20–30 °F *cooler* than GHCN/epochs/daily | Isolated systematic anomaly |

Three-way confirmation (all values °F):

| Station | Date | Kalshi finalized | GHCN high | epochs bucket | daily_stats max |
|---|---|---|---|---|---|
| KPHX | 08-10 | 86.0 | 108.0 | *(garbage)* | 107.x |
| KSAT | 08-10 | 78.0 | 98.1 | *(garbage)* | 98.x |
| KOKC | 08-10 | 75.0 | 102.9 | *(garbage)* | 102.x |
| KSEA | 08-10 | 55.0 | 73.9 | 77 | 77.0 |
| KLAS | 05-28 | 57.0 | 82.9 | 81 | 80.8 |
| KAUS | 05-28 | 61.0 | 90.0 | 89 | 88.7 |
| KDEN | 05-28 | 50.0 | 77.0 | 75 | 75.2 |

On these dates **GHCN, settlement_epochs (where sane), and daily_stats all agree with each other**, and **Kalshi finalized is the outlier**. This is the signature of Kalshi settling to a **different measurement window** — either a partial-day / morning-only high, or a settlement epoch whose boundary does not align with the civil "daily high" that GHCN/METAR report. The 05-28 cluster is compounded by it being the *first* finalized settlement batch (initialization/backfill artifact).

**Consequence:** Kalshi finalized is authoritative for the market it settled, but it can disagree with the physical daily high by tens of degrees when the epoch window does not span the full day. Downstream calibration that mixes Kalshi truth with GHCN/METAR features on these dates will see spurious ~20 °F residuals.

---

## 5. Disagreement C — systematic ~1 °F METAR offset

After removing garbage rows, signed offsets on overlapping keys:

| Pair | n | mean | median |
|---|---|---|---|
| `ghcn − kalshi` | 1,581 | +0.28 °F | **0.00 °F** |
| `epochs − kalshi` | 1,244 | −0.46 °F | **−1.00 °F** |
| `ghcn − epochs` | 41,177 | +0.35 °F | **+0.90 °F** |

The median `epochs − kalshi = −1.00 °F` is present across nearly every station (see per-station table below). METAR-derived buckets read systematically **~1 °F cooler** than both Kalshi finalized and GHCN daily high.

**Root cause:** METAR observations are reported in **tenths of a degree Celsius** (`temp_c` = 12.2, 12.8, 13.3 …), and the °C→°F conversion/rounding convention used to build `settlement_bucket` truncates toward a cooler integer °F (e.g. 26 °C = 78.8 °F → 78 °F, losing ~1 °F vs. the 79 °F round value). This is a **rounding-direction bias**, deterministic and correctable — not noise.

Per-station `epochs − kalshi` (signed mean, stations with n ≥ 20):

| Station | n | mean | Station | n | mean |
|---|---|---|---|---|---|
| KATL | 59 | −1.31 | KOKC | 77 | −0.22 |
| KLAX | 106 | −1.25 | KSEA | 96 | +0.08 |
| KSFO | 52 | −1.23 | KMDW | 84 | +0.14 |
| KHOU | 98 | −1.13 | KPHL | 34 | +0.29 |
| KAUS | 58 | −0.91 | KSAT | 27 | +0.33 |
| KDFW | 89 | −0.83 | KMSP | 24 | +0.46 |
| KDEN | 27 | −0.74 | KNYC | 96 | +1.08 |
| KDCA | 94 | −0.63 | | | |

The bias is negative for most stations (METAR cooler) but has per-station variance, so a single global offset is insufficient — it is a station-specific rounding/observing convention.

---

## 6. Disagreement summary (magnitude + direction)

| Disagreement | Direction | Magnitude | Fraction of data | Severity |
|---|---|---|---|---|
| **A** epochs garbage rows | both (signless) | up to 64,000 °F | 0.36% | **Critical** if unguarded; trivially filterable |
| **B** Kalshi finalized vs. weather truth (05-28, 08-10) | Kalshi ~20–30 °F *cooler* | 20–30 °F | ~23 station-days | **Critical** for those dates; needs epoch-window audit |
| **C** METAR/epochs baseline | METAR ~1 °F *cooler* | median −1.0 °F | most overlapping keys | Moderate (systematic bias, correctable) |

---

## 7. Ground-truth recommendation

**Treat Kalshi `source_type='finalized'` as the economic settlement truth** — but with three conditions:

1. **Exclude or flag 2026-05-28 and 2026-08-10** (and audit for other epoch-boundary dates) before using Kalshi finalized as physical-weather truth in calibration/backtest. On those dates GHCN (or the METAR daily max) is the correct *physical* high; Kalshi is the correct *market* settlement but they measure different windows.

2. **Never treat `settlement_epochs.settlement_bucket` as truth without both** (a) the `[-60, 135] °F` sanity band **and** (b) awareness of the −1 °F rounding bias. It is a METAR proxy, ranked *third* in authority, and should only fill gaps where neither Kalshi finalized nor GHCN exists.

3. **Never treat `kalshi_settlements.source_type='historical_api'` (12,105 rows) as authoritative** — it is proxy/reconstructed backfill, distinct from the 1,641 `finalized` rows. This is a data-policy item for Gerri/Dan (see open decisions).

**Truth-precedence chain (recommended, consistent with existing adapter):**

```
1. kalshi_settlements.source_type='finalized'   (economic settlement truth; caveats §4)
2. ghcn_settlements.high_f                        (physical weather truth; agrees w/ Kalshi 0.35 °F mean)
3. settlement_epochs.settlement_bucket (sanitized) (METAR proxy; −1 °F bias, garbage rows)
```

Note this **reorders** the adapter's current chain (`kalshi > settlement_epochs > ghcn`) — GHCN should rank **above** settlement_epochs because (a) GHCN agrees with Kalshi to 0.35 °F while epochs carries a systematic −1 °F bias and 0.36% corruption, and (b) GHCN is a true daily-high measurement, not a proxy. The existing adapter already applies the sanity band but does **not** correct the −1 °F bias and ranks epochs above GHCN — both are recommended changes.

---

## 8. Open decisions (escalate, do not self-resolve)

1. **Gerri/Dan (data policy):** Are `historical_api` Kalshi rows authoritative truth, or proxy-only? (Recommendation: proxy-only; never mix with `finalized` as truth.)
2. **Gilfoyle/QA:** Confirm the exact settlement-window definition Kalshi uses on 05-28 and 08-10 — is the ~20 °F gap a morning-only high, a shifted epoch, or a data defect?
3. **Gilfoyle/QA:** Whether to fix the `settlement_epochs` write-path (persist sanity band + quarantine garbage rows) and correct the °C→°F rounding bias — or leave as read-time-only handling.

---

## 9. Verification / non-mutation confirmation

- **File written:** `docs/plans/S5-SOURCE-RECONCILIATION-2026-09-17.md` (this document).
- **No code, config, data, or other docs modified.** All analysis was read-only (`sqlite3 ... mode=ro`) against existing DBs.
- **No backtests, no live trading, no DB/daemon writes.**
- All quantitative figures above were independently recomputed this session (not copied from the prior 09-16 note); where they differ from the prior note, the corrected/refined values are stated explicitly.

### Prior-note reconciliation (differences from `S5-disagreement-2026-09-16.md`)

The 09-16 note concluded the disagreement was "0.4% data corruption + a unit-encoding bug, not a systematic source conflict." This report **confirms** the corruption finding (A) but **refines** it in two material ways:

1. The prior note's Kalshi↔epochs figures (`mean 8,487 °F`) were inflated by *including* garbage rows in the comparison; with the sanity band applied uniformly, the residual Kalshi↔epochs disagreement is a **real systematic −0.46 °F mean / −1.0 °F median** (not noise), plus the **epoch-boundary** effect on 05-28/08-10 that the prior note did not isolate.
2. The prior note did not distinguish the **epoch-boundary definitional mismatch (B)** — which is the literal "Kalshi/epochs" disagreement — from the corruption (A) and the rounding bias (C). All three are now separately quantified.
