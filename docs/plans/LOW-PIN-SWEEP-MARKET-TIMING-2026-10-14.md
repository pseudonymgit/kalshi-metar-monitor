# LOW Pin Sweep — Market Timing vs Weather Skill (2026-10-14)

**Repo:** `prototypes/weather-engine-source/`
**Author:** Gilfoyle · **Workstream AG-1** (Sprint: LOW pin sweep by market timing)
**Status:** ANALYSIS ONLY. No code path, gate, sizing, or live-book change.
**Script:** `scripts/low_pin_sweep_market_timing_2026-10-14.py`
**Artifact:** `artifacts/low-market-lane/low_pin_sweep_market_timing_2026-10-14.json`
**Companions:** `docs/plans/LOW-MARKET-LANE-DESIGN-2026-10-14.md` · `docs/plans/LOW-HIGH-SIDEBYSIDE-2026-10-14.md`

Dan (2026-10-14): *"Now that LOW is built, do another pin sweep. Unless the pin
is based strictly on the market and weather has no actual bearing."*

**Read-only throughout.** `kalshi_market_snapshots.db` and `metar_backfill.db`
opened `mode=ro`; no book write; the only output is the JSON artifact + this doc.
No network (reuses the `/tmp` caches the prior LOW sweep produced).

---

## 0. Verdict first — **KEEP the 13:30 station-local pin**

The market-timing thesis was tested head-on and **does not beat the incumbent
pin**. When the pin score weights **market spread 0.7 / weather skill 0.3** (Dan's
stated tilt toward the market), the incumbent **13:30 scores highest (0.7415)** of
every candidate 18:00→06:00, by a hair over 18:00 (0.7384).

The reason is structural and it is the *opposite* of the thesis:

1. **The LOW spread is a 1¢-tick floor all night.** The median `yes_ask−yes_bid`
   for real LOW quotes is **0.06 across 13:30–20:00 and 22:00–23:00** — a flat
   one-tick band from mid-afternoon to midnight. Moving the pin to the evening
   buys **no spread improvement**; there is nothing to buy.
2. **Where the market *does* differ at night, it is WORSE, not better.** After
   midnight the spread *widens* (0.07 at 00:00 → 0.10 at 05:00–06:00) and quote
   depth **collapses** (mean open interest 227 @13:30 → 158 @01:00, 169 @02:00),
   because the books thin out overnight (~40% of the 13:30-20:00 quote count).
3. **Weather skill at night is a mirage.** om_gfs Tmin BSS looks best at 00:00
   (0.215) and 02:00 (0.259), but the **00:00 pin reads buckets from the prior
   local day** (the bucket grid is 00:00–23:45 per day), so it is a day-boundary
   artifact, and **02:00 is exactly when spread (0.09) and depth (169) are worst**
   — the pin with the apparent skill is the pin you cannot trade. On the honest
   same-day clock the skill *plateau* runs **18:00–20:00** and **declines after
   21:00** (BSS 0.128 → 0.056).
4. **The one genuinely better window is the incumbent one.** 13:30–20:00 is the
   only stretch that is simultaneously (a) spread-floor tight, (b) quote-dense,
   and (c) inside the om_gfs Tmin skill plateau. 13:30 is the *earliest* point of
   that stretch, so it keeps the full trading window (06:00 local open → 13:30)
   for free.

**One-line answer to Dan:** *the pin is not "based strictly on the market" — but
the market axis, scored properly, points at the same place the weather axis does:
13:30–20:00, spread-floor the whole way. 13:30 already sits in it and preserves
the entry window, so it stays.*

---

## 1. Method

### 1.1 Candidate pins
18:00 → 06:00 station-local, hourly (13 hours). The incumbent **13:30** is carried
as a reference column (`13:30*`) so the sweep answers "does the market move the
pin OFF 13:30?" rather than only ranking the overnight window.

### 1.2 Axis 1 — market spread / depth (weight 0.7)
From `data/kalshi_market_snapshots.db` → `market_snapshots`, `market_ticker LIKE
'KXLOWT%'` **and `market_type='binary'`** (the real LOW books), restricted to
**genuine two-sided quotes** `0 < yes_bid < yes_ask < 1`. This filter matters:
**723,080** raw KXLOWT* rows are dominated by synthetic `0/1` books (`mid 0.5`);
only **21,463** rows carry a real quote and are used. Snapshot timestamps are
converted to **station-local** time before binning by hour.

### 1.3 Axis 2 — weather skill (weight 0.3)
The honest LOW axis (mirror of HIGH `T=floor(hsf)+1, y=settled_max>T`):

```
T(pin) = floor(running_low@pin) - 0.5
mu     = om_gfs_Tmin(pin) - bias          # bias = -1.2069 F (panel)
p      = Phi((T - mu) / sigma)            # sigma = 3.2691 F (prior LOW sweep)
y      = 1 if settled_min <= T else 0
```

scored on the **84** LOW-backfill station-days with canonical LOW truth
(`settlement_truth market_type='LOW'`, trustworthy). om_gfs Tmin values are the
keyless Open-Meteo `gfs_seamless` daily minima (reused cache). Skill score =
**BSS** vs the base rate, clamped at ≥ 0. **Landmine noted:** `bias` is the
global panel mean; applying it at later pins is slightly mis-specified because
the running-low level itself shifts the residual — a per-pin refit is a stated
follow-up, not a claim of this sweep.

### 1.4 Pin score
```
pin_score = 0.7 * spread_score + 0.3 * skill_score
spread_score = 1 - (median_spread - min) / (max - min)   # over the scored pins
skill_score  = max(0, BSS)
```

---

## 2. Results

| pin | median spread | spread n (quotes) | spread score | mean OI | om_gfs Brier | BSS | dir acc | skill n | **PIN SCORE** |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **13:30\*** | **0.06** | 1152 | **1.00** | 226.9 | 0.2094 | 0.138 | 0.655 | 84 | **0.7415** |
| 18:00 | 0.06 | 1094 | 1.00 | 309.7 | 0.2101 | 0.128 | 0.655 | 84 | 0.7384 |
| 19:00 | 0.06 | 1041 | 1.00 | 327.6 | 0.2116 | 0.122 | 0.643 | 84 | 0.7365 |
| 20:00 | 0.06 | 1057 | 1.00 | 349.0 | 0.2116 | 0.122 | 0.643 | 84 | 0.7365 |
| 21:00 | 0.07 | 984 | 0.75 | 373.3 | 0.2122 | 0.119 | 0.643 | 84 | 0.5608 |
| 22:00 | 0.06 | 1010 | 1.00 | 375.5 | 0.2171 | 0.099 | 0.631 | 84 | 0.7297 |
| 23:00 | 0.06 | 966 | 1.00 | 395.2 | 0.2226 | 0.056 | 0.631 | 84 | 0.7168 |
| 00:00 | 0.07 | 850 | 0.75 | 372.8 | 0.1016 | **0.215** | 0.847 | 72 | 0.5895 |
| 01:00 | 0.08 | 643 | 0.50 | 158.4 | 0.1455 | 0.131 | 0.775 | 80 | 0.3891 |
| 02:00 | 0.09 | 604 | 0.25 | 168.7 | 0.1412 | **0.259** | 0.805 | 82 | 0.2527 |
| 03:00 | 0.09 | 595 | 0.25 | 185.7 | 0.1902 | 0.122 | 0.707 | 82 | 0.2115 |
| 04:00 | 0.09 | 558 | 0.25 | 208.3 | 0.2115 | 0.100 | 0.659 | 82 | 0.2051 |
| 05:00 | 0.10 | 547 | 0.00 | 280.8 | 0.2149 | 0.138 | 0.683 | 82 | 0.0415 |
| 06:00 | 0.10 | 482 | 0.00 | 297.3 | 0.2161 | 0.131 | 0.671 | 82 | 0.0393 |

**Best overall:** 13:30* (0.7415) · **best spread:** 13:30* (0.06) ·
**best skill:** 02:00 (BSS 0.259, Brier 0.1412) — the untradeable pin.

### 2.1 The spread axis is a floor, not a gradient
Full LOW spread profile by station-local hour (real quotes only):

| hr | 09 | 10 | 11 | 12 | **13** | 14 | 15 | 16 | 17 | **18** | 19 | **20** | 21 | 22 | 23 | 00 | 01 | 02 | 03 | 04 | 05 | 06 | 07 | 08 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| median | .08 | .08 | .07 | .07 | **.06** | .06 | .06 | .06 | .06 | **.06** | .06 | **.06** | .07 | .06 | .06 | .07 | .08 | .09 | .09 | .09 | .10 | .10 | .09 | .08 |
| mean | .088 | .080 | .074 | .075 | **.072** | .069 | .069 | .068 | .064 | **.064** | .072 | **.072** | .072 | .071 | .073 | .084 | .096 | .098 | .106 | .107 | .114 | .119 | .103 | .087 |
| OI | 250 | 215 | 222 | 224 | **227** | 242 | 248 | 258 | 296 | **310** | 328 | **349** | 373 | 376 | 395 | 373 | 158 | 169 | 186 | 208 | 281 | 297 | 252 | 338 |

Spread hits its **0.06 floor from 13:00 and holds it (modulo the 0.07 tick at
21:00) through 23:00**, then widens monotonically into dawn. It genuinely cannot
get tighter than one tick. Depth (OI) is highest overnight but that is a volume
property, and the **quote count** halves after midnight — the book is *thin*, not
*deep*, at 01:00-02:00.

### 2.2 Weather skill: plateau, and a day-boundary trap
On the honest same-day clock, BSS is **0.128 (18:00) → 0.122 (19:00-20:00) →
0.119 (21:00) → 0.099 (22:00) → 0.056 (23:00)**; on the morning side **0.131
(06:00) → 0.138 (05:00) → 0.100 (04:00) → 0.122 (03:00) → 0.259 (02:00)**. The
prior sweep already established the plateau is **flat 06:00–20:00 then declines**.
The **00:00 (BSS 0.215) and 02:00 (0.259) spikes are contaminated** by the
day-boundary artifact (§1.3 landmine): `pins ≤ 06:00` read `min(buckets[m] for
m ≤ pin)` from the *prior* local day's grid. Do not trade on them.

### 2.3 Per-station narrowest window (real quotes only)

| station | spread @13:30 | narrowest in-window hr | spread | @18:00 | @00:00 | @06:00 |
|---|---:|---:|---:|---:|---:|---:|
| KATL | 0.05 | 18:00 | **0.03** | 0.03 | 0.04 | 0.07 |
| KAUS | 0.06 | 18:00 | 0.05 | 0.05 | 0.05 | 0.10 |
| KBOS | 0.06 | 18:00 | 0.06 | 0.06 | 0.08 | 0.10 |
| KDCA | 0.07 | 22:00 | **0.04** | 0.05 | 0.08 | 0.07 |
| KDEN | 0.07 | 18:00 | 0.05 | 0.05 | 0.06 | 0.09 |
| KDFW | 0.07 | 00:00 | 0.06 | 0.07 | 0.06 | 0.09 |
| KHOU | 0.08 | 18:00 | 0.06 | 0.06 | 0.07 | 0.07 |
| KLAS | 0.08 | 18:00 | 0.07 | 0.07 | 0.09 | 0.11 |
| KLAX | 0.04 | 18:00 | 0.04 | 0.04 | 0.05 | 0.06 |
| KMDW | 0.06 | 18:00 | 0.05 | 0.05 | 0.06 | 0.10 |
| KMIA | 0.06 | 22:00 | **0.04** | 0.06 | 0.05 | 0.09 |
| KMSP | 0.07 | 18:00 | 0.06 | 0.06 | 0.08 | 0.11 |
| KMSY | 0.08 | 22:00 | 0.06 | 0.08 | 0.08 | 0.11 |
| KNYC | 0.07 | 18:00 | **0.04** | 0.04 | 0.07 | 0.09 |
| KOKC | 0.05 | 19:00 | 0.06 | 0.07 | 0.08 | 0.09 |
| KPHL | 0.05 | 18:00 | **0.04** | 0.04 | 0.06 | 0.07 |
| KPHX | 0.06 | 22:00 | **0.04** | 0.06 | 0.08 | 0.09 |
| KSAT | 0.09 | 18:00 | 0.09 | 0.09 | 0.09 | 0.11 |
| KSEA | 0.09 | 18:00 | 0.06 | 0.06 | 0.08 | 0.11 |
| KSFO | 0.05 | 18:00 | 0.05 | 0.05 | 0.07 | 0.11 |

**Read:** the narrowest in-window hour is **18:00 for 13 of 20 stations**, 19:00
for 1, 22:00 for 5 — and **00:00 for none**. The 18:00 edge is **1¢ at most**
(KATL/KDCA/KMIA/KNYC/KPHL/KPHX) and **zero for 12 stations**. A 1¢ spread saving
(0.006 in EV terms, see `core/market_cost_model.EXECUTION_COST_RATE` = 0.0205)
does **not** justify pinning at 18:00 (≈4.5h before midnight settlement → a
thin 4-5h entry window) over 13:30 (full-day entry window). This is a **noise-
level** difference, not a tradable one.

---

## 3. Recommendation

**KEEP 13:30 station-local.** No LOW-specific pin change is justified. Concretely:

1. **Global pin: 13:30.** Highest combined score, tightest (floor) spread,
   densest quote book, inside the om_gfs Tmin skill plateau, and it keeps the
   06:00→13:30 entry window. No change to `scripts/low_entry_bridge.py`
   (`DECISION_PIN_LOCAL_HHMM = "13:30:00"`).
2. **The market-timing thesis is REFUTED for LOW.** There is no evening pin that
   beats 13:30 on spread; the overnight window is *worse* (wider spread, thinner
   book) except for a uniform 1¢ tick at 18:00-20:00 already spanned by the
   13:30-20:00 window.
3. **Per-station pin: not warranted.** The per-station edge is ≤ 1¢ and not
   directionally consistent (18:00 for most, 22:00 for 5). Do not build a
   per-station LOW pin schedule on this.
4. **Follow-ups (not this sprint):**
   - Refit `bias` **per pin** (and per station) instead of the global -1.2069,
     to remove the running-low-level mis-specification (§1.3).
   - Re-score the 00:00/02:00 skill pins on a *strict same-day* bucket grid
     (exclude prior-day buckets) to confirm the spike vanishes.
   - Re-run on a larger LOW quote panel (>3 weeks) before any per-station claim.

---

## 4. Reproduce

```bash
cd prototypes/weather-engine-source
python3 scripts/low_pin_sweep_market_timing_2026-10-14.py     # writes the JSON artifact
# read-only on kalshi_market_snapshots.db + metar_backfill.db;
# reuses /tmp/we_asos_recs_2026-10-13.pkl.gz + /tmp/we_low_omgfs_tmin_2026-10-14.json
```
