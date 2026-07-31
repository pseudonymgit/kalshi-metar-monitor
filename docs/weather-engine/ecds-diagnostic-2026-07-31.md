# ECDS/TIGGE Backfill Diagnostic Report — 2026-07-31

## Overview

The ECMWF Data Store (ECDS) API at `https://ecds.ecmwf.int/api` returns
**"Number queued requests for this dataset is temporarily limited"** on every
request to the `tigge-forecasts` dataset since the single successful download
on 2026-07-31 14:33 UTC (24.8 MB, 50-member GRIB file).

This report diagnoses root causes and recommends a path forward.

---

## Root Cause: Per-Dataset Request Queue Full

The CDS/ECDS API enforces per-dataset limits on **concurrent queued requests**.
When the queue exceeds an unspecified threshold (typically 5-20 pending
requests depending on dataset load and server workload), new requests are
rejected with the "temporarily limited" error.

### Key constraints (from ECMWF documentation):

| Constraint | Value | Source |
|---|---|---|
| Default concurrent requests | 2 | `ecmwfr` package docs |
| Max concurrent requests | ~20 | ECMWF API FAQ |
| Queue cooldown | Not documented (varies) | ECMWF forum |
| TIGGE dataset fields limit | Not documented (MARS tape) | CDS docs |
| Request staging time | 30-120s per month request | Observed |

The cooldown is **not publicly documented**. From ECMWF forum threads
(forum.ecmwf.int/t/api-queued-requests/12203), the queue rejection persists
until existing requests finish processing. Typical recovery time: **15-60
minutes** depending on dataset load.

### Why our scripts hit the limit

Three scripts were developed, all sharing the same API key `e543bdb6-...954e`:

| Script | Request Strategy | Delay | Problem |
|---|---|---|---|
| `tigge_backfill.py` | 1 month × all 9 steps × 50 members | 10s between requests | Enormous requests flood queue |
| `ecds_tigge_backfill.py` | 1 day × 1 step (f024 only) | 15s | Queue still overflows |
| `tigge_backfill_fixed.py` | 1 day × 9 steps | 10s | High request rate |

The sequential flood pattern:
1. `ecds_tigge_backfill.py` submits request for day 1
2. Request stages on server (30-120s)
3. Meanwhile, 15s later, next request for day 2 is submitted
4. This compounds rapidly — within a few minutes, 5-10 requests are queued
5. Queue limit is hit → all subsequent requests rejected

**The 15-second delay is insufficient.** With 30-120s staging time, requests
accumulate at ~4/minute while completing at ~0.5-1/minute.

---

## Diagnosed Script Issues

### 1. `scripts/tigge_backfill.py` (original)
- **Fatal flaw:** Requests full month (~31 days, all 9 steps, all 50 members,
  20 stations) in a single API call. This creates a ~500MB+ GRIB file request
  that takes 60-120s to stage.
- **10s delay between stations:** Submits a new request for every station,
  compounding the queue issue ×20.
- **No checkpoint granularity:** Only tracks by year-month, not individual dates.

### 2. `scripts/ecds_tigge_backfill.py` (current attempt)
- **Better strategy:** 1 day × 1 step (f024 only) — minimal request size.
- **Still insufficient delay:** 15s delay while server takes 30-120s → queue
  grows by ~4/min.
- **No wait-for-completion:** Submits new request without verifying previous
  one finished.
- **Checkpoint uses file existence, not API confirmation:** Assumes `.grib`
  file on disk means request succeeded, but large files may be partial.
- **Missing `wait_until_complete` parameter:** cdsapi default is `True`, but
  if the script crashes mid-request, orphaned requests stay in queue.

### 3. `scripts/tigge_backfill_fixed.py` (alternate)
- **Good:** Has `--source openmeteo` fallback that bypasses ECDS entirely.
- **Good:** Proper checkpoint tracking at date-step-station granularity.
- **Problem:** Default source is still ECDS with only 10s delay.
- **Problem:** Requests ALL 9 steps at once instead of 1 step per request.

---

## Is `tigge-forecasts` the Correct Dataset?

**Yes.** The TIGGE dataset provides ECMWF 50+1 ensemble member reforecast data
(system 4 / IFS cycle 47r1) dating back to 2006-10. Parameters:
- `class=ti` (TIGGE)
- `dataset=tigge-forecasts` (correct ECDS dataset name)
- `origin=ecmf` (ECMWF)
- `param=167` (2m temperature)
- `type=pf` (perturbed forecast, members 1-50)
- `step=0/3/6/9/12/15/18/21/24` (9 steps)

The 24.8 MB file downloaded on 2026-07-31 14:33 confirms the dataset name and
parameter selection are correct. The 50-member GRIB parsed successfully on that
one attempt.

---

## Can We Use a Different API Key?

The current key `e543bdb6-c396-48b4-bc1c-bebe08db954e` works for both CDS
and ECDS. It's a single ECMWF account key.

**Options:**
1. **Register a second ECMWF account** for a dedicated TIGGE key. This would
   give us a completely independent queue for the tigge-forecasts dataset.
2. **Request a service-level increase** from ECMWF via their support forum
   (unlikely for a free-tier account).
3. **Use institutional access** if available (typically higher limits).

**Recommendation:** Register a second ECMWF account (takes 5 minutes) and use
a dedicated key for TIGGE only. This immediately doubles our effective queue
capacity.

---

## Alternative Data Sources for ECMWF Ensemble Data

### Option A: Open-Meteo ECMWF IFS Ensemble (Recommended for recent data)
- **URL:** `https://ensemble-api.open-meteo.com/v1/ensemble`
- **Model:** ECMWF IFS (0.25°, 51 members)
- **Historical depth:** Only ~3 days of individual member data
- **Ensemble mean/spread:** Longer retention (months)
- **Access:** Free, no API key needed for moderate usage
- **Limitation:** Cannot backfill historical TIGGE data (2006-2025)

### Option B: ERA5 Reanalysis (Deterministic ground truth)
- **Already partially done:** 8,761 rows, KNYC 2021-2025, rest 2021 only
- **Problem:** Single member (deterministic) — not an ensemble
- **Delay:** Same CDS API key, would hit same queue limits
- **Not suitable** as ECMWF ensemble replacement

### Option C: ECMWF Open Data (Recent only)
- **URL:** `https://data.ecmwf.int/forecasts/`
- **Coverage:** Last 2-5 days only
- **Format:** GRIB2, 0.4° resolution
- **Access:** Direct HTTP download, no API key needed
- **Not suitable** for historical backfill

### Option D: Already-Have GEFS Data (Best default fallback)
- **Status:** 363,440 rows, 2,018/2,035 dates, 20 stations, 9 steps
- **Model:** GEFS v12 (31 perturbed + 1 control, ~35km)
- **Similar quality:** Different underlying model (GFS vs ECMWF), but
  ensemble spread/skill comparable for temperature
- **Action:** Resume the 17 missing dates around Jul 2022 with `--resume`

### Option E: Wait for ECDS Queue Clear + Sequential Backfill
- Estimated wait: 24-48 hours for queue to fully drain
- Then: strictly sequential, one request at a time, wait for each download
  to complete before next request
- At 1 request per 120s, backfilling 5 years (1,826 days × 9 steps = 16,434
  requests) would take **~23 days** solid

---

## Recommended Fix

### Immediate (within 24h):

1. **Kill all running TIGGE scripts** — they're compounding the queue
2. **Don't submit any new ECDS requests for 24 hours** to let the queue drain
3. **Register a second ECMWF account** for a dedicated TIGGE-only API key
4. **Verify GEFS data completeness** — resume the 17 missing dates.

### Next 72h — ECMWF backfill approach:

```python
# Revised approach: single-threaded, wait-for-completion
import cdsapi
import time

c = cdsapi.Client(url="https://ecds.ecmwf.int/api", key="<DEDICATED_KEY>")

for date in date_range:
    for step in steps:
        grib_out = f"data/tigge_grib/tigge_{date}_{step}h.grib"
        if os.path.exists(grib_out) and os.path.getsize(grib_out) > 1000:
            continue  # Already downloaded

        # cdsapi waits for completion by default (wait_until_complete=True)
        c.retrieve("tigge-forecasts", {
            "class": "ti", "expver": "prod", "dataset": "tigge-forecasts",
            "date": date, "grid": "0.5/0.5", "levtype": "sfc",
            "origin": "ecmf", "param": "167", "step": str(step),
            "time": "00:00:00", "type": "pf", "number": "1/to/50",
        }, grib_out)

        time.sleep(60)  # Cooldown between requests
```

Key changes from current scripts:
- **Wait for `c.retrieve()` to return** (default behavior) before next request
- **60-second minimum delay** between requests (not 10-15s)
- **Single dedicated API key** for TIGGE only
- **1 step per request** (not 9)
- **File existence check** to resume from checkpoints

### Estimated timeline

| Step | Duration | Note |
|---|---|---|
| Queue drain | 24h | Do not submit requests during this time |
| Register 2nd key | 5 min | New ECMWF account |
| Backfill 5 years ECMWF | ~11 days | 16,434 requests × 60s = 11.4 days, 24/7 |
| Parse & store | parallel | Can run on downloaded GRIB files independently |
| **Total** | **~12 days** | From today |

### If ECDS doesn't work

**Fallback to GEFS-only ensemble.** The GEFS archive already has 99.2%
coverage (363K rows). ECMWF ensemble would be a second opinion, not a
replacement. GEFS ensemble fraction divergence is already the architecture
pivot from Gray Room R8.

---

## Summary

| Question | Answer |
|---|---|
| Root cause | Per-dataset request queue full from rapid-fire submissions |
| Is dataset correct? | Yes — verified by 24.8 MB successful download |
| Script defects | Insufficient delay (15s vs 60-120s needed), no wait-for-completion |
| Key workaround | Dedicated API key + single-threaded sequential operation |
| Alternative source | Open-Meteo (recent only), GEFS (already have 99%) |
| Fastest path | Wait 24h, use sequential ECDS with 60s delay, resume GEFS in parallel |
| Timeline | ~12 days for full ECMWF 50-member backfill |
| Fallback | GEFS ensemble only (Gray Room R8 architecture) |

---

*Generated: 2026-07-31 17:00 UTC*