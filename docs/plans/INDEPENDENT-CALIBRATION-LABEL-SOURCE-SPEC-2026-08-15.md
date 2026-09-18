# Independent Calibration-Label Source Specification — 2026-08-15

## Status
**BLOCKED / PARKED.** This specification defines the gate; it does not authorize calibration, evaluator changes, production changes, shadow mode, or trading.

## Target semantics
The calibration target is the same directional question used by the weather engine, but must be measured from an independently sourced historical outcome: for station `s` and local trading date `d`, compare the authoritative daily outcome for `d` with the authoritative daily outcome for the strictly prior local date `d-1`.

* `delta = outcome(s,d) - outcome(s,d-1)` in °F.
* `label = 1` when `delta > epsilon`; `label = 0` when `delta < -epsilon`.
* `label = null` (ineligible, never silently coerced) when `abs(delta) <= epsilon` or either outcome is missing.
* `epsilon = 0.5°F`, matching the non-flat directional convention. Values must preserve the source's native precision and units; conversion must be explicit and reproducible.
* The source must represent the same NWS CLI settlement concept (daily maximum temperature and its station/day reporting rules), not merely a temperature observation or a model/reanalysis estimate.

## Acceptable source hierarchy
1. Immutable, versioned NWS CLI daily climate reports / CF6 or equivalent official archive containing the final station/day maximum and report identity.
2. An immutable historical outcome table independently acquired from NWS/NOAA with report IDs, revision/version metadata, and documented station/day semantics.
3. A separately governed official observation archive only if its daily aggregation and revision rules are demonstrated equivalent to the NWS CLI settlement definition.

METAR/ASOS extrema, WeatherAPI archives, ERA5/reanalysis, GEFS/NWP, forecasts, settlement epochs, scored settlement tables, and derived daily-stat tables are diagnostic/proxy sources unless equivalence to the NWS CLI process is independently proven. They must never be silently promoted to labels.

## Provenance and immutability
Every accepted source snapshot requires provider/owner, product name, acquisition date, source version or report revision, exact local path/table, raw-field mapping, and SHA-256 of every raw input plus a manifest hash. The builder must fail on source mutation, absent provenance, unexpected schema, duplicate station/date keys, or non-deterministic rerun.

## Station mapping
Use an explicit immutable mapping from engine/Kalshi station code to the official CLI station identifier, including station name/coordinates where available, mapping version, confidence, and effective dates. No fuzzy nearest-station mapping. Unknown or ambiguous mappings are missing and ineligible.

## Coverage and cutoff
Calibration training eligibility is `target_date <= 2026-02-28`. The accepted source must document coverage, gaps, revisions, and station/date overlap. A row may be emitted for any date only when both `d` and `d-1` are present and valid under the source rules. Future dates or post-cutoff rows may be retained only as explicitly non-training audit rows.

## Exact construction and fields
A frozen artifact would contain one row per station/date with: station, source station ID, target date, prior date, raw source record IDs, raw daily maximum fields, normalized maxima, delta, epsilon, label, missingness/QC flags, source version/hash, mapping version/confidence, and cutoff eligibility. Labels are computed solely from accepted source fields; the Kalshi settlement table may be joined only after freezing for descriptive validation.

## Validation, tolerance, and rejection
Run schema, provenance, mutation, duplicate, coverage, station-map, null/NaN, unit, deterministic-rerun, and artifact-hash tests. Agreement with Kalshi is an audit, not a construction input: report overlap, confusion/agreement, flat rate, lag/measurement mismatch, and revisions. Reject as calibration truth if the source is a proxy, lacks CLI-equivalent semantics, cannot prove independence from scored settlements, has unresolved station mapping, or has insufficient cutoff coverage. A high agreement rate cannot cure a proxy source.

## Stop condition
If no hierarchy-1/2/3 source is locally available with the required provenance and equivalence evidence, stop with **BLOCKED/PARKED**, do not build `data/independent_calibration_labels_20260815.json`, and recommend acquiring an immutable NWS CLI historical archive plus station mapping and revision metadata. No calibration fit or re-enable is permitted from this work.
