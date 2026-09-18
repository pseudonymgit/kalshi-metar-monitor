# Independent Calibration Label Source Audit — 2026-08-15

## Decision
**BLOCKED / PARKED. No locally available source passes the specification as calibration truth.** No frozen independent label artifact or builder was created. This is intentional: local candidates are observations, reanalysis, forecasts, derived aggregates, or scored settlement data, and substituting any of them would silently change the target.

## Candidate inventory

| Candidate | Local path/table | Owner/source | Raw fields and coverage | Relation to Kalshi settlement | Bias / decision |
|---|---|---|---|---|---|
| METAR archive | `data/metar_backfill.db::metar_observations` (12,823,140 rows; 73 stations; 2021-01-01–2026-08-15) | Ingested METAR reports | station, UTC timestamp, temp, raw METAR and QC fields | Observation stream; not the NWS CLI daily maximum/report cutoff/revision product | **Diagnostic proxy only.** Intraday sampling and UTC/local-day aggregation can miss CLI extrema and differ from official revisions. |
| Derived METAR daily stats | `data/metar_backfill.db::daily_stats` (58,103 rows; 30 stations; 2021-01-01–2026-08-15) | Locally derived from METAR archive | date_local/date_utc, max/min/avg, count, first/last observation | Explicitly a derived strike proxy, not immutable NWS CLI | **Rejected.** Building labels from this would be the prohibited METAR daily-max proxy substitution. |
| IEM ASOS 1-minute | `data/iem_asos_1min.db::observations` (5,380,236; 21 stations; local snapshot 2024-01-01–2026-08-04) | IEM ASOS observation feed | station, valid, tmpf, dewpoint, wind/pressure etc. | Independent observation archive, but no local evidence of CLI report identity, official daily aggregation, or revision history | **Diagnostic proxy only; insufficient equivalence/provenance.** |
| WeatherAPI archive | `data/weatherapi_archive.db::daily` (120,584; 20 stations; 2010-01-01–2026-08-01) and hourly | WeatherAPI cache | daily/hourly temperatures and metadata | Third-party/API-derived daily values; not NWS CLI settlement records | **Rejected as proxy.** |
| ERA5 | `data/era5_archive.db::era5_archive` (40,760; 20 stations; 2021-01-01–2026-07-31) | ECMWF reanalysis archive | daily max/min/mean t2m, hours, source | Gridded modeled/reanalysis estimate, not station CLI outcome | **Rejected as proxy.** |
| GEFS/TIGGE/NWP | `data/gefs_archive.db`, `data/tigge_archive.db`, `data/nwp_forecasts.db` | Forecast/reanalysis products | forecast ensemble values and metadata | Predictors, not outcomes | **Rejected.** |
| Kalshi settlement table | `data/kalshi_settlements.db::kalshi_settlements` (9,527; 21 stations; 2021-08-19–2026-08-12) | Kalshi/live API settlement records | station, series, event ticker, target date, kalshi_temp, market counts, source type | Scored settlement table itself | **Not independent by definition; prohibited as label source.** Used only for post-freeze audit, which was not run because no accepted source exists. |
| `settlement_epochs` | `data/metar_backfill.db::settlement_epochs` (98,640) | Engine-derived settlement epochs | intraday epoch/settlement fields | Derived from engine settlement pipeline | **Rejected / contaminated.** |

## Coverage and spot checks

The local Kalshi table has 21 stations and 9,527 rows. The locally derived METAR daily table overlaps cutoff-eligible Kalshi rows, but overlap is not evidence of semantic equivalence. A descriptive spot check found 3,373 cutoff rows with prior-day values available; 2,862 were non-flat under both series and only 117 agreed directionally (4.1% of jointly non-flat rows). This disagreement is consistent with a measurement/aggregation mismatch and further confirms that METAR daily max is not a safe substitute. It is not used to fit, alter, or select labels.

## Provenance/hash snapshot

SHA-256 at audit time:

```
95c6f77889cccc98de81b82fa5492272cc8c4a6d22ac320c7e86e2f11ac799ba  data/metar_backfill.db
592050387ce1ab5b59e7c987f188fda035c84df14faf3e5baa50374218e41f59  data/iem_asos_1min.db
b7195f6b8fe2eaf971e082c2625a13e44116404828cfa2e9da2afad1bcd36525  data/iem_asos.db
b78bad8f0b1ead62b2278639cc28b18723a5dee35b5fa613e08dce0bfec58297  data/weatherapi_archive.db
677701cc1887599fca7f2f507fd79b7b6898203b07b7a7976e6479a8c2929116  data/era5_archive.db
aa6a2de93261c51dc9674f002652dfe63497d59895430504f2e248f31d401f75  data/kalshi_settlements.db
```

These hashes document the audit inputs only; they do not turn proxies into accepted truth. No source-local immutable NWS CLI/CF6 archive, report IDs, revision manifest, or approved station mapping was found.

## Required acquisition path
Acquire and freeze an immutable historical NWS CLI/CF6 archive covering all target stations through 2026-02-28, preserving report IDs/revisions and official station mapping. Then rerun the specification gate, construct labels from raw CLI fields only, and perform the Kalshi comparison as a read-only audit.
