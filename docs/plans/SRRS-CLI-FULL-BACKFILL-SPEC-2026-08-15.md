# NCEI SRRS CLI Full Historical Backfill Specification

**Date:** 2026-08-15 UTC  
**Status:** frozen acquisition specification; descriptive/read-only  
**Scope:** official NCEI Service Records Retention System (SRRS) ANX HTTPS archive only

## 1. Objective and non-goals

Acquire the original NWS Daily Climate Report (CLI) bulletins for the exact 20 station/date keys used by the weather-engine target contract. The acquisition must preserve archive locator, scan row, exact byte range, response metadata, raw bytes, SHA-256, report parsing, revisions, missingness, and unresolved ambiguity. This is provenance and QC work only: no evaluator, calibration, production, trading, signal, sweep, or source-acceptance enablement is authorized by this document.

The official route is `https://www.ncei.noaa.gov/data/service-records-retention-system/access/anx/`. The archive has date/hour directories, `.ANX` bundles, and companion `.scan` indexes. Only `.scan` indexes and exact HTTP byte ranges may be fetched; whole ANX bundles are prohibited.

## 2. Frozen station universe and target contract

The mapping below is copied from `data/station_mapping.json` and is frozen for this run. No city, airport, WMO, PIL, NWS office, or GHCN substitution is permitted.

| station | city | NWS office/site | issuedby/PIL | expected WMO office token | Kalshi series |
|---|---|---|---|---|---|
| KATL | Atlanta GA | FFC | ATL / CLIATL | KFFC | KXHIGHTATL |
| KAUS | Austin TX | EWX | AUS / CLIAUS | KEWX | KXHIGHAUS |
| KBOS | Boston MA | BOX | BOS / CLIBOS | KBOX | KXHIGHTBOS |
| KDCA | Washington DC | LWX | DCA / CLIDCA | KLWX | KXHIGHTDC |
| KDEN | Denver CO | BOU | DEN / CLIDEN | KBOU | KXHIGHDEN |
| KDFW | Dallas/Fort Worth TX | FWD | DFW / CLIDFW | KFWD | KXHIGHTDAL |
| KHOU | Houston TX | HGX | HOU / CLIHOU | KHGX | KXHIGHTHOU |
| KLAS | Las Vegas NV | VEF | LAS / CLILAS | KVEF | KXHIGHTLV |
| KLAX | Los Angeles CA | LOX | LAX / CLILAX | KLOX | KXHIGHLAX |
| KMDW | Chicago Midway IL | LOT | MDW / CLIMDW | KLOT | KXHIGHCHI |
| KMIA | Miami FL | MFL | MIA / CLIMIA | KMFL | KXHIGHMIA |
| KMSP | Minneapolis MN | MPX | MSP / CLIMSP | KMPX | KXHIGHTMIN |
| KMSY | New Orleans LA | LIX | MSY / CLIMSY | KLIX | KXHIGHTNOLA |
| KNYC | New York NY | OKX | NYC / CLINYC | KOKX | KXHIGHNY |
| KOKC | Oklahoma City OK | OUN | OKC / CLIOKC | KOUN | KXHIGHTOKC |
| KPHL | Philadelphia PA | PHI | PHL / CLIPHL | KPHI | KXHIGHPHIL |
| KPHX | Phoenix AZ | PSR | PHX / CLIPHX | KPSR | KXHIGHTPHX |
| KSAT | San Antonio TX | EWX | SAT / CLISAT | KEWX | KXHIGHTSATX |
| KSEA | Seattle WA | SEW | SEA / CLISEA | KSEW | KXHIGHTSEA |
| KSFO | San Francisco CA | MTR | SFO / CLISFO | KMTR | KXHIGHTSFO |

Target summary dates are **2025-01-15 through 2026-02-28 inclusive** (410 dates x 20 = 8,200 station/date pairs). Planning estimate is 30,000–40,000 report/revision bodies and approximately 150–250 MB of raw report bytes; these are estimates, not completeness assumptions.

## 3. Date/hour search and identity rules

For each target local calendar date `D`, search archive UTC date directories `D-1`, `D`, and `D+1`, every available hour directory listed by NCEI. The next-day directory is required because KATL `D=2025-01-15` issued at `2025011601Z`. The three-day window is a bounded operational policy; a report outside it is recorded as out-of-window and is not silently substituted. Fetch and cache the directory listing, then fetch one `.scan` for every candidate `.scan` file in the date/hour directories. No ANX bundle listing or body is downloaded.

Parse `.scan` rows as `Filename,Start Index,Byte Range,WMO Header`. A row is a candidate only when its WMO header contains the frozen NWS office token and has a `CDUS` climate-report family. The row's `Start Index` is zero-based and `Byte Range` is the exact byte count. Fetch `Range: bytes=start-(start+length-1)` from the exact `.ANX` URL. Require HTTP 206, exact `Content-Range`, and exact body length. A 200 response, short body, malformed range, or inconsistent content range is a failed attempt, never a whole-bundle fallback.

After extraction, require both the frozen WMO office token and exact PIL (`CLI{issuedby}`) in the bulletin header. Reject and retain as non-target if station/PIL does not match. Parse the internal summary date from the bulletin. The internal summary date, not archive directory date, UTC request date, filename date, or issuance date, controls target identity. A report with a different internal summary date is never relabeled to `D`.

## 4. Intermediate, final, and revision policy

Retain every matching CLI body. Classify each body as `intermediate` when it explicitly carries `VALID AS OF`/`VALID TODAY AS OF` or equivalent preliminary cutoff language, and `full_day` when it reports the completed day's summary without that intermediate marker. Keep a separate record for every issuance/revision, including identical-content duplicates; never overwrite a prior body.

A candidate is eligible for the settlement-facing descriptive view only if its internal summary date equals `D`, station/PIL/WMO identity passes, and its class is known. The preferred class is `full_day`, otherwise `intermediate` is retained as `no_full_day_observed`. Among candidates of the preferred class, select only an explicitly latest issuance when issuance timestamp and revision lineage are unambiguous. If multiple distinct candidates remain without a documented finality/revision rule, the station/date is `blocked_ambiguous`; it is not resolved by file order, latest retrieval, non-null value, or arbitrary SQL order. The full candidate set remains in the evidence ledger.

Report parser extracts maximum and minimum Fahrenheit values only from the temperature table, preserving `MM`, `T`, negative values, raw text, and parse status. Report values are labels for audit, not enabled evaluator labels.

## 5. Rate limiting, retries, and cache

Requests are serialized globally. Default inter-request delay is 0.20 seconds, with a minimum delay of 0.05 seconds for a controlled QC run. Use a descriptive User-Agent naming the official SRRS backfill and a contact only when configured. Cache successful directory listings, `.scan` bytes, and exact raw bodies by immutable URL/range key. Do not re-fetch a cached success on resume.

Retry transient network errors, HTTP 408/425/429/500/502/503/504, and malformed transient responses up to 5 attempts with exponential backoff capped at 120 seconds. Honor numeric `Retry-After` (and HTTP-date where parseable) before retrying. Do not retry permanent 404/403 except to record the failed attempt. Every attempt is appended to the manifest/checkpoint with status, bytes, headers, error, and retry number.

## 6. Resume/checkpoint and append-only provenance

Output root is `data/raw/srrs_cli_backfill/2026-08-15/` with `scans/`, `raw/`, `reports/`, `failures/`, `manifest.jsonl`, `checkpoint.json`, and `run_summary.json`. The manifest is append-only JSONL. Each station/date is a deterministic key. Checkpoint updates atomically after each completed station/date and records script/spec hashes, configuration, and status. A resumed run skips only keys whose prior terminal record and all referenced artifacts pass local hash/size validation. It never treats an interrupted or failed request as success.

Raw body filenames contain station, target date, archive bundle, start, length, and body hash. Existing raw bytes are immutable: if a path exists with different bytes, stop with a corruption failure. Scan bytes and all report bodies are independently SHA-256 hashed. Attempts/missing/ambiguous rows are preserved in manifest and `failures/`; no silent substitution from IEM, METAR/ASOS, GHCN, LCD, WFO UI, API UUID, or any proxy is allowed.

## 7. Missing, out-of-window, and ambiguity policy

If no matching CLI exists after all configured date/hour scans, record `missing_no_cli` with every searched directory, scan count, candidate count, and request outcomes. If scan/list access is unavailable, record `blocked_archive_access`; it is not equivalent to missing. If a matching bulletin exists but fails internal date or identity checks, record the rejection with raw evidence. If multiple distinct settlement candidates or revisions cannot be resolved under Section 4, record `blocked_ambiguous`. Missing values remain missing; no fabricated label, carry-forward, neighboring-station value, or alternate product is emitted.

## 8. Phase gates and acceptance criteria

**Phase 0:** this spec exists, hash is recorded, exact 20-station mapping and target range are frozen.

**Phase 1/QC:** extractor compiles and focused tests pass: official host/path; `.scan` row/range integrity; KATL WMO/PIL/station match; internal summary date; no UTC-date substitution; raw immutability; deterministic rerun. The preserved proof must reproduce exactly: KATL target raw `4,407` bytes, SHA-256 `6eaa594496635d54a80e1323a45f255f971d04195dfcb9432825c24f1f4a05b4`, maximum `52°F`, minimum `34°F`, internal date 2025-01-15.

**Phase 2:** bounded serialized full backfill completes or leaves explicit resumable checkpoint. Every one of 8,200 station/date keys has terminal `found`, `missing_no_cli`, or `blocked_*` disposition. All report/revision bodies and attempts have provenance and hashes. “Complete” means accounted-for, not that all reports exist.

**Phase 3:** after raw freeze, run a read-only three-way audit against frozen GHCN-Daily and frozen Kalshi settlement inputs. Report coverage; exact station/date/product identity; intermediate/full-day/revisions; duplicate settlement classification; CLI-vs-GHCN absolute/directional differences; CLI-vs-Kalshi agreement; and unresolved rows. Canonical output must be byte-identical and hash-identical on two runs.

Source acceptance remains separate from this acquisition/audit. Calibration stays disabled fail-closed. No sweep, evaluator, production, deployment, capital, or trading action is part of acceptance.

## 9. Artifact and receipt requirements

The final receipt at `.meta/continuity/weather-engine/2026-08-15-srrs-cli-backfill.md` must contain exact commands, script/spec hashes, UTC run interval, counts for station/date statuses, candidate/revision/body counts, raw bytes, missing/ambiguous/blocked counts, manifest/checkpoint/report hashes, KATL QC results, audit commands and two-run hashes, audit disposition, and one accuracy-focused suggestion. Update `TASKS.md`, `PROJECTS.md`, and `docs/reference/SUBAGENT-QUALITY-JOURNAL.md` only after verified artifacts exist; those ledger edits must not imply calibration or source acceptance.
