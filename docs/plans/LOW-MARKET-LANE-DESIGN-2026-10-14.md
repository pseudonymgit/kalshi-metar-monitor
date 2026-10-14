# LOW Market Lane — Signal + Pin Design (2026-10-14)

**Owner:** Gilfoyle · **Lane:** Sprint 2 (technical)
**Repo:** `prototypes/weather-engine-source/`
**Status:** **DESIGN ONLY. Nothing wired.** No gate, booking, or live-book change.
**Scripts:** `scripts/low_trajectory_signal.py` (new signal + pin sweep)
**Artifacts:** `data/low_trajectory_pin_sweep_2026-10-14.json` · `artifacts/low-market-lane/low_trajectory_pin_sweep_2026-10-14.json`
**Companions:** `docs/plans/LOW-MARKET-HOLDOUT-2026-10-13.md` (holdout spec) · `docs/plans/LOW-MARKET-PRICE-BACKFILL-2026-10-13.md` (entry-price series) · `docs/plans/HONEST-SIGNAL-ACCURACY-2026-10-14.md` (HIGH honest axis)

Dan (2026-10-14): *"We'll need a different pin for LOW. We want the same predictive and trading functionality, but it's going to take some tweaks in the low lane."*

---

## 0. Bottom line (read this first)

The brief's premise — *"cooling happens overnight, so the LOW pin should be later than 13:30, maybe 20:00–22:00 local"* — **does not survive the data**:

1. **The US daily minimum is set near DAWN, not in the evening.** On 12,629 full-coverage station-days, the time-of-day of the daily minimum peaks at **05:00–07:00 local** (5,869 of 12,629 days = 46% in that 3-hour window). The running low **already equals the final min on 81.7% of days at 13:30**, 82.8% at 18:00, and 84.5% at 21:00 — a later pin adds only **+2.8 percentage points** of "low already determined". Only **17.3%** of days see the min occur at/after 18:00 (concentrated in Nov–Feb).
2. **A later pin buys almost nothing.** The running-low level MAE improves only from 1.28°F (13:30) to 0.70°F (23:00) — a slow grind, and it still *doesn't* discriminate (level-only Brier ≈ 0.19 → 0.17, no skill vs base rate).
3. **The cooling-rate signal is near-inert.** The `rate_1h` mirror (1h drop + projection) has **BSS ≈ 0** at every pin 18:00–23:00 — it adds nothing over the running-low level. The level-only arm *beats* the cooling arm at most pins.
4. **The real LOW primary signal is a NWP Tmin level forecast.** Generalizing the HIGH recipe (`om_gfs` Tmax) to the daily **minimum** gives **BSS 0.12–0.14, flat from 03:00 to 20:00 local**, i.e. a genuine LEVEL forecast with real skill — and its optimum is **early, not late**.

**Recommendation:** keep a **13:30 station-local pin** (parity with HIGH) — do **not** move it later. Build the LOW lane on a **Tmin level forecast (om_gfs generalized) + the running-low truncation**, not on a cooling-rate nowcast. The trajectory/cooling module is designed and measured here (`scripts/low_trajectory_signal.py`) but **should not be the primary LOW signal**. Full rationale in §4.

---

## 1. Why LOW is structurally different (mapped to the brief)

| brief claim | finding | source |
|---|---|---|
| "cooling happens overnight, not daytime heating" | **True, but the min is pre-dawn, not post-sunset.** Median time-of-min 05:45 local; 46% of days in 05:00–07:00. | `time_of_min_histogram_B`, this sweep |
| "rate_1h (heating) doesn't apply; design a cooling rate" | A cooling mirror (`scripts/low_trajectory_signal.py::cooling_nowcast`) is built and mirrored, **but it has no discrimination** (BSS≈0); the running-low level alone is as good. | §3.2 |
| "pin should be later than 13:30, possibly 20:00–22:00" | **No.** By 18:00 the low is already set on 82.8% of days; later pins add a slow, non-discriminating MAE grind. NWP Tmin peaks EARLY. | §3.1–3.3 |
| "om_gfs's Tmin accuracy is unknown" | **Now measured.** om_gfs Tmin: bias −1.21°F, sigma 3.27°F, level MAE **2.73°F** (raw); same-slot Tmax MAE is 1.30–2.09°F → **Tmin is ~1.5–2× harder**. But it still carries BSS 0.12–0.14. | §3.3 |
| "exit rules are simpler" | Confirmed direction (fewer late revaluations), unchanged from the holdout spec. | `LOW-MARKET-HOLDOUT` §3 |

**Downside of a late pin (the real cost):** settlement is midnight-local, so a 20:00–22:00 pin leaves only ~2–4h to entry — thin liquidity and few intraday snapshots. A 13:30 pin keeps HIGH parity and a full trading window. Since the data shows no predictive gain from a later pin, a **late pin is pure cost**.

## 2. Cohorts & axis (what was measured)

**Honest LOW axis** (mirror of the HIGH `T = floor(hsf)+1`, `y = settled_max > T`):

```
T(pin) = floor(running_low@pin) − 0.5      # "just below what we've seen"
y      = 1 if settled_min <= T(pin) else 0
p      = Phi((T − mu) / sigma)             # P(daily min <= T); increasing in T
```

| cohort | n | source | truth |
|---|---:|---|---|
| **A** — LOW backfill | **84** station-days | 178 LOW-backfill station-days with canonical LOW truth (requested subset, `570cd7ee7`) | `settlement_truth` market_type=LOW, trustworthy=1 |
| **B** — ASOS∩GHCN-low | **12,629** station-days | ASOS 15-min bucket grid ∩ GHCN `low_f`, full-coverage days only | GHCN daily low (`low_f`) |

Cohort B is the **sensitivity panel**: 12,629 days gives a stable read on what actually moves with the pin. Cohort A is the requested subset (small: n≈52–65 per pin, so pin *ordering* on A is noisy). Both use the station-local bucket grid from the honest ASOS loader (`predictor_diversity_study.load_asos`; bucket `mod` is genuinely station-local).

## 3. Sweep results

### 3.1 Time-of-minimum (cohort B, n=12,629)

| local hour | 00 | 01 | 02 | 03 | 04 | **05** | **06** | 07 | 08 | 09–17 | 18–21 | **22** | **23** |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| days | 811 | 479 | 629 | 885 | 1295 | **2275** | **2436** | 1158 | 162 | ~350 | ~350 | 319 | 1502 |

- **46%** of days: min in 05:00–07:00. **17.3%**: min at/after 18:00 (mostly Nov–Feb).
- **Running low already equals the final min on:** **81.7%** of days at **13:30**, 82.8% at 18:00, 84.5% at 21:00 — moving the pin from 13:30 to 21:00 gains only **+2.8pp**.

### 3.2 Cooling-rate signal vs running-low level (cohort B, `offset=1.0`)

`p = Phi((T − mu)/sigma)`; `mu = rlow − deflated_drop` (cooling) or `rlow` (level-only). BSS = 1 − Brier/Brier(base-rate).

| pin | n | cooling Brier | cooling BSS | **level-only Brier** | base | Lvl MAE | %low already set |
|---|---:|---:|---:|---:|---:|---:|---:|
| 06:00 | 11,487 | 0.2317 | +0.02 | 0.2398 | 0.385 | 1.68 | 0.112 |
| 09:00 | 11,263 | 0.2060 | −0.07 | 0.1922 | 0.259 | 1.35 | 0.147 |
| 12:00 | 10,868 | 0.2028 | −0.06 | 0.1917 | 0.257 | 1.30 | 0.148 |
| **13:30** | 11,071 | **0.1995** | **−0.04** | 0.1920 | 0.258 | 1.28 | 0.148 |
| 15:00 | 11,271 | 0.2013 | −0.05 | 0.1929 | 0.261 | 1.25 | 0.149 |
| 18:00 | 11,448 | 0.2732 | −0.42 | 0.1933 | 0.261 | 1.16 | 0.150 |
| 20:00 | 11,255 | 0.3148 | −0.68 | 0.1898 | 0.251 | 1.04 | 0.152 |
| 21:00 | 11,375 | 0.2819 | −0.53 | 0.1875 | 0.244 | 0.95 | 0.154 |
| 22:00 | 11,652 | 0.2529 | −0.42 | 0.1831 | 0.231 | 0.83 | 0.157 |
| 23:00 | 11,792 | 0.2397 | −0.48 | 0.1725 | 0.204 | 0.70 | 0.162 |

**Read:** the cooling term *hurts* at every pin after 09:00 (its BSS is negative; level-only is better). The level-only curve falls with a later pin, but that fall tracks the **base rate** (0.26 → 0.20) — the axis threshold `T=floor(rlow)−0.5` gets more extreme as rlow falls, so the event gets rarer, not easier. **Neither arm has real discrimination on cohort B.**

On cohort A (n≈52–65/pin) cooling BSS is noisy: best +0.07 at 21:30, then run of negatives — consistent with no reliable signal.

### 3.3 The real primary: om_gfs Tmin level forecast (cohort A, n=84)

`om_gfs` Tmin fetched from the same Open-Meteo endpoint as the HIGH recipe (`gfs_seamless`, °C→°F), scored as `p = Phi((T − (Tmin − bias))/sigma)`.

| metric | value |
|---|---:|
| bias (om_gfs Tmin − settled low) | **−1.21°F** |
| sigma of the bias | **3.27°F** |
| level MAE vs settled low | **2.73°F** |

Per-pin (bias-corrected):

| pin | 00:00 | 03:00 | 06:00 | 09:00 | 12:00 | **13:30** | 18:00 | 20:00 | 21:00 | 22:00 | 23:00 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Brier | **0.1016** | 0.1902 | 0.2161 | 0.2138 | 0.2138 | **0.2094** | 0.2101 | 0.2116 | 0.2122 | 0.2171 | 0.2226 |
| **BSS** | **+0.215** | +0.122 | +0.131 | +0.123 | +0.123 | **+0.138** | +0.128 | +0.122 | +0.119 | +0.099 | +0.056 |

**Read:** om_gfs Tmin is the only LOW arm with real skill, and its **optimum is EARLY** (00:00 best, then flat ~0.12–0.14 from 03:00 to 20:00, **declining** after 21:00). It is *not* a late-pin signal. (The 00:00 column is the few station-days whose *previous* day's buckets extend into the target local day; on the full-HIGH-style axis the stable plateau is 03:00–20:00.)

Context: HIGH om_gfs Tmax level MAE is **1.30–2.09°F**; LOW Tmin is **2.73°F** — the min is ~1.5–2× harder, as expected (nocturnal boundary layer, radiational cooling variance). But BSS 0.12–0.14 is real, comparable to the HIGH `gfs` member.

## 4. Design recommendations

### 4.1 Primary signal (LOW) = om_gfs Tmin level forecast + running-low truncation

Mirror the HIGH `om_gfs` substrate with the daily **minimum**, not a cooling rate:

```
# at the LOW decision pin p (station-local):
rlow(p)  = min(ASOS bucket means for mod <= p)          # running low, truncation floor
mu_low   = om_gfs_Tmin(today) − bias_st                  # NWP level, per-station debias
sigma    = sigma_st (≈3.27°F global; refit per station)
T        = chosen strike (Kalshi "or below" ladder)
p_low_le = Phi((T − mu_low)/sigma)
p_low_ge = 1 − p_low_le
# running-low lock (mirror of high_so_far truncation):
if T >= rlow(p):   p_low_le = 1.0                        # min already <= T with certainty
```

The **cooling-rate module** (`cooling_nowcast`) is retained in `scripts/low_trajectory_signal.py` as a designed mirror and a diagnostic, but the sweep says it is **not** the LOW primary. Its only plausible future use is as a **micro-adjustment near dawn** (03:00–07:00) if a station-specific validation ever shows it beats level-only there — not as the spine.

### 4.2 Entry pin = **13:30 station-local** (unchanged from HIGH)

Do **not** move the LOW pin later. Evidence:
- The min is pre-dawn: the running low **already equals the final min on 81.7% of days at 13:30** (82.8% at 18:00, 84.5% at 21:00) — a later pin buys only +2.8pp of determination.
- om_gfs Tmin BSS is **flat 03:00–20:00 then declines** — no predictive reason to pin late.
- A late pin (20:00–22:00) shortens the entry window to 2–4h before midnight settlement (thin liquidity, few snapshots) with **zero** measured gain.

**Pin schedule:** reuse `per_station_pins.json` (13:30-based) as-is for LOW initially; no LOW-specific pin schedule is justified by this sweep. If a station is genuinely a winter-evening-min station (Nov–Feb, min ≥18:00), a **per-station/per-season** late pin could be revisited later — but the panel does not support a blanket later pin.

### 4.3 Entry windows

Settlement is midnight-local, so `T−18h ≈ 06:00 local` (same as HIGH). With a 13:30 pin the **06:00–13:30 local window** is open (full trading window), and the entry-timing calculus is materially the **same as HIGH** — no LOW-specific window shift. This is the opposite of the brief's expectation that "the pin might be much later."

### 4.4 Gate / exit

Unchanged from `LOW-MARKET-HOLDOUT-2026-10-13.md`: same G0–G5 gate stack, same F7 grade ladder, same F9 exposure caps, `exit_profile = {"auto_close_h1"}` — the shared substrate already handles `market_type="LOW"` direction-symmetrically. Only the *primary signal* (om_gfs Tmin) and the *running-low truncation* are LOW-specific.

## 5. Explicit non-goals / do-not-wire

- No `market_monitor` / `sbox_intraday_ev_bridge` change. No `market_type="LOW"` booking path.
- No gate, sizing, or exposure-matrix change. No live-book write. No P&L change.
- No full holdout run (per constraints). This is signal + pin **design** only.
- Do **not** wire the cooling-rate signal as the LOW primary — measure first if ever proposed.

## 6. Recommendation

**ADVANCE to a data-readiness spike**, not to wiring:
1. Refit `om_gfs` Tmin to the **full** station-day population (only the 84-day cohort was scored here; the bias/sigma need a larger fit), then re-score BSS on a held-out window.
2. Keep the **13:30 pin**; validate the LOW holdout on the existing pre-registered 30 (P1–P3) using the om_gfs-Tmin primary.
3. Park the cooling-rate module (documented, measured, not wired).

**One-line answer to Dan:** *LOW needs a different **signal** (NWP Tmin level, not a cooling rate) but **not a different pin** — the daily low is set near dawn, so 13:30 local stays. A late pin is pure cost.*

---

## Reproduce

```bash
python3 scripts/low_trajectory_signal.py sweep       # pin sweep (cohort A + B + om_gfs Tmin)
python3 scripts/low_trajectory_signal.py signal --station KATL --date 2026-09-16
# needs /tmp/we_asos_recs_2026-10-13.pkl.gz (honest ASOS grid) and one keyless
# Open-Meteo Tmin pull (20 reqs) cached at /tmp/we_low_omgfs_tmin_2026-10-14.json
```
