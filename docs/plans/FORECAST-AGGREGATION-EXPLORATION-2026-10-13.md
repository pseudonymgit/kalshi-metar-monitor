# Forecast Aggregation Exploration — Multi-Source Tmax + Outlier-Trimmed Consensus

**Date:** 2026-10-13
**Author:** Gilfoyle (subagent, per Dan's directive)
**Repo:** `prototypes/weather-engine-source/`
**Script:** `scripts/forecast_aggregation_exploration_2026-10-13.py`
**Results:** `artifacts/forecast-aggregation/consensus_results.json`, `.../station_day_rows.json`
**Window:** 2026-09-13 .. 2026-10-12 (30 days, 20 ASOS stations, 559 scored station-days)
**Truth:** `data/ghcn_settlements.db` (GHCN-Daily — Kalshi settlement source), °F

---

## 1. What was built

A single-script, free-source Tmax aggregator. Every source is aligned to the
**same calendar horizon** (the forecast valid for its own target day), so no
source is handed a lead-time advantage over another. Sources:

| Group | Source | How | Auth |
|---|---|---|---|
| In-repo | `nws` | official NWS forecast archive (`forecast_disagreement_live.db`) | none |
| In-repo | `gfs`, `ecmwf`, `icon`, `gem` | deterministic NWP (`nwp_forecasts.db`, °C→°F) | none |
| In-repo | `gefs_ens` | 31-member GEFS mean (`nwp_forecasts.db`) | none |
| Open-Meteo API | `om_gfs`, `om_ecmwf`, `om_icon`, `om_gem`, `om_mf`, `om_ukmo`, `om_jma`, `om_knmi`, `om_dmi` | Historical Forecast API (free, keyless) — 9 independent NWP pipelines | none |
| Scraped | `wu` | wunderground.com embedded daily JSON | none |
| Scraped | `wc` | weather.com (attempt) | none |
| Scraped | `accu` | accuweather.com (attempt) | none |
| Web API | `wttr` | wttr.in multi-source consensus (today only) | none |

All network calls: real User-Agent, **≥2 s between requests to any host**, no
browser, no auth flows, HTML cached to disk and deleted after parse. HTML cache
is gone after the run; API/scrape result caches are gitignored and re-fetchable.

---

## 2. Coverage matrix

600 station-days possible (20 stations × 30 days).

| Source | Station-days | Family | Note |
|---|---:|---|---|
| `ecmwf`, `gfs`, `icon`, `gem` | 600 | local NWP | full |
| `gefs_ens` | 580 | local NWP | 20 member-days missing (KHOU tail) |
| `nws` | 399 | NWS archive | archive joined 2026-07 (window has 20-day NWS coverage) |
| `om_*` (9 models) | 600 each | Open-Meteo | full |
| `wu` | 11 / 20 (today only) | scraped | see §7 |
| `wc` | 0 / 20 | scraped | JS-rendered — not statically parseable |
| `accu` | 0 / 20 | scraped | Cloudflare 403 |
| `wttr` | 20 / 20 (today only) | web API | wraps multiple sources |

**No new key-based provider was used** — AerisWeather / WeatherAPI /
AccuWeather were skipped: no free tier is obtainable without signup friction
(and AccuWeather actively 403s plain requests). `WEATHERAPI_KEY` exists in
`.env` but the free tier is 3-day-only and not worth the signup/rota coupling
for this study; documented, not wired.

---

## 3. Divergence stats

Across the **full 15-source pool**, sources disagree by a lot more than the
"71–77 → 72–77" intuition:

- **Mean per station-day range: 7.63 °F** (trimmed range 5.05 °F)
- Range distribution: `≤2°F` = **0**, `2–3` = 8, `3–5` = 141, `5–8` = 220, `>8` = 190
- No station-day had all 15 sources within 2 °F. The "Venn narrowing" idea needs
  a *curated* pool (§6), because the raw pool carries known-bad members.

### Worst 5 station-days (widest divergence)

| Station | Date | Truth | Range | Std | n | Raw mean | Trim3 | Median |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| KSFO | 2026-10-03 | 90.0 | 31.7 | 8.3 | 15 | 86.2 | 87.9 | 88.5 |
| KSFO | 2026-10-04 | 82.9 | 29.0 | 7.5 | 15 | 84.6 | 86.0 | 85.1 |
| KSFO | 2026-10-07 | 75.9 | 28.5 | 6.9 | 15 | 78.4 | 80.1 | 81.9 |
| KSFO | 2026-10-06 | 80.1 | 27.4 | 7.3 | 15 | 80.6 | 82.3 | 83.3 |
| KSFO | 2026-10-05 | 75.9 | 25.3 | 6.8 | 15 | 79.7 | 81.6 | 81.5 |

**All five worst days are KSFO** — the expected coastal-microclimate case, and
worse than expected. Root cause: Open-Meteo's coarse grids place coastal
stations on ocean cells. `om_mf` (Météo-France) returns a **flat ~60 °F** at
KSFO for the entire month (grid elevation 1 m; it is effectively a marine cell),
and `om_jma` runs ~8–10 °F cold. Per-station mean ranges: **KSFO 18.5,
KLAX 12.2, KMDW 9.7, KAUS 8.7, KOKC 8.2** vs **KMIA 5.4, KMSY 5.7, KDFW 5.8**.

### Duplicate sources (independence violation)

`om_dmi` and `om_knmi` are **byte-identical** — mean absolute difference
**0.00 °F over all 559 rows**. Both HARMONIE pipelines fall back to the same
global model. Treating them as two members double-counts one. Detected and
de-duplicated in the curated pool.

---

## 4. Consensus vs best single source

Per-source MAE (out of the identical window, n≈559):

| Source | MAE °F | | Source | MAE °F |
|---|---:|---|---|---:|
| **`om_gfs`** | **1.240** | | `om_ecmwf` | 2.292 |
| `om_icon` | 1.706 | | `gem` | 2.341 |
| `om_gem` | 1.709 | | `om_mf` | 2.669 |
| `nws` | 1.831 | | `ecmwf` | 2.848 |
| `om_ukmo` | 1.845 | | `gefs_ens` | 3.043 |
| `om_knmi`/`om_dmi` | 1.881 | | `om_jma` | 4.003 |
| `gfs` | 2.043 | | | |
| `icon` | 2.221 | | | |

**Best single source: `om_gfs` @ 1.240 °F MAE** (Open-Meteo's GFS-seamless,
which applies elevation downscaling / bias correction). Note the *same* GFS
from our local raw DB scores 2.043 °F — **Open-Meteo's downscaling is worth
~0.8 °F MAE on its own.**

### Consensus variants

| Variant | MAE °F | Bias °F | vs `om_gfs` (paired Δ) |
|---|---:|---:|---:|
| raw mean (all 15) | 1.666 | −0.97 | +0.43 (worse) |
| trimmed mean (drop hi+lo) | 1.615 | −0.93 | +0.38 (worse) |
| median (all 15) | 1.586 | −0.97 | +0.35 (worse) |
| trimmed-pair-3 (all 15) | 1.596 | −0.94 | +0.36 (worse) |
| NWP family only (in-repo) | 2.071 | −0.36 | +0.83 (worse) |
| NWP family + NWS | 1.985 | −0.33 | +0.75 (worse) |
| inverse-MAE weighted (all) | 1.540 | −0.86 | +0.30 (worse) |
| **inverse-MAE weighted (reliable)** | **1.227** | −0.64 | **−0.01 (tie)** |
| reliable subset (top-5, dedup) | 1.255 | −0.65 | +0.02 (tie) |
| *oracle*: best single source per row | 0.299 | — | (not deployable) |

**Answer to the key question: no, a trimmed consensus does NOT beat the best
single source.** The absolute best deployable consensus (`inv_mae_rel`, 1.227)
only *ties* `om_gfs` (1.240) — and it ties only because it is itself ≈ a
weighted `om_gfs`. The naive trimmed mean/median of the full pool is
**0.3–0.4 °F worse** than `om_gfs`, because trimming only 1–3 tail members
cannot excise the two systematically-broken coastal members (`om_mf`, `om_jma`)
which sit inside the trimmed envelope.

Corollary: the in-repo NWP family alone is the *worst* option (2.071 °F). Any
consensus that includes the raw local NWP stack is dragged down by it.

---

## 5. Dan's Venn hypothesis — test result

Hypothesis: when sources agree tightly, the consensus target is more accurate
(agreement narrows the band and sharpens it).

Test on the **curated top-5 pool** (homogeneous quality, spread ≤2 °F actually
occurs):

| Internal spread (top-5) | n | Consensus MAE °F | Review |
|---|---:|---:|---:|
| **tight ≤2 °F** | 105 | **1.285** | |
| mid 2–4 °F | 303 | 1.185 | *lower* |
| wide ≥4 °F | 151 | 1.377 | |

Result: **the tight-agreement days are NOT the most accurate.** Mid-spread days
are actually best (1.185); tight days (1.285) are no better, and wide days are
worst (1.377). The relationship is non-monotone and the effect size is small.

Interpretation: a ≤2 °F spread means the models have converged — which often
happens on *easy* synoptic days **and** on days where several models share the
same bias (agreement ≠ correctness). Tight agreement mostly encodes low
synoptic uncertainty, not low forecast *bias*. The hypothesis, as a decision
rule, does not hold on this sample.

---

## 6. Verdict — **PARK**

*(Full pooling/target-range aggregation: PARK. The Open-Meteo bias-corrected
single source: ADVANCE as a cheap upgrade — see below.)*

**Why not ADVANCE:**
1. Aggregation **does not beat** the best single source. `om_gfs` = 1.240 °F;
   best deployable consensus = 1.227 °F (a statistical tie, and it only gets
   there by shrinking toward `om_gfs`).
2. It **cannot** deliver Dan's imagined tightening (71–77 → 72–77). The raw pool
   spreads **7.63 °F** on average; no trim rule turns that into a ≤2 °F band
   without hand-curating the member list.
3. The Venn agreement rule does **not** hold — tight-agreement days are not more
   accurate.
4. Scraped sources contribute **near-zero** historical value (no 30-day history)
   and are fragile (§7).

**Why not KILL:**
1. **`om_gfs` itself is a real, free, huge win.** 1.240 °F MAE vs our current
   best in-repo input. This is worth wiring into the rate_1h/NWP stack
   immediately — it is one keyless request per station per day.
2. The **robust consensus ties the best source** despite carrying 4 bad members.
   That robustness has option value if any single upstream (e.g. GFS) degrades:
   `inv_mae_rel` is a strictly safer centre at ~equal MAE.
3. The **divergence signal is free and already computed**: `range`, `std`, and
   the identified duplicate (`om_knmi`/`om_dmi`) and broken members (`om_mf`,
   `om_jma` at coasts) are useful as a **feature**, even if the level isn't.

**Recommendation (ranked):**
1. **ADVANCE:** wire Open-Meteo `om_gfs` (and `om_icon`/`om_gem`) as a
   bias-corrected Tmax input. Expected ~0.8 °F MAE gain over raw local GFS at
   near-zero cost. One request/station/day, keyless.
2. **ADVANCE (small):** pipe the diagnostics (`n_sources`, `range`, `std`,
   duplicate flags) into the disagreement signal as features.
3. **PARK:** full multi-source pooled/trimmed consensus, and any
   spread-conditioned narrowing rule. Revisit only with per-station,
   bias-corrected members (no raw coarse coastal grids).

**On rate_1h (Brier 0.092):** this is an MAE comparison on the daily-Tmax level,
not the same axis as rate_1h's intraday Brier. The consensus MAE 1.23–1.66 does
**not** establish an edge over rate_1h's Brier — do not read it as "beats
rate_1h". A same-axis (strike-crossing) test is required before any such claim.

---

## 7. Free-API vs scraped — fragility

**Free keyless APIs (robust, recommended):**
- **Open-Meteo Historical Forecast API** — 9 models, full 30-day history, one
  request/station, no key, no rate-limit friction. **The workhorse of this study.**
- **NWS archive** — already in-repo, official.

**Web APIs (today-only, low history):**
- **wttr.in** — 20/20 stations today, wraps a multi-source consensus. No history,
  so unusable for backtest; fine as a live cross-check.

**Scrapers (fragile — not production-grade):**
- **wunderground.com — `parse_wu_daily`:** works on **11/20**, two distinct
  failure modes:
  1. **Anti-bot interstitial** (~86 KB page, no `temperatureMax`) for ~5
     stations (KAUS, KLAX, KOKC, KPHL, KDCA) — intermittent.
  2. **`temperatureMax` schema mutation** — some stations (e.g. KHOU) now emit a
     long hourly array instead of the daily 4-element array; our daily parser
     correctly rejects it (no date alignment) → returns None. The hourly
     `validTimeLocal` (e.g. `2026-10-13T01:00:00-0600`) can be aggregated to a
     local-day max as a fix, but the schema is clearly unstable.
- **weather.com — `parse_wc_high`:** 0/20. The "today" page is JS/Next-rendered;
  no static `temperatureMax`/`high` field. Would require a headless browser —
  explicitly out of scope.
- **accuweather.com:** 0/20. Cloudflare 403 on plain GET. Not scrapable politely.

**Scraper verdict:** keep them as an *optional, best-effort live enrichment*
(≤11/20, no history), never as a backtest or consensus input. The stable value
is in the keyless APIs.

---

## 8. Caveats / method notes

- **Truth-source gap.** GHCN-Daily (the settlement source) and ASOS 1-min
  disagree slightly (e.g. KSFO 2026-10-03: ASOS max 88.0 °F vs GHCN 90.0 °F).
  We score against GHCN because that matches Kalshi settlement; the ~1–2 °F
  absolute offset inflates every source's MAE roughly uniformly and does not
  change the *ranking*.
- **Lead/joint alignment.** All sources are same-calendar-day. `nws` from the
  archive is the forecast issued *on* the target day, so NWS's 1.831 is
  "nowcast-grade" and should not be compared to a multi-day-lead NWP run. Our
  in-repo NWP is likewise the target-day forecast.
- **Open-Meteo "seamless"** models concatenate runs by lead time; for the
  historical-day horizon this is the short-lead component, which is the
  appropriate comparison.
- **Oracle row-best (0.299 °F)** is *not* deployable — it picks the winner
  after seeing truth — but it bounds what a perfect source-selection layer could
  achieve. `om_gfs` is the single best on only 15% of rows; no fixed source
  dominates.

---

## 9. Artifacts

- Script: `scripts/forecast_aggregation_exploration_2026-10-13.py`
- Results: `artifacts/forecast-aggregation/consensus_results.json`
- Per-row detail: `artifacts/forecast-aggregation/station_day_rows.json`
- HTML cache: created and **deleted** post-parse (gitignored).
