ARCHIVED — superseded; not current requirements.

# Production-Readiness Audit (Weather-Driven Alert System)

## 1) Silent failure paths

### Issue 1.1: Alert-path exceptions are swallowed without telemetry
- **Risk:** `_send_alert` catches all exceptions and suppresses them.
- **Why real:** Any failure in Kalshi snapshot fetch, transition evaluation, or webhook POST is invisible to operators.
- **Production manifestation:** Alerts stop during transient API/network faults while health endpoints still show normal scheduler activity.
- **Minimal fix:** Log exceptions (including station/source context) and increment an in-memory error counter exposed via metrics.

### Issue 1.2: Cache persistence failures are silently ignored
- **Risk:** `_save_cache` catches all exceptions and does nothing.
- **Why real:** Disk permission/path issues will drop restart continuity with no signal.
- **Production manifestation:** After restart, prior `last_seen_iso` / ladder-adjacent state is lost, producing duplicate or missed transitions.
- **Minimal fix:** Emit structured error logs and expose a `cache_write_failures` metric.

## 2) Suppressed alerts due to incorrect conditions

### Issue 2.1: Raw alert suppression can happen even when composed alert was not sent
- **Risk:** In `_send_alert`, if any ladder exists, raw METAR alert is always suppressed (`return`), regardless of whether `send_composed_weather_market_alert` succeeded.
- **Why real:** `send_composed_weather_market_alert` can return `{"ok": False, ...}` (no webhook, no markets after filter, webhook failure), but caller ignores return value.
- **Production manifestation:** Complete alert drop: no composed alert and no raw fallback.
- **Minimal fix:** Suppress raw alert only when composed alert returns success; otherwise fall back to raw webhook payload.

## 3) Ladder state corruption risks

### Issue 3.1: Ladder state is mutable shared global without synchronization
- **Risk:** `_ladder_state` / `_ladder_event_keys` are module-level dicts mutated from alert flow and HTTP routes without locking.
- **Why real:** Flask handlers and scheduler thread can run concurrently.
- **Production manifestation:** Lost updates or inconsistent transition detection (duplicate `entry` alerts or missed `bucket/final` alerts).
- **Minimal fix:** Guard reads/writes with a `threading.Lock` around ladder state mutation and retrieval.

## 4) Event rollover edge cases

### Issue 4.1: Date token uses wall-clock "now" rather than observation/event context
- **Risk:** `_station_local_kalshi_date_token` always uses current local date, and snapshot queries/filtering require that token.
- **Why real:** Late/backfilled observations near midnight are evaluated against the wrong day’s event ticker.
- **Production manifestation:** Valid ladders for the observation day are not found; alerts are skipped or routed incorrectly.
- **Minimal fix:** Accept a reference timestamp (e.g., METAR obs_time) when computing date token during transition evaluation.

## 5) API failure handling

### Issue 5.1: Kalshi/public API failures are converted to generic `{"ok": false}` in multiple routes
- **Risk:** Endpoint handlers swallow exceptions and return 200 with no reason.
- **Why real:** Operators cannot distinguish auth errors, rate limits, upstream 5xx, or parse failures.
- **Production manifestation:** Monitoring sees green HTTP 200 while Kalshi integration is effectively down.
- **Minimal fix:** Return structured error details (sanitized) and non-2xx status for upstream failures.

### Issue 5.2: Weather API timeout retry is incomplete for non-timeout transient errors
- **Risk:** `_fetch_range_nws` retries only `Timeout`, not connection resets/5xx.
- **Why real:** Common transient failures are not retried.
- **Production manifestation:** Increased missed windows and fewer ingestions during upstream instability.
- **Minimal fix:** Add bounded retry/backoff for retryable `requests` exceptions and 5xx responses.

## 6) Infinite loops or unnecessary API calls

### Issue 6.1: No infinite loop detected
- **No risk detected.**

### Issue 6.2: Duplicate Kalshi snapshot fetches per alert
- **Risk:** `_send_alert` builds snapshots per market type, then each composed alert call builds snapshot again.
- **Why real:** One transition can trigger multiple redundant Kalshi market API calls.
- **Production manifestation:** Higher latency and avoidable API quota consumption during volatile periods.
- **Minimal fix:** Reuse already-built snapshot in composed alert path or pass precomputed ladder payload.

## 7) Race conditions (shared module-level state)

### Issue 7.1: Market-state tracking globals are unsynchronized
- **Risk:** `_last_market_state`, `_last_composed_sent`, `_last_market_check_summary` are shared mutable globals.
- **Why real:** Concurrent `/kalshi/check`, `/kalshi/health`, and alert-triggered updates can interleave.
- **Production manifestation:** Inconsistent summaries, duplicate alerts, or missed diff detection due to torn updates.
- **Minimal fix:** Use a lock around read-modify-write operations on these dicts.

## 8) Memory growth risks

### Issue 8.1: Unbounded growth of per-ticker and per-event state maps
- **Risk:** `_last_market_state`, `_ladder_state`, `_ladder_event_keys`, `_last_composed_sent` never evict stale keys.
- **Why real:** New event tickers appear daily; old entries persist indefinitely.
- **Production manifestation:** Gradual memory growth, longer dict operations, eventual process pressure in long-lived workers.
- **Minimal fix:** Add TTL/LRU pruning keyed by event date or last-updated timestamp.

## 9) Incorrect assumptions about Kalshi ladder structure

### Issue 9.1: Hard dependency on ticker suffix `B<digits>` for strike extraction
- **Risk:** Markets are discarded unless ticker matches regex `B(\d+)$`.
- **Why real:** Strike should be derived from structured fields (`floor_strike`/`cap_strike`) when available.
- **Production manifestation:** Valid markets omitted from ladder if ticker naming differs, causing false "no ladder" conditions.
- **Minimal fix:** Prefer structured strike fields; use ticker parsing only as fallback.

### Issue 9.2: Final-rung detection assumes presence of `greater` (HIGH) / `less` (LOW) contracts
- **Risk:** `_determine_bucket` only marks final hit when these rung types exist.
- **Why real:** Some ladders may terminate with between buckets only.
- **Production manifestation:** Expected "final" transition alerts never fire on top/bottom extremes.
- **Minimal fix:** When no terminal rung exists, treat movement beyond outermost between bucket as final boundary condition.
