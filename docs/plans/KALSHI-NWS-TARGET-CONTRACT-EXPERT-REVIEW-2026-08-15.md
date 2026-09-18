# Kalshi/NWS target-contract expert review

**Date:** 2026-08-15 UTC  
**Scope:** GHCN/Kalshi target equivalence sprint  
**Repository:** `prototypes/weather-engine-source`  
**Disposition:** **Do not treat GHCN-Daily as an exact Kalshi settlement-label source yet.** It is a useful historical comparison candidate, but the current evidence supports only a station/date-level approximation and reveals source, cutoff, revision, and duplicate-row hazards.

## Executive conclusion

For the daily high/low temperature markets reviewed here, Kalshi says the authoritative source is the **final NWS Daily Climate Report (CLI)** named in the contract’s rules. The target is the report’s maximum/minimum temperature for the contract’s listed station and date—not a generic city temperature, a METAR-derived maximum, an arbitrary LCD field, or an unqualified GHCN-Daily TMAX.

The source chain should therefore be modeled as:

> **Kalshi contract/event → its settlement source/rules → NWS CLI issued for the exact `issuedby` station and local-standard-time climate date → reported integer °F extreme → Kalshi outcome/ladder interpretation.**

The repo has strong evidence for the station mapping and event rows, but not yet a frozen copy of every contract’s actual rules/source payload. The code’s `core/station_time.py` currently describes settlement as an approximation at midnight local time; that is not an adequate equivalence definition. NWS says the climate day is midnight-to-midnight **local standard time**, so during daylight time the effective window is 01:00–01:00 local daylight time. Kalshi additionally warns that the final report is normally issued the next morning and that determination can be delayed when the final value is inconsistent with METAR 6-hour/24-hour highs or is lower than a preliminary report.

**Recommendation:** use NWS CLI as the reference label for the pilot; use GHCN-Daily only as a separately flagged candidate/comparison series. Do not enable calibration or change production/trading paths from this document.

## Evidence status

| Claim | Status | Evidence / implication |
|---|---|---|
| Daily high/low temperature markets settle from the NWS Daily Climate Report | **Verified** | Kalshi Help Center, retrieved 2026-08-15. It explicitly says daily markets settle on the final NWS Daily Climate Report and that the market rules name the source. |
| Hourly temperature markets use The Weather Company, not NWS/GHCN | **Verified** | Kalshi Help Center. This review is for daily high/low markets only; do not mix hourly series into the target. |
| Target station is the contract’s named NWS climate station | **Verified at contract/example level; repo mapping is strong but should be re-frozen per event** | Kalshi examples identify Los Angeles Airport, Washington DC, Chicago Midway, Seattle, Philadelphia, etc.; repo `data/station_mapping.json` maps Kalshi series to NWS `issuedby` and CLI URLs. |
| NWS CLI is issued twice daily, with the full-day report shortly after local standard-time midnight / local daylight-time 01:30 | **Verified** | NWS Chicago FAQ. Product timing is operational guidance, not a guarantee of Kalshi determination time. |
| NWS climate day uses local standard time year-round | **Verified** | NWS Chicago FAQ. During DST the effective local clock window is 01:00–01:00 daylight time. |
| CLI is preliminary and can be revised; NCEI final/certified data arrive months later | **Verified** | NWS Chicago FAQ. This makes a later GHCN/NCEI value unsuitable as an automatic replacement for the value used by Kalshi. |
| Kalshi may delay determination for a final CLI high inconsistent with METAR 6-hour/24-hour highs or lower than a preliminary report | **Verified** | Kalshi Help Center. Preserve preliminary/final provenance and determination timestamps. |
| GHCN-Daily is an exact equivalent of NWS CLI | **Not verified / currently contradicted as an assumption** | GHCN integrates many source streams, applies source hierarchy and QA, updates daily, and can revise values. Its station ID and date alone do not prove same report semantics. |
| GHCN TMAX unit is tenths of °C | **Verified** | GHCN-Daily README. Conversion to °F is deterministic, but the resulting value need not equal the CLI/Kalshi integer °F label. |
| LCD can substitute for the NWS CLI | **Not verified** | LCD is a useful NCEI/NWS product and contains daily extremes, but the contract names CLI; LCD is a different product and may be final/quality-controlled on a different release schedule. |
| A duplicate `(station, target_date)` row is one target | **False for the current DB** | The DB contains both HIGH and LOW series for the same station/date. Rows are distinct contracts/market types; deduplication must include event/series and direction. |

## What Kalshi weather markets actually settle on

### Official product/source

For **daily** temperature markets, Kalshi’s current weather-market guidance says the final **NWS Daily Climate Report** is authoritative. It also says to follow the individual market’s rules because the rules name the settlement source. A public Kalshi market example states that the maximum temperature for the listed location/date is determined according to the NWS “Climatological Report (Daily)” and links the relevant NWS product.

The product commonly appears as `CLI` in NWS URLs, for example the repo’s mapping:

```text
https://forecast.weather.gov/product.php?site=FFC&product=CLI&issuedby=ATL
```

For **hourly** temperature markets, Kalshi’s guidance names The Weather Company and says NWS CLI, Google Weather, and other services are not authoritative. Those are a separate target family and should be excluded from this GHCN-Daily pilot.

### Station identity

The settlement identity is the station/observation site named by the contract’s rules and NWS `issuedby` code. A city label is not enough. The current repo mapping is a useful operational cache with 20 entries, including examples that matter for avoiding common city-to-airport mistakes:

- `KMDW` / `KXHIGHCHI` → **Chicago Midway**, not O’Hare.
- `KNYC` / `KXHIGHNY` → **Central Park / NYC climate site**, not JFK or a generic New York grid point.
- `KLAX` → Los Angeles Airport.
- `KDCA` → Reagan Washington National.
- `KDFW` → Dallas/Fort Worth; the ticker uses a `DAL` token but the NWS `issuedby` is `DFW`.
- `KMSP` → Minneapolis–St. Paul; the ticker uses `MIN`.

The repo file `data/station_mapping.json` records station name, Kalshi series, NWS office, NWS `issuedby`, and CLI URL. Treat it as a mapping cache, not as proof that all historical events used the same source. For every pilot row, retain the event ticker, series, market rules/settlement-source payload, NWS URL, station code, and retrieval timestamp.

### Local-day / cutoff semantics

The contract date is a local climate date. NWS’s FAQ defines a calendar-day climate summary as **midnight-to-midnight local standard time**. The NWS statement is explicit: daylight saving time does not move the climate boundary; during DST the reporting window is 01:00 to 01:00 local daylight time. Consequently:

- Do **not** define the target by UTC date.
- Do **not** use a fixed “next local midnight” as the complete semantic definition.
- Do use the station’s NWS climate date from the CLI report.
- For observations, the last hour of the displayed local DST day can belong to the preceding standard-time climate date.
- The CLI full-day report is normally released the following morning; report release/determination time is separate from the event’s target date.

The repo’s `core/station_time.py` currently says “settlement: midnight local time of the next day” and uses that for an entry window. That may be a scheduling approximation, but it must not be used as the label/equivalence rule. Kalshi’s own guidance says determination can occur after market close and may be delayed by source validation.

### Rounding and units

Kalshi daily examples resolve against integer Fahrenheit thresholds/ranges. NWS CLI reports the daily high/low in °F. The authoritative target should preserve:

1. the raw CLI integer °F value;
2. the exact report text/URL and retrieval timestamp;
3. the Kalshi event/market interpretation (for example, threshold or range outcome).

Do not reconstruct the target by taking a rounded METAR display. NWS explains that ASOS values can be converted from Fahrenheit to Celsius, rounded, and converted back, creating 1–2°F display differences; daily extrema can occur between METARs. This is a direct reason METAR scraping is not equivalent to CLI.

GHCN-Daily `TMAX` is an integer in tenths of °C. Convert numerically as:

```text
F = (TMAX_tenths_C / 10) * 9/5 + 32
```

Retain the raw integer, measurement flag, quality flag, source flag, and time-of-observation fields. Do not silently round the converted value into a purported Kalshi label. If comparing to CLI, use an explicitly documented comparison rule (for example, nearest °F only for descriptive error analysis) and retain the unrounded converted value.

### Revisions and determination timing

There are two different revision concepts:

1. **Kalshi/NWS operational revision:** Kalshi says daily markets use the final CLI and may delay determination in rare source-consistency cases. NWS says CLI products are preliminary and subject to revision; final/certified NCEI data are available later.
2. **GHCN/NCEI dataset revision:** NCEI says GHCN-Daily receives daily updates, is reconstructed each weekend, has versioned archives, and real-time U.S. sources are normally replaced with archive-ready sources 45–60 days after month end. Even archive-quality values can change later, although U.S. changes are rare after 60 days.

Therefore, “latest GHCN value” is not a stable historical proxy for “value Kalshi used to settle.” For auditability, freeze both a **settlement-time CLI snapshot** and the later GHCN file/version used for comparison. A later GHCN value can be a diagnostic of data quality or station mismatch, not an automatic relabeling authority.

### Duplicate settlement semantics

A database uniqueness key of `(station, target_date)` is wrong for the current market universe. The repo DB currently contains separate HIGH and LOW series on the same station/date. Examples include:

```text
KATL, 2026-05-24: KXHIGHTATL = 84°F; KXLOWTATL = 70°F
```

The correct identity at minimum is:

```text
(event_ticker or market family, station, target_date, direction/series)
```

The table has 9,527 unique `event_ticker` rows across `2021-08-19` to `2026-08-12` in the reviewed checkout, with both `historical_api` and `finalized` source types. A same-date HIGH/LOW pair is not a duplicate and must not be collapsed. Conversely, repeated ingestion of the same event ticker is a true duplicate and should be rejected or reconciled by immutable event identity.

One older validation report recorded a `TEST` station and a duplicate historical row; the current mapping/spec notes this development artifact was intended for removal. The expert review does not modify it. Pilot preparation should exclude `TEST`, verify distinct real stations against the 20-entry mapping, and report any remaining duplicate event tickers explicitly.

## Can CLI, LCD, and GHCN-Daily be equivalent?

### CLI: reference / settlement-equivalent candidate

**Yes, conditional on exact event mapping and retrieval.** The NWS CLI is the product Kalshi names for daily temperature settlement. It is the reference source for the pilot. “Equivalent” here means the value printed in the relevant final CLI report for the exact `issuedby` and climate date, not a value inferred from raw observations.

Caveat: the CLI is operationally preliminary and may be revised; the pilot must preserve the version available at/near Kalshi determination and any later revision. If a fetched URL exposes only the latest product, it is not an immutable historical record.

### LCD: not automatically equivalent

**No exact-equivalence claim.** NCEI describes LCD as a summary product from major airport/prominent stations containing daily temperature extremes and other observations. It is highly relevant for cross-checking and may contain the same underlying station value, but it is not the product named in the daily Kalshi rules. LCD’s final quality-controlled copy can also be delayed by one to two months. Use LCD only as a secondary reconciliation source unless a row-level comparison demonstrates the exact same value and station/date semantics.

### GHCN-Daily: not automatically equivalent

**No exact-equivalence claim.** GHCN-Daily is a multi-source integrated dataset. NCEI documents source hierarchies, mingling, QA flags, source flags, and differences between real-time and archive-quality U.S. data. The exact airport station mapping in this repo is a good prerequisite, but identical station coordinates plus a `TMAX` row do not prove identical report period, source, rounding, or revision state.

The current frozen audit is informative but not acceptance proof. It reports 3,138 paired observations through 2026-02-28 with 92.798% directional agreement and 0.747°F mean absolute temperature difference under the audit’s own prior-day pairing and `±0.5°F` flat rule. Results are highly station-dependent: `KAUS` 100% over 814 pairs, `KDEN` 99.69% over 321, `KDCA` 97.06% over 34, `KDFW` 37.5% over 16, and `KMDW` 86.53% over 1,463. These results demonstrate that station/date/source semantics matter; they do not establish that GHCN is the settlement truth.

## Current repo evidence and risks

### Positive evidence

- `data/station_mapping.json` has 20 mapped stations with Kalshi series, NWS `issuedby`, NWS office, station name, and CLI URL.
- The GHCN acquisition manifest is dated 2026-08-15, pins URLs, file hashes, and exact GHCN IDs; calibration remains disabled.
- Raw GHCN files preserve TMAX flags and have not been used to construct Kalshi labels.
- `data/kalshi_settlements.db` stores event ticker, station, target date, Kalshi temperature, source type, and source metadata.
- `core/kalshi_monitor.py` attempts station resolution from settlement-source metadata and ticker patterns, including the non-obvious ticker aliases.

### Risks that block exact equivalence

1. **Wrong product:** GHCN TMAX, METAR, LCD, and CLI are not interchangeable by name.
2. **Wrong station:** city/ticker tokens can differ from NWS `issuedby`; KMDW/KNYC/KDFW are especially important examples.
3. **Wrong day:** UTC date and local daylight clock date can misassign the NWS standard-time climate day.
4. **Wrong value:** METAR display rounding and inter-report extrema can differ from CLI.
5. **Wrong vintage:** final CLI used by Kalshi is not necessarily the current GHCN archive value.
6. **Wrong deduplication:** HIGH and LOW rows can share station/date but are different targets.
7. **Insufficient source capture:** a mapping URL alone does not archive the event’s actual rules/source payload or the report text used for settlement.

## Recommended five-station pilot acceptance matrix

Use one station from each of five distinct semantic/operational cases. The suggested pilot is **KAUS, KDEN, KMDW, KNYC, and KDFW**. This is deliberately not a random sample: it includes two high-agreement examples and three high-risk identity/coverage cases. Use only daily HIGH markets first; add LOW only after the event-key rule is validated.

For every row, capture: event ticker, series, market type, contract target date, Kalshi rules/source payload, settlement source URL, NWS office/`issuedby`, CLI report issue time, CLI reported high/low, any later CLI revision, GHCN ID, GHCN raw date/TMAX/flags/source, converted °F, LCD value if available, comparison result, and duplicate/ambiguity status.

| Station | Why selected | Required acceptance checks | Suggested pilot gate |
|---|---|---|---|
| **KAUS / Austin-Bergstrom** | Large existing overlap and clean audit result; validates the basic same-station path. | Contract says NWS CLI; `issuedby=AUS`; CLI climate date equals Kalshi target date; GHCN `USW00013904` is the exact airport; preserve TMAX flags and report vintages. | 100% event/source/date identity; CLI exact value available for every included event; GHCN-vs-CLI difference distribution reported, no silent imputation. |
| **KDEN / Denver International** | Large overlap and near-perfect audit result; western mountain/DST case with strong sample size. | Same as above, plus DST boundary dates and missing/late CLI handling; verify `issuedby=DEN`, not a nearby Denver station. | ≥99% CLI identity on eligible rows; zero UTC/local-day shifts; every DST boundary row manually reviewable. |
| **KMDW / Chicago Midway** | High-risk station identity: series label says Chicago but contract is Midway, not O’Hare; audit is materially weaker. | Prove `KXHIGHCHI` resolves to MDW; compare CLI `issuedby=MDW`; test dates where METAR-derived and CLI extrema differ; reject O’Hare substitution. | 100% station identity; zero use of KORD/O’Hare as fallback; disagreements must be attributable to report/day/revision/source flags before acceptance. |
| **KNYC / Central Park** | City label is especially dangerous: current mapping identifies Central Park, not JFK/LGA/EWR. | Prove the event’s exact NWS source and station; verify CLI issuedby and report text; compare GHCN candidate only if its station metadata represents the same site. | 100% source identity; no airport substitution; if GHCN station is not exact, classify as non-equivalent rather than impute. |
| **KDFW / Dallas–Fort Worth** | Ticker alias (`DAL`) differs from settlement station (`DFW`); small/poor current audit sample. | Verify ticker-to-source mapping from each event, `issuedby=DFW`, and GHCN `USW00003927`; investigate all >1°F / direction disagreements and any event duplication. | **No-go until ≥30 clean, exact CLI-linked events** and no unresolved alias/date issues; report confidence intervals/sample size rather than accepting 16-row evidence. |

### Pilot acceptance rules

- **Hard fail:** wrong NWS `issuedby`, wrong CLI date, wrong market direction, missing event rules/source, use of UTC date, or collapsing HIGH/LOW rows.
- **Reference-label pass:** exact CLI value and report provenance are captured for at least 95% of eligible events per station; remaining rows are explicitly missing/blocked, never imputed.
- **GHCN comparability pass:** only after reference-label pass; require a documented station/date/source/flags match and report station-level error/agreement. Do not require 100% numerical equality without first classifying revisions and product differences.
- **Pilot promotion:** all five stations pass identity/date/source gates, and KDFW reaches the minimum sample. Promotion still means “approved comparison source,” not “proven settlement-equivalent” unless the exact CLI/GHCN relationship is established.

## Accuracy-focused improvement recommendation

**Implement a provenance-first, event-level CLI snapshot and reconciliation ledger before any calibration use.** For each daily Kalshi event, store the exact contract/rules payload, event/series ticker, NWS office and `issuedby`, target climate date, CLI URL, raw CLI text/hash, issue/retrieval times, reported value, later revision if observed, and the GHCN/LCD comparison fields. Key rows by event ticker plus market direction, not station/date alone.

Why this improves accuracy: it attacks the dominant error modes directly—wrong station, wrong local-standard-time day, stale/revised source, and HIGH/LOW deduplication—rather than tuning a model against labels whose provenance is uncertain. It also permits a clean distinction between (a) a true weather forecast error, (b) a target-construction error, and (c) a later data revision. Keep calibration disabled while collecting this ledger.

## Sources (retrieved 2026-08-15 UTC)

1. Kalshi Help Center, **Weather Markets**: https://help.kalshi.com/en/articles/13823837-weather-markets  
   Daily source = final NWS Daily Climate Report; hourly source = The Weather Company; DST and preliminary/final caveats.
2. Kalshi Help Center, **Market Outcomes**: https://help.kalshi.com/en/articles/13823826-market-outcomes  
   Contract rules/source govern outcome; determination can be delayed after closure.
3. Kalshi public market example, Washington DC daily maximum temperature: https://kalshi.com/markets/kxhightdc/washington-dc-daily-max-temp/kxhightdc-26jul01  
   Example rule text names NWS Climatological Report (Daily).
4. Kalshi public market example, Chicago daily maximum temperature: https://kalshi.com/markets/kxhighchi/highest-temperature-in-chicago/kxhighchi-26jul02  
   Example identifies Chicago Midway, not O’Hare.
5. NWS Chicago, **Weather Observations and NWS Climate Products FAQ**: https://www.weather.gov/lot/weather_observations_faq  
   CLI definition/timing; preliminary vs final; local-standard-time climate day; METAR rounding and between-report extrema.
6. NWS Daily Climate Report example (CLI): https://forecast.weather.gov/product.php?site=lot&product=CLI&issuedby=ord  
   Example of the report product format and station/date summary.
7. NCEI, **Global Historical Climatology Network daily (GHCNd)**: https://www.ncei.noaa.gov/products/land-based-station/global-historical-climatology-network-daily  
   Daily updates, versioning, source streams, real-time/archive replacement, revisions, integration and QA.
8. NCEI, **GHCN-Daily README v3.34**: https://www.ncei.noaa.gov/pub/data/ghcn/daily/readme.txt  
   Fixed-width format; TMAX tenths °C; measurement/quality/source flags.
9. NCEI, **Local Climatological Data (LCD), Version 2 metadata**: https://www.ncei.noaa.gov/access/metadata/landing-page/bin/iso?id=gov.noaa.ncdc:C01689  
   LCD station/product description; useful secondary product, not the named CLI source.
10. Kalshi API documentation, **Get Events**: https://docs.kalshi.com/api-reference/events/get-events  
    Event response includes `settlement_sources`; retain this payload when available.

## Repository evidence inspected

- `data/station_mapping.json` (20-station mapping; version 2.2 in current checkout)
- `data/kalshi_settlements.db` (`kalshi_settlements` table; event-level settlement rows)
- `data/external/ghcn_daily/manifests/acquisition_manifest_20260815.json`
- `data/external/ghcn_daily/derived/ghcn_kalshi_target_equivalence_audit_20260815.json`
- `scripts/audit_ghcn_kalshi_target_equivalence.py`
- `scripts/audit_ghcn_daily_calibration_source.py`
- `scripts/validate_kalshi_settlement_sources.py`
- `core/kalshi_monitor.py`
- `core/station_registry.py`
- `core/station_time.py`
- `docs/plans/GHCN-DAILY-CALIBRATION-SOURCE-ACQUISITION-SPEC-2026-08-15.md`
- `docs/plans/GHCN-KALSHI-TARGET-EQUIVALENCE-AUDIT-SPEC-2026-08-15.md`

No source, evaluator, calibration, trading, scheduler, or bulk-data changes were made by this review.
