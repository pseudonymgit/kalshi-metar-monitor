# Official NWS CLI Historical Five-Station Pilot Specification

**Date:** 2026-08-15 UTC  
**Authorization:** bounded five-station pilot only; no bulk history, calibration, evaluator, production, or trading changes.  
**Stations:** KATL/ATL/FFC, KDFW/DFW/FWD, KMDW/MDW/LOT, KPHX/PHX/PSR, KSEA/SEA/SEW.

## Scope and exact fixtures

Attempt at most 12 report dates per station (60 requested reports total), stopping immediately if the official historical route is unavailable. Fixtures are chosen to cover summer, winter, transition, DST boundaries, and known date/duplicate-risk cases:

`2025-01-15` (winter), `2025-03-09` (US spring DST boundary), `2025-03-10` (post-DST), `2025-05-15` (transition), `2025-07-15` (summer), `2025-08-15` (summer), `2025-10-31` (transition), `2025-11-02` (US fall DST boundary), `2025-11-03` (post-DST), `2026-01-15` (winter), `2026-03-08` (US spring DST boundary), `2026-03-09` (post-DST).

No report is selected by “newest” unless its parsed local report date exactly equals the requested fixture. Product UUIDs and complete official API envelopes are immutable raw evidence. A duplicate/ambiguous fixture records all candidate product IDs and is not silently resolved.

## Official source and acquisition method

Only `https://api.weather.gov` and mapped `https://forecast.weather.gov/product.php?site=...&product=CLI&issuedby=...` are allowed. For each station/date, query the official product collection with an explicit time window around the target date:

`GET https://api.weather.gov/products?type=CLI&location={location}&start={date}T00:00:00Z&end={date+2}T23:59:59Z&limit=20`

Then request each returned exact official product UUID at `GET https://api.weather.gov/products/{uuid}` and retain only products whose parsed CLI report date equals the target local date. The list response, product response, HTTP status/headers, URL, retrieval time, and User-Agent are retained. No IEM, METAR, GHCN, LCD, WeatherAPI, or proxy substitute is permitted.

## Expected size/time

If historical products are exposed, expect 60 reports, approximately 4–8 KiB JSON each (roughly 240–480 KiB plus diagnostics), and under 10 minutes at a conservative official API rate. The first unavailable/empty/malformed official historical response is sufficient to stop; do not probe bulk alternatives or retry loops.

## Raw preservation and manifest

Raw bytes are preserved unchanged under `data/raw/nws_cli_historical_pilot/2026-08-15/` with `reports/` and `failures/` subdirectories. `manifest.json` records station, issuedby/location, requested report date, list URL, product URL/ID, issuance time, report date, bytes, SHA256, retrieval time, HTTP status, content type, and relevant HTTP metadata. Failed endpoint responses are retained verbatim with reason.

## Parser/audit contract

A read-only parser may be used only if an official report is acquired. It preserves raw text and extracts station mapping, product ID, issuance time, report date, local timezone, period label, valid-as-of/local semantics, preliminary/final/revision markers, TMAX/TMIN, and source URL. QC requires exact station mapping, requested date == parsed CLI report date, local-standard-time interpretation, no duplicate/silent selection, immutable hashes, and deterministic rerun byte-for-byte equality.

## Stop criteria

Stop and record exact response/reason when: official historical collection is HTTP non-200/empty; no exact-date CLI product is returned; response is malformed/non-CLI/HTML; station mapping mismatches; duplicate candidates cannot be resolved without a documented official rule; required date/TMAX/TMIN fields cannot be parsed; or any request would exceed the 60-report bound. Do not substitute a newer/current product.

## Comparisons and disposition

Only after raw CLI is frozen, compare audit-only to frozen GHCN and Kalshi records if exact fixture overlap exists. No evaluator/calibration/production/trading artifacts are changed. Metrics report overlap, exact agreement, mean absolute difference, and date/status mismatch counts; unavailable historical retrieval yields `BLOCKED` and identifies the smallest next official acquisition path.
