# Same-Station Mixed-Behavior Packet

## Task Type
IMPLEMENTATION (docs-only report artifact)

## Exact Question
What is the exact minimal same-station mixed-behavior capture packet when one station shows both a real ladder-backed alert and a ladder-missing / filtered-to-zero event within a short interval?

## Verified Current Reality
- Repository: `pseudonymgit/kalshi-metar-monitor`
- Verified branch at report creation: `docs/same-station-mixed-behavior-packet`
- Verified commit at report start: `add16fbbad7049e486730a6255bea880f781ae39`
- Allowed mutation scope: `docs/reports/2026-03-22_same_station_mixed_behavior_packet.md` only.

## Evidence Base

### 1. Same-station mixed behavior is a live diagnosis target
- `docs/CURRENT_STATE.md` explicitly records that real alerts are occurring, ladder-missing alerts are also occurring, and the same city/station family can show both behaviors.
- File: `docs/CURRENT_STATE.md`
- Lines: 26-31
- RAW_URL: https://raw.githubusercontent.com/pseudonymgit/kalshi-metar-monitor/main/docs/CURRENT_STATE.md

### 2. The smallest historical proof surface is `alert-preview`
- `app.py` shows `/observability/alert-preview` emits compact recent alert rows with the exact fields operators need to compare two events quickly: `station`, `created_utc`, `alert_type`, `market_type`, `direction`, `event_ticker`, `bucket_index`, `metadata`, `reason`, `attention_phrase`, and `payload_preview.temp_f`.
- File: `app.py`
- Lines: 3195-3249
- RAW_URL: https://raw.githubusercontent.com/pseudonymgit/kalshi-metar-monitor/main/app.py

### 3. Real ladder-backed alert proof is persisted as `composed_alert_sent`
- `core/metar_monitor.py` records a successful real market alert by auditing `alert_type="composed_alert_sent"` with `event_ticker`, transition `reason`, `attention_phrase`, and `alert_context`.
- File: `core/metar_monitor.py`
- Lines: 3182-3233
- RAW_URL: https://raw.githubusercontent.com/pseudonymgit/kalshi-metar-monitor/main/core/metar_monitor.py

### 4. Bad-path proof is persisted as `ladder_missing` or `ladder_selection_empty`
- `core/metar_monitor.py` records same-station bad-path alert rows with:
  - `alert_type="ladder_missing"` when the cache is truly missing/empty, or
  - `alert_type="ladder_selection_empty"` when directional selection collapses.
- The same audit write persists `metadata.empty_reason`, `metadata.cause`, and `metadata.explanation`, which is the smallest durable reason bundle for fast review.
- File: `core/metar_monitor.py`
- Lines: 3079-3144
- RAW_URL: https://raw.githubusercontent.com/pseudonymgit/kalshi-metar-monitor/main/core/metar_monitor.py

### 5. Cause mapping is deterministic
- `core/metar_monitor.py` maps:
  - `cache_missing_or_empty -> market_cache_empty`
  - `filtered_to_zero -> market_filters_removed_all_candidates`
  - `no_directional_ladder_match -> directional_ladder_mismatch`
- File: `core/metar_monitor.py`
- Lines: 191-205
- RAW_URL: https://raw.githubusercontent.com/pseudonymgit/kalshi-metar-monitor/main/core/metar_monitor.py

### 6. The compact normalization surface for filtered-zero interpretation is `market-eligibility-runtime`
- `app.py` shows `/observability/market-eligibility-runtime` normalizes `no_directional_ladder_match` to `filtered_to_zero`, exposes `latest_evaluation_outcome`, `latest_suppression_reason`, and the compact `current_cache_probe` counts needed to distinguish cache-empty from filter-collapse.
- `docs/API_REFERENCE.md` documents the same operator interpretation: zero post-directional ladders should be treated as `NO_ELIGIBLE_MARKET / filtered_to_zero` unless code reality changes.
- File: `app.py`
- Lines: 1452-1523
- RAW_URL: https://raw.githubusercontent.com/pseudonymgit/kalshi-metar-monitor/main/app.py
- File: `docs/API_REFERENCE.md`
- Lines: 1115-1129
- RAW_URL: https://raw.githubusercontent.com/pseudonymgit/kalshi-metar-monitor/main/docs/API_REFERENCE.md

### 7. `alert-path-truth` and `alert-decision-trace` are supporting, not minimal, for this packet
- `app.py` shows both endpoints are latest-state surfaces. They are useful when a reviewer wants the current blocking stage or latest send-alert truth, but they are not the smallest historical proof surface for pairing one good and one bad event from the same station.
- File: `app.py`
- Lines: 3277-3414
- RAW_URL: https://raw.githubusercontent.com/pseudonymgit/kalshi-metar-monitor/main/app.py

## Verdict
Use one **minimal same-station three-block packet**:
1. **Station/window header** to prove the two rows belong to one station and a short interval.
2. **Good-event block** from `alert-preview` to prove a real ladder-backed alert happened for that station.
3. **Bad-event block + bad-side normalization block** to prove the same station also produced a ladder-missing / filtered-to-zero event and to classify which bad path actually occurred.

This is tighter than sibling-station comparison because the good row from the same station already proves that the station and process were capable of real ladder-backed alerting in the same short window.

## Minimal Required Packet

### A. Station / window header
Required source:
- `GET /observability/alert-preview?station=<ICAO>&limit=10`

Required fields:
- `station`
- `good_event_created_utc`
- `bad_event_created_utc`
- `delta_minutes_between_events`
- `capture_window_utc`

Why required:
- Without these fields, the packet cannot prove the comparison is truly same-station and same-window.

### B. Good-event block
Required source:
- `GET /observability/alert-preview?station=<ICAO>&limit=10`

Required fields:
- `created_utc`
- `alert_type`
- `market_type`
- `event_ticker`
- `reason`
- `direction`
- `bucket_index`
- `attention_phrase`
- `payload_preview.temp_f`

Preferred qualifying row:
- `alert_type = composed_alert_sent`

Why required:
- This is the smallest persisted proof that the station produced a real ladder-backed market alert rather than just a generic transition or summary artifact.
- `event_ticker` is the compact ladder-backed proof field.

### C. Bad-event block
Required source:
- `GET /observability/alert-preview?station=<ICAO>&limit=10`

Required fields:
- `created_utc`
- `alert_type`
- `market_type`
- `metadata.empty_reason`
- `metadata.cause`
- `metadata.explanation`
- `payload_preview.temp_f`

Preferred qualifying row:
- `alert_type = ladder_missing` or `alert_type = ladder_selection_empty`

Why required:
- This is the smallest persisted proof that the same station emitted the bad-path signal and why it did so.
- `metadata.empty_reason` plus `metadata.cause` is the minimum pair that separates true cache-empty behavior from filter-collapse behavior.

### D. Bad-side normalization block
Required source:
- `GET /observability/market-eligibility-runtime?station=<ICAO>`

Required fields:
- `latest_evaluation_outcome`
- `latest_suppression_reason`
- `current_cache_probe.raw_market_count`
- `current_cache_probe.filtered_market_count`
- `current_cache_probe.post_directional_market_count`
- `current_cache_probe.empty_reason`

Why required:
- This is the smallest operator-facing normalization bundle for the bad side.
- It converts stale directional-collapse wording into the operator-facing `filtered_to_zero` interpretation and distinguishes:
  - `NO_MARKETS_CACHED / cache_empty`, from
  - `NO_ELIGIBLE_MARKET / filtered_to_zero`.

## Optional / Supporting Only
Use these only when the minimal packet leaves ambiguity.

### Optional supporting surfaces
- `GET /observability/alert-path-truth?station=<ICAO>`
  - Use when the reviewer wants the latest send-alert truth (`send_alert_entered`, `blocking_stage`, webhook attempt/success flags).
- `GET /observability/alert-decision-trace?station=<ICAO>`
  - Use when the reviewer wants the latest blocking stage or the latest decision chain.
- `GET /observability/market-coverage?station=<ICAO>`
  - Use when the reviewer wants current series/cache coverage framing in addition to the historical bad row.
- `GET /observability/internal-alert-runtime?station=<ICAO>`
  - Use when the reviewer wants the compact latest summary surface.
- Full raw JSON payload dumps
- `ladder_cache`
- `runtime-authority-snapshot`
- `alert-fire-audit`

### Why optional only
- These surfaces are valuable context, but they do not reduce the minimum proof set for the same-station question.
- The exact operator-facing question is not “what is the full current runtime state?”; it is “what is the exact minimal packet needed to compare one good and one bad event for the same station?”

## Compact Operator-Facing Packet Shape

```text
SAME_STATION_MIXED_BEHAVIOR_PACKET

capture_window_utc:
station:
good_event_created_utc:
bad_event_created_utc:
delta_minutes_between_events:

GOOD_EVENT
- source: /observability/alert-preview?station=<ICAO>&limit=10
- created_utc:
- alert_type:
- market_type:
- event_ticker:
- reason:
- direction:
- bucket_index:
- attention_phrase:
- payload_preview.temp_f:

BAD_EVENT
- source: /observability/alert-preview?station=<ICAO>&limit=10
- created_utc:
- alert_type:
- market_type:
- metadata.empty_reason:
- metadata.cause:
- metadata.explanation:
- payload_preview.temp_f:

BAD_SIDE_NORMALIZATION
- source: /observability/market-eligibility-runtime?station=<ICAO>
- latest_evaluation_outcome:
- latest_suppression_reason:
- current_cache_probe.raw_market_count:
- current_cache_probe.filtered_market_count:
- current_cache_probe.post_directional_market_count:
- current_cache_probe.empty_reason:

VERDICT
- same_station_mixed_behavior_confirmed: yes|no

ONE_LINE_RATIONALE
- <why this proves one station produced both a real ladder-backed alert and a ladder-missing / filtered-to-zero event in a short interval>
```

## What To Leave Out By Default
Do not require these in the first operator paste unless ambiguity remains:
- full payload dumps
- sibling-station comparison rows
- full decision chains
- full alert-path-truth payloads
- full market-coverage payloads
- runtime-authority snapshots
- ladder-cache details
- fire-audit tables

These are useful escalation surfaces, but they are not first-pass requirements for the same-station mixed-behavior question.

## Most Likely Current Explanation
The repository already has all of the persistence needed for a compact same-station packet:
- the good side is durably captured as a real emitted alert row,
- the bad side is durably captured as a ladder-missing or ladder-selection-empty row with explicit reason metadata,
- and the route-level normalization surface already explains when directional collapse should be interpreted as `filtered_to_zero`.

Because of that, sibling-station comparison is no longer the tightest first-pass packet when the same station already demonstrates both behaviors within a short interval.

## Single Recommended Next Action
Replace the current sibling-station comparison workflow for this issue class with the single-station two-event packet above, and escalate to supporting surfaces only if the bad row and the bad-side normalization block disagree.
