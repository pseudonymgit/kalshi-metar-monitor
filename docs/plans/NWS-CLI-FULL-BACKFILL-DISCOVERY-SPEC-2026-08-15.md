# NWS CLI Full Historical Backfill — Discovery and Conditional Implementation Specification

**Date:** 2026-08-15 UTC  
**Status:** **BLOCKED — discovery complete; no historical CLI route passed one-date QC**  
**Authorization:** discovery, one-date validation, QC, and documentation only. No calibration, evaluator, production, trading, or sweep changes. No bulk backfill was executed.

## 1. Target and non-negotiable source contract

The eventual target is the exact 20-station mapping in `data/station_mapping.json`:

`KATL, KAUS, KBOS, KDCA, KDEN, KDFW, KHOU, KLAS, KLAX, KMDW, KMIA, KMSP, KMSY, KNYC, KOKC, KPHL, KPHX, KSAT, KSEA, KSFO`.

The historical interval is inclusive from **2025-01-15 through 2026-02-28** (410 target report dates per station; 8,200 station/date pairs). The planning estimate is **30,000–40,000 historical report records** after publication revisions/duplicates, with approximately **150–250 MB** of immutable raw source material. These are estimates only, not an acquisition commitment.

A usable record must preferably be an official historical NWS **CLI (Daily Climate Report)** product with:

- immutable official product UUID (or an equally strong official immutable identifier);
- complete raw product response/text, retained byte-for-byte;
- product URL and retrieval metadata;
- station/location/product-code identity;
- issuance time, revision/version/preliminary/final metadata where present;
- parsed local report date, which must equal the requested target date;
- raw text hash and file byte count.

No value may be inferred from a current/newest product, another observation product, or a date selected by publication time. `TODAY`, `YESTERDAY`, `VALID AS OF`, and revision language must be retained and interpreted before any comparison.

## 2. Official-source hierarchy

The acquisition lane is ordered and deliberately narrow:

1. **NWS immutable product UUID/archive (preferred).** Use `https://api.weather.gov/products/{uuid}` for an official immutable product envelope and `productText`. The collection query `https://api.weather.gov/products?type=CLI&location=...&start=...&end=...` is only a discovery index; it is not evidence that an historical UUID exists when it returns an empty graph. NWS `forecast.weather.gov/product.php?...` pages are provenance/cross-check pages, not a substitute for a preserved UUID response.
2. **NCEI official CLI/LCD archive, only if semantics are proven.** An NCEI archive may be considered only after proving that it preserves the same CLI report semantics needed here: exact station/date, CLI text or an authoritative equivalent, report issuance/revision identity, and an immutable reproducible record. NCEI LCDv2 availability is verified below, but its CSV is a daily/monthly/hourly climate dataset, not an NWS CLI product and does not expose a CLI UUID/issuedby/revision record in the retrieved artifact. It therefore does **not** currently satisfy this target.
3. **BLOCKED.** If neither the UUID route nor a semantics-proven official archive is available, stop. Do not substitute IEM, METAR, GHCN, ISD, LCD, CD, or another proxy for settlement authority. **IEM is explicitly rejected as settlement authority unless Dan separately approves it as evidence-only.**

## 3. Discovery evidence (verified versus hypothesis)

Retrieval timestamps below are UTC and were captured on 2026-08-15.

### 3.1 NWS API and current product behavior — verified

- NWS API documentation: <https://www.weather.gov/documentation/services-web-api> (HTTP 200, fetched `2026-08-15T07:47:41Z`). It documents the public `api.weather.gov` service, required User-Agent, rate-limit expectations, and links to the OpenAPI specification.
- NWS OpenAPI: <https://api.weather.gov/openapi.json> (HTTP 200, fetched `2026-08-15T07:47:41Z`; API version `3.11.0`). The specification documents `/products` with `location`, `start`, `end`, `office`, `wmoid`, `type`, and `limit`, and `/products/{productId}` as the specific text-product endpoint.
- Existing current-product pilot: `data/raw/nws_cli_lcd_pilot/2026-08-15/manifest.json`. It proves current CLI availability only; it does not prove historical availability.
- Historical index probe already present in this repository: `data/raw/nws_cli_historical_pilot/2026-08-15/`. For the first fixture, `2025-01-15`, all five tested official collection requests returned HTTP 200 with `@graph: []`. Each raw response is 71 bytes and has SHA-256 `626005b1dc3947255e87d9c3c59357835ff66514e67f6b5c5b057f981ea1a80d`. The exact KATL request was:
  `https://api.weather.gov/products?type=CLI&location=ATL&start=2025-01-15T00:00:00Z&end=2025-01-17T23:59:59Z&limit=20`
- The five-station result is an availability failure, not proof that no historical record exists anywhere. No UUID was returned, so no `/products/{uuid}` request could be made.

### 3.2 NCEI LCDv2 — official availability verified; CLI semantic equivalence rejected

- NCEI publications overview: <https://www.ncei.noaa.gov/products/land-based-station/data-publications> (HTTP 200, fetched `2026-08-15T07:47:41Z`). It says LCD publications contain daily temperature extremes and other climate summaries, and that ASCII text files are no longer available for the dynamically generated LCD product.
- LCDv2 metadata: <https://www.ncei.noaa.gov/access/metadata/landing-page/bin/iso?id=gov.noaa.ncdc%3AC01689> (HTTP 200, fetched `2026-08-15T07:47:41Z`). It identifies LCDv2 as NOAA/NCEI dataset `gov.noaa.ncdc:C01689`, DOI `10.25921/96dw-mb77`, coverage 2005–present, daily update frequency, CSV distribution, and direct download resources.
- LCDv2 product page: <https://www.ncei.noaa.gov/products/land-based-station/local-climatological-data> (HTTP 200, fetched `2026-08-15T07:47:46Z`). It describes Daily Summaries, Hourly Observations, and monthly-form structure; it does not describe immutable NWS CLI product UUIDs or CLI revision history.
- NCEI official object-store listing: `https://www.ncei.noaa.gov/oa/local-climatological-data/?list-type=2&prefix=v2%2Faccess%2F2025%2F&delimiter=%2F&max-keys=1000` (HTTP 200, fetched `2026-08-15T07:49:51Z`; raw listing SHA-256 `fe4109d7635993ed7e8cd7abfeec2cb23ad480fdec42dd05b15cdff01e7f7fdc`). The listing exposes official yearly station CSVs, including `LCD_USW00013874_2025.csv`.
- Official KATL-aligned NCEI station: `USW00013874`, “ATLANTA HARTSFIELD INTERNATIONAL AIRPORT, GA US,” identified from the official station list <https://www.ncei.noaa.gov/oa/local-climatological-data/v2/doc/lcdv2-station-list.txt> (retrieved during this discovery; station list is preserved by URL evidence, not copied into the repository).
- Candidate official file: `https://www.ncei.noaa.gov/oa/local-climatological-data/v2/access/2025/LCD_USW00013874_2025.csv` (HTTP 200, `content-type: text/csv`, `content-length: 8823529`, `last-modified: 2026-05-05T21:01:30Z`, fetched `2026-08-15T07:49:51Z`). The exact bytes are preserved at `data/raw/nws_cli_full_backfill_discovery/2026-08-15/official_ncei_lcd/USW00013874_2025.csv`, 8,823,529 bytes, SHA-256 `15440946ec50d6090cf0188cae7e5b48815917965553e1404514f5b0499fd011`. A second fetch was byte-identical with the same hash.

**Interpretation:** NCEI LCDv2 is an official historical climate archive and its KATL file contains an exact `2025-01-15` daily row. However, that row is `REPORT_TYPE=SOD`, has no NWS CLI product UUID, no NWS CLI `issuedby`, no CLI raw narrative, and no publication revision identity. Its temperature fields are useful as an official archive discovery fact only; they are not accepted as a CLI settlement record. This is a verified official archive hypothesis that fails the required CLI semantic gate.

### 3.3 AFOS/tgftp and search results — no usable official historical route verified

Searches for an official NWS AFOS/text-product historical archive did not identify a documented NOAA/NWS route exposing immutable historical CLI UUIDs or complete historical CLI text. `tgftp.nws.noaa.gov` exposes current/raw product paths but no verified historical CLI archive was found in this discovery. IEM search results do expose historical AFOS/CLI text, but IEM is a mirror and is explicitly excluded from settlement authority. This lane remains **BLOCKED**, not silently promoted.

## 4. Exactly-one-date validation and QC

### Requested validation

- Station: **KATL**
- Target local report date: **2025-01-15**
- Preferred route: official NWS CLI collection followed by an immutable UUID product fetch.

### NWS route result

The exact official collection request returned HTTP 200 and a valid JSON-LD envelope with zero candidates (`@graph=[]`). Therefore there was no UUID, raw CLI text, issuance/revision metadata, or report to parse. The preserved raw response and manifest are under `data/raw/nws_cli_historical_pilot/2026-08-15/`.

### NCEI route result

The official NCEI LCDv2 KATL yearly CSV was fetched and preserved. Its exact target-date row has:

- station: `USW00013874` (KATL-aligned Atlanta Hartsfield airport);
- `DATE`: `2025-01-15T00:00:00`;
- `REPORT_TYPE`: `SOD`;
- `NAME`: `ATLANTA HARTSFIELD INTERNATIONAL AIRPORT, GA US`;
- `DailyMaximumDryBulbTemperature`: `11.1` °C;
- `DailyMinimumDryBulbTemperature`: `1.1` °C;
- `DailyAverageDryBulbTemperature`: `6.1` °C;
- no `product_id`, UUID, NWS `issuedby`, CLI `productCode`, raw CLI narrative, issuance time, or revision field.

The date gate itself passes for an official NCEI daily row, but the source-type/identity gate fails: **this is LCDv2 SOD data, not a CLI product**. Thus one-date CLI validation is **FAIL/BLOCKED**, not accepted.

### QC disposition

- Official host and HTTPS: PASS for both NWS and NCEI evidence.
- Exact KATL station/date identity: PASS for NCEI row; NWS has zero candidates.
- Exact CLI product code and raw CLI narrative: **FAIL**.
- Immutable UUID/report issuance/revision metadata: **FAIL** for NCEI; unavailable from NWS empty index.
- Byte preservation/hash: PASS for all preserved artifacts.
- Proxy/substitute use: PASS — no IEM, METAR, GHCN, ISD, or other proxy was used.
- Full-backfill readiness: **NOT READY**.

## 5. Conditional resumable full-backfill implementation/spec (not executed)

This section defines what may be implemented only after an official route passes the one-date gate and is explicitly accepted. It is a design, not permission to acquire.

### Inputs and checkpoints

- Freeze a route manifest containing the exact 20 stations, NWS location/issuedby/site mapping, start/end dates, route version, and acceptance decision.
- Generate deterministic station/date work items for 20 × 410 = 8,200 target pairs.
- Use an append-only checkpoint/manifest (SQLite or JSONL plus atomic replacement) keyed by `(station, report_date_local, product_uuid, revision_key)` with states `pending`, `discovered`, `fetched`, `validated`, `duplicate`, `failed`, `blocked`.
- Checkpoint after every successful immutable raw write and before parsing. Resume must never overwrite an existing raw object; a retry creates a separately timestamped attempt record while retaining the same content hash if identical.
- Dry-run must report exactly 8,200 target pairs, expected candidate/revision counts only after a bounded index scan, and predicted bytes without downloading product bodies.

### Discovery/fetch order and rate limits

- Query only official NWS/NCEI endpoints accepted in the route decision. For NWS, use explicit date windows and pagination/limit handling; do not select newest/current implicitly.
- Use a conservative serialized request rate (recommendation: one request per second unless the accepted official documentation permits otherwise), honor `Retry-After`, and cap exponential backoff at 5 retries for transient 429/5xx/timeouts. Persist every failed response/diagnostic.
- Do not retry 4xx semantic failures, empty historical indexes, malformed payloads, or station/date mismatches as if they were transient.
- Fetch each candidate immutable UUID exactly once per manifest generation; retain complete response bytes, URL, status, content type, request User-Agent, retrieval time, and response headers.

### Immutable raw layout and manifest

```text
data/raw/nws_cli_full_backfill/YYYY-MM-DD/
  manifest.jsonl
  checkpoints.sqlite
  reports/{station}/{report_date}/{issuance-or-revision}_{uuid}.json
  indexes/{station}/{report_date}/attempt-*.json
  failures/{station}/{report_date}/attempt-*.json
```

Each manifest record must include station, NWS location/issuedby/site, requested local date, candidate UUID, product endpoint, issuance time, revision/version/preliminary-final fields, raw relative path, exact byte count, SHA-256, HTTP status/content type, retrieval time, parser/QC version, and disposition. The manifest is append-only; duplicate content hashes are linked, not deleted.

### Duplicate/revision model

- A UUID is the immutable product identity.
- A same-station/date set may contain multiple UUIDs or revisions. Preserve every candidate and classify as `initial`, `revision`, `duplicate-content`, or `ambiguous` only when the official payload provides enough evidence.
- Never collapse revisions by newest retrieval time. Keep all raw products and designate a canonical record only through a documented official revision rule after QC acceptance.
- If two products disagree on TMAX/TMIN or report date and no official revision rule resolves them, mark the pair `ambiguous` and stop expansion for that station/date.

### Validation/QC gate before scale

For the accepted route, fetch and validate exactly KATL/2025-01-15 first. Require: official host; product code CLI; exact station mapping; exact local report date; non-empty raw text; UUID; issuance/revision metadata; parsable TMAX/TMIN; preserved raw bytes/hash; and manifest round-trip verification. Then run a bounded second review (recommended 5–12 additional fixture pairs) before any full download. Only an explicit acceptance of that QC may authorize full acquisition.

## 6. Estimate and operational disposition

Planning estimate for 8,200 target dates and approximately 30,000–40,000 report/revision bodies is 150–250 MB raw, plus 5–15% manifests/checkpoints/logs. At a conservative one request/second effective rate, a UUID-based run would likely take several hours to a day depending on candidate counts, retries, and archive latency; this is an estimate, not a schedule or authorization.

**Disposition:** **BLOCKED.** The official NWS historical collection did not expose a KATL 2025-01-15 UUID, and the verified NCEI LCDv2 route is not proven CLI-equivalent. No bulk download occurred.

**Smallest next input:** one official immutable historical NWS CLI product UUID for **KATL on 2025-01-15**, or a documented NOAA/NWS immutable archive route exposing the exact CLI text plus issuance/revision identity. Once supplied, fetch only that one product, rerun the exact QC above, and seek route acceptance before expanding.

## 7. Accuracy-focused improvement suggestion

Add a hard source/date/publication-state gate before any accuracy metric: require `productCode=CLI`, exact local `report_date`, station mapping, and parsed `TODAY`/`YESTERDAY`/`VALID AS OF` plus revision state. Reject valid-but-wrong-day current CLI text and reject LCD/GHCN/METAR rows as non-CLI. This directly addresses the existing validation artifacts' suspicious repeated station values and prevents a source-semantic mismatch from being misreported as forecast or settlement error.

## 8. Scope exclusions

No calibration/evaluator/production/trading/sweep artifact was changed. Existing `data/nws_cli_validation_*.json` files were inspected as historical evidence only and were not accepted as source data. IEM was not used; it remains evidence-only pending separate approval by Dan.
