# LOW Signal Options — what else can we look at for the uncertain days?

**Date:** 2026-10-15 · **Owner:** Gilfoyle · **Repo:** `prototypes/weather-engine-source/`
**Status:** analysis complete (read-only). **Do NOT arm `LOW_BRIDGE_ENABLED`.**
**Companions:** `docs/plans/LOW-PIN-CORRECTED-MODEL-2026-10-15.md`,
`docs/plans/LOW-MARKET-HOLDOUT-2026-10-13.md`, `docs/reference/WE-DATA-INTEGRITY-REGISTER.md`

Dan: *"And you tested all of our forecasters at each pin? What else can we look at for LOWs?"*

---

## 0. Bottom line

**Nothing free resolves the uncertain days, and the reason is structural, not a gap in our
source list.** The honest LOW cohort is **84 station-days**; the low has already formed by the
13:30 pin on **84.5%** of them, leaving **13 uncertain days at 13:30** (12 at the recommended
17:00 pin). A signal cannot be *learned and validated* on a dozen rows — every candidate below
fails on **statistical power before it fails on physics**, and the strongest of them (GOES LST)
fails on physics too.

Recommendation: **do not buy wethr.net Pro for a Tmin signal on this cohort.** The paid ECMWF-IFS
path is the only one that adds *information*, and it adds it to a 12-day validation set. Spend the
$25/mo only if the LOW cohort is first grown (more settled station-days, or contracts on stations
where the low forms after the pin). Otherwise this lane is correctly parked.

---

## 1. Why the search space is smaller than it looks

The LOW has two distinct regimes and only one of them is a forecast problem:

| regime | share @13:30 | what the market is | can a signal help? |
|---|---:|---|---|
| low **already formed** | 84.5% | settled information, priced | no — you'd be trading a decided contract |
| low **not yet formed** | 15.5% (13 days) | a genuine forecast | yes — but n = 13 |

Already eliminated (see companions): cooling rate (dead — decoupled from the settlement min),
running-low certainty (84% → 90% but the late pins are mispriced and the days too few), HVac /
electricity load (no data source), GEFS/om_gfs Tmin (miscalibrated on this cohort: Brier 0.436 vs
market 0.223). The **pin sweep** refuted "move the pin late": realized edge is negative at every
in-bounds pin and the Brier on uncertain days is *minimized early*.

So the remaining question is narrow: **is there a free observation that resolves the final Tmin on
the 12–13 uncertain days, before the market has priced it?**

---

## 2. Candidate sources — verified feasibility (probed live, 2026-10-15)

All probes below were made from this container; status codes are real.

| source | endpoint | free? key? | cadence | history | verified status |
|---|---|---|---|---|---|
| **GOES ABI L2 LST** (`ABI-L2-LSTF` full-disk, `LSTC` CONUS) | `noaa-goes16/18/19.s3.amazonaws.com` | free, no key | 15 min | full archive (walked `ABI-L2-LSTF/2026/228/…` — files present, 0.49 MB full-disk / 1.47 MB CONUS) | **HTTP 200, files listed** |
| **MADIS LDAD mesonet** (NetCDF, hourly) | `madis-data.ncep.noaa.gov/madisPublic1/data/LDAD/mesonet/netCDF/` | free, no key | 1 h | rolling ~1 day + yearly `archive/` (2001→2026, walked `archive/2026/` listing) | **HTTP 200, listing + `.gz` files** |
| **MADIS LDAD RWIS** (road-surface temp, NetCDF) | `…/data/LDAD/rwis/netCDF/` | free, no key | 1 h | same rolling + archive | **HTTP 200** |
| **RTMA 2-m temp** (FAA station text, 501 stations) | `noaa-rtma-pds.s3.amazonaws.com/rtma2p5.<date>/…FAA_T_stn_analysis_values.txt` | free, no key | hourly | **most-recent cycle only** (overwritten) | earlier RTMA-SCOPE work: fetchable; txt path not stable |
| **Synoptic / MesoWest API** | `api.synopticdata.com/v2/...` | freemium, **token required** | 5 min | vector/obs history by token tier | **HTTP 401 without token** |
| **wethr.net Pro (ECMWF-IFS)** | paid API | **$25/mo** | model | model history | not tested (no credential) |

**Reader cost:** `netCDF4` wheel downloads cleanly into this environment (10.4 MB, cp311 manylinux);
`h5py`/`pyproj`/`netCDF4` are **not** currently installed. GOES ABI is HDF5/netCDF with a
geostationary projection needing `pyproj` for pixel→lat/lon; MADIS LDAD is plain netCDF.

---

## 3. Why each candidate does not close the gap

### 3.1 GOES ABI land-surface temperature — exists, wrong quantity

The data is free, full-history, 15-minute, and covers all 20 stations. It still **cannot resolve
station Tmin**, for the same reason the earlier scan (§Workstream M) rejected satellite LST for
**Tmax** — and the reason is worse for Tmin:

- **LST is a skin temperature, not the 2-m air temperature that settles the contract.** On a clear
  night the radiative skin can run **several °F below** the 2-m shelter temperature; the offset is
  itself a function of wind, cloud, and soil moisture — i.e. it is another model, not an observation
  of the settlement quantity.
- **The GOES LST retrieval is unavailable/retrieval-error under cloud and at high zenith angles** —
  exactly the conditions (cloud, low sun) that govern whether the low keeps falling. The sensor goes
  blind precisely when the answer is uncertain.
- **Projection + footprint mismatch:** one ABI pixel is ~2 km; a station's Tmin is a point
  measurement with siting the pixel cannot see. The 15-min cadence is not the limiting factor; the
  quantity is.

**Verdict: NO.** Free and fetchable, but it observes the wrong physical quantity. (It also fails the
power test below.)

### 3.2 MADIS LDAD mesonet / RWIS — a denser version of what we already have

MADIS is the free minute-scale obs backbone we already identified as the PWS network sans marketing.
For the LOW it adds **more stations reporting the same 2-m air temperature**, not a new variable:

- It cannot tell you the *future* minimum; it tells you the **current** running low, which the ASOS
  buckets already tell us at far higher temporal resolution (1-min vs hourly).
- **RWIS road-surface temperature** is physically closer to "surface" than air temp but it is a
  *pavement* temperature with its own thermal mass and solar/insolation lag — a bridge-deck can be
  several degrees off the 2-m air min. It is a biased proxy for the settlement quantity, not a clean
  observation of it.
- Networks are sparse at our specific airports' surroundings and QC-heavy.

**Verdict: NO new information** for Tmin resolution. It is a spatial-density play we already have
via `iem_asos_1min.db`.

### 3.3 RTMA/URMA — an analysis, already tested and rejected

RTMA is an hourly 2.5-km 2-m temperature *analysis*. The RTMA-SCOPE test already showed it is
**worse than `rate_1h` on the strike-crossing axis** (≈ +0.15 Brier) and its errors are **not
independent** (level corr 0.84–0.96). Re-derived for the LOW it would inherit the same result: it is
a smoothed re-expression of the same observations, not a new constraint on the overnight minimum.

**Verdict: NO** (already `DO_NOT_WIRE`).

### 3.4 Synoptic / MesoWest — same quantity, keyed

Synoptic aggregates mesonets + PWS + METAR. With a (free, non-commercial) token it is the best single
aggregator — but it delivers **the same 2-m air temperature** as MADIS, keyed. It adds coverage, not
the missing variable, and the free tier's history is limited.

**Verdict: NO new quantity.** Worth a token only if a spatial/microclimate signal is ever
pre-registered (per Workstream L's finding that microclimate divergence is a *level* phenomenon).

### 3.5 wethr.net Pro (ECMWF-IFS) — the only path that adds real information

This is the one source that produces a **modeled Tminimum** — an actual forecast of the settlement
quantity — from a provider whose Tmin skill we do not already have via om_gfs. That is a genuinely
new signal, at **$25/mo**. The blocker is not the $25; it is the **validation set** (§4).

---

## 4. The real blocker: statistical power, not source availability

Even the perfect new Tmin model cannot be *validated* on this cohort:

| quantity | value | source |
|---|---:|---|
| honest LOW cohort | **84** station-days | `low_pin_corrected_2026-10-15.json` |
| low formed by 13:30 | **71 / 84 = 84.5%** | same |
| **uncertain at 13:30** | **13** station-days | same |
| uncertain at recommended 17:00 | **12** station-days | same |
| contested near-money contracts @13:30 (for market-vs-model) | **19** | same |

Twelve to thirteen independent rows cannot distinguish a Brier improvement of even 0.03 with any
confidence (the paired SEs on the pin sweep were **0.03–0.06** — *larger than every between-pin
difference*). A new signal tested on 12 days would report "no measurable gain" whether or not the
signal is real. Buying a data source to be validated on n≈12 is spending into noise.

**This is the structural conclusion Dan asked for: on the free-plus-cheap source landscape, LOW Tmin
is under-observable *and* under-validatable on this cohort.** The observability half is a physics
ceiling (nothing free measures the overnight 2-m min before it happens); the validatability half is a
cohort ceiling.

---

## 5. What would actually change the answer

Ranked by leverage:

1. **Grow the cohort, not the source list.** The uncertain set is 12–13 days because the low forms
   overnight. Contracts on stations/dates where the decision pin is *before* the diurnal minimum
   (e.g. evening pins on stations whose min is at 05:00–07:00 local, already past the T−2h window)
   would enlarge the uncertain set. **This is the prerequisite for every candidate above.**
2. **Only then** buy wethr.net Pro (ECMWF-IFS Tmin) — it is the sole candidate that adds a modeled
   Tmin, and it is only worth it once there are enough uncertain days to validate against.
3. **Re-test GOES LST only as a *feature* inside a model**, never as a standalone signal — and only
   if (1) is solved. Skin-vs-2m offset must be calibrated, which itself needs the uncertain days.

Do **not**: buy wethr.net now, wire GOES/RWIS/MADIS for LOW, or re-run the RTMA path — each spends
into n≈12.

---

## 6. Guardrails

- **Read-only** on every DB. No book writes. No network writes; probes were GETs only.
- **Do NOT arm `LOW_BRIDGE_ENABLED`.**
- Endpoints and status codes in §2 were probed live on 2026-10-15; re-verify before any build
  (register standing rule: *always re-verify*).
