# NWS CLI/LCD Five-Station Pilot Acquisition Specification

**Date:** 2026-08-15 UTC  
**Scope:** bounded acquisition only; KATL, KDFW, KMDW, KPHX, KSEA  
**Status:** official NWS CLI pilot acquired (one current daily CLI product per station). No evaluator, calibration, trading, or bulk-acquisition changes were made.

## 1. Objective and guardrails

Acquire a small, auditable sample of official National Weather Service **CLI (Daily Climate Report)** products, preserving the raw response and provenance. This is not a backfill, model run, evaluator run, calibration update, or trading input change. Do not substitute Iowa Environmental Mesonet (IEM), METAR, GHCN, LCD files, or any other proxy for an official NWS CLI product.

The pilot is deliberately five stations × one current product (five product responses; 23,200 bytes total at acquisition). Any expansion requires a separate approval.

## 2. Repository mapping

The repository's authoritative station mapping is `data/station_mapping.json` and `core/station_registry.py`. Relevant entries:

| station | NWS CLI location/issuedby | NWS office/site | mapped settlement URL | mapped station name |
|---|---|---|---|---|
| KATL | ATL | KFFC / FFC | `https://forecast.weather.gov/product.php?site=FFC&product=CLI&issuedby=ATL` | Hartsfield-Jackson Atlanta International Airport |
| KDFW | DFW | KFWD / FWD | `https://forecast.weather.gov/product.php?site=FWD&product=CLI&issuedby=DFW` | Dallas/Fort Worth International Airport |
| KMDW | MDW | KLOT / LOT | `https://forecast.weather.gov/product.php?site=LOT&product=CLI&issuedby=MDW` | Chicago Midway International Airport |
| KPHX | PHX | KPSR / PSR | `https://forecast.weather.gov/product.php?site=PSR&product=CLI&issuedby=PHX` | Phoenix Sky Harbor International Airport |
| KSEA | SEA | KSEW / SEW | `https://forecast.weather.gov/product.php?site=SEW&product=CLI&issuedby=SEA` | Seattle-Tacoma International Airport |

The station-to-location mapping is also consistent with the current NWS product identifiers (`CLIATL`, `CLIDFW`, `CLIMDW`, `CLIPHX`, `CLISEA`) in the acquired responses. The existing `data/nws_cli_validation_*.json` artifacts are historical validation evidence only, not a source for this pilot.

## 3. Exact official endpoints

For each row, first request the official product index (no proxy):

- `https://api.weather.gov/products/types/CLI/locations/ATL`
- `https://api.weather.gov/products/types/CLI/locations/DFW`
- `https://api.weather.gov/products/types/CLI/locations/MDW`
- `https://api.weather.gov/products/types/CLI/locations/PHX`
- `https://api.weather.gov/products/types/CLI/locations/SEA`

Select the newest returned `@graph[0].id`, then request the exact official product endpoint:

- `https://api.weather.gov/products/{product_id}`

The corresponding human-facing official pages (useful for provenance/cross-check only) are the five mapped `forecast.weather.gov/product.php` URLs above. The API product response is JSON with `productText`; it is the preserved source artifact.

**Observed availability at 2026-08-15 07:18 UTC:** all five index endpoints returned HTTP 200 and recent products. Product issuance samples were KATL `2026-08-15T00:34:00+00:00`, KDFW `06:49`, KMDW `06:34`, KPHX `00:18`, KSEA `01:17`. The five exact product IDs and SHA-256 values are in `data/raw/nws_cli_lcd_pilot/2026-08-15/manifest.json`.

## 4. Date samples and temporal meaning

This pilot's newest products summarize the local calendar date **2026-08-14** (the report date), while requests were made on 2026-08-15 UTC:

- KATL: `2026-08-14`, `TODAY`, valid as of 8:00 PM local in the report.
- KDFW: `2026-08-14`, `YESTERDAY`.
- KMDW: `2026-08-14`, `YESTERDAY`.
- KPHX: `2026-08-14`, `TODAY`, valid as of 5:00 PM local.
- KSEA: `2026-08-14`, `TODAY`, valid as of 5:00 PM local.

Do not infer report date from UTC issuance date. Parse the station-local report date from the narrative and retain issuance time, validity/as-of text, and local time zone. CLI publication timing and same-day/incomplete wording vary by office.

## 5. Raw preservation layout

Acquired files are preserved without normalization under:

```text
data/raw/nws_cli_lcd_pilot/2026-08-15/
  manifest.json
  cli/
    {station}_{issuance-time-Z}_{nws-product-id}.json
  metadata/                 # reserved; no proxy data belongs here
```

The five raw JSON files contain the complete NWS API envelope (`@context`, `@id`, `id`, WMO ID, issuing office, issuance time, product code/name, and `productText`). Never overwrite a raw file; a re-fetch is a new dated acquisition directory or a new immutable filename.

## 6. Manifest and hash schema

`manifest.json` is JSON UTF-8 and contains:

```json
{
  "retrieved_at_utc": "RFC3339 timestamp",
  "source": "official NOAA NWS api.weather.gov",
  "artifacts": [{
    "station": "KATL",
    "location": "ATL",
    "list_endpoint": "HTTPS URL",
    "product_endpoint": "HTTPS URL",
    "product_id": "NWS UUID",
    "issuance_time": "RFC3339 timestamp",
    "path": "repository-relative raw path",
    "sha256": "64 lowercase hex characters",
    "bytes": 4687
  }]
}
```

Hash the exact bytes on disk (`sha256sum` equivalent), before parsing. A manifest row is valid only if station, location, product ID, endpoint, response hash, byte count, and raw path all agree. Record HTTP status, content type, retrieval timestamp, and request User-Agent in an expanded manifest for future runs; the current five rows retain the required provenance and content hash.

## 7. Parsing contract

Parse `productText` as an official semi-structured report while preserving the original text. Required normalized fields:

- `station`, `nws_location`, `product_code` (`CLI`), `product_id`, `wmo_collective_id`, `issuing_office`.
- `issuance_time_utc`, `report_date_local`, `local_timezone`, `valid_as_of_local` (nullable), and source `product_endpoint`.
- Report subject (`CLIMATE SUMMARY` station text), period label (`TODAY` or `YESTERDAY`), and `completeness_status` (`complete`, `as_of`, `unknown`).
- Temperature rows: `max_temp_f`, `max_observed_time_local`, `min_temp_f`, `min_observed_time_local`, `average_temp_f`.
- Context rows when present: `normal_temp_f`, `departure_from_normal_f`, `record_temp_f`, `record_year`, `last_year_temp_f`.
- Precipitation: daily value plus month-to-date/since-period values, retaining tokens such as `T`, `M`, and `MM` rather than coercing them to zero.
- Optional snow/degreeday fields, preserving raw token and parsed numeric value separately.
- `raw_text_sha256` and parser version.

Numeric parsing must honor NWS missing/trace markers (`M`, `MM`, `T`) and never silently convert them. Fahrenheit and local clock times are the canonical CLI values for this pilot. A downstream comparison must use the report's `report_date_local`, not the product issuance UTC date, and must explicitly account for `TODAY`/`YESTERDAY` and `VALID AS OF` wording.

## 8. Stop conditions

Stop immediately and preserve the failed response/diagnostic without retry loops if any of these occurs:

1. Any endpoint is not an official `api.weather.gov` or mapped `forecast.weather.gov` NWS endpoint.
2. HTTP status is non-200, response is empty, malformed, HTML/error content, or lacks a valid CLI product ID/text.
3. The index has no product for a station, the selected product is not `CLI`, or the location/product station code does not match the mapping.
4. A hash cannot be computed or the manifest does not verify byte-for-byte.
5. The report date, period, or required max/min fields cannot be parsed without guessing.
6. The request would exceed five stations × one current product, or would become historical/bulk acquisition without explicit scope approval.
7. Any request proposes IEM, METAR, GHCN, NCEI LCD, or another proxy as a substitute for official CLI.

Historical CLI retrieval is not assumed from the product index: this official API test clearly establishes current product availability, but the pilot must stop rather than invent a historical endpoint if a requested historical date is not directly available from an official NWS product record. A future historical sample should use exact NWS product UUIDs or another documented official archive route, one bounded date per station.

## 9. Accuracy-focused improvement suggestion

**Improve the acquisition/validation layer with report-date and publication-state parsing before any temperature comparison.** Existing validation artifacts show implausibly large and repeated station-specific mismatches (for example, a fixed KPHX value and KDFW value across many dates), consistent with selecting the wrong report date or reusing the newest CLI text rather than retrieving the requested historical product. Parse `report_date_local`, `TODAY`/`YESTERDAY`, `VALID AS OF`, and local observation time; require the requested date to equal the report date before comparing. This directly attacks label misalignment and prevents contaminating accuracy conclusions with a valid-but-wrong-day CLI report. It is accuracy-focused, low-risk, and does not change evaluator, calibration, or trading behavior.

## 10. Acquisition result

The bounded official pilot was available and acquired. Raw files and manifest are at `data/raw/nws_cli_lcd_pilot/2026-08-15/`. No IEM proxy was used. No bulk historical acquisition was run. No evaluator, calibration, or trading artifacts were modified.
