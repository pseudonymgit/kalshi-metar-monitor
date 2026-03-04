# Deterministic Alert-Path Diagnosis — KNYC / KPHL (2026-03-02)

## Deterministic execution proof (local code-path + runtime)

Commands executed:

- `python - <<'PY' ... ensure_ladder_hydration_prerequisite('KNYC'/'KPHL') ... PY`
- `python - <<'PY' ... metar_monitor._poll_once() ... PY`

Observed deterministic runtime result in this environment:

- `requests.exceptions.ProxyError`
- upstream detail: `Tunnel connection failed: 403 Forbidden`
- failing network call path: Kalshi series discovery (`/trade-api/v2/series?tags=Daily%20temperature`)

Because `_poll_once` calls `ensure_ladder_hydration_prerequisite` before any fetch/ingest work, this exception prevents reaching Stage 1 acceptance for both stations.

---

## Acceptance decision path from `core/metar_monitor.py`

1. Scheduler entry: `_poll_once`.
2. Per-station gate: `ensure_ladder_hydration_prerequisite(icao)`.
3. If hydration status is not `cache_valid`, station is skipped (`continue`) and no fetch/ingest occurs.
4. Only hydrated stations execute `_fetch_range_strict(...)` and `_ingest_obs(...)`.
5. Inside `_ingest_obs`, acceptance guards are applied in this order:
   - outside end window (`obs_dt > window_end`) -> skip
   - too old for grace (`obs_dt < window_start - METAR_ACCEPTANCE_GRACE_SECONDS`) -> skip
   - local-day mismatch with `window_end` day -> skip
   - duplicate/non-monotonic ordering (`obs_dt <= last_seen_iso`) -> skip
   - otherwise accepted via `set_latest_observation(...)`

For this incident, execution does not reach step 5.

---

## Exact first failing gate (KNYC, KPHL)

**First failing gate:** hydration prerequisite call during `_poll_once`.

- Deterministic branch equivalent: **PRE_INGESTION_HYDRATION_PREREQUISITE_UNAVAILABLE**.
- Concrete failure in this runtime: series discovery request raises `ProxyError` before a hydration status object is returned.

Result: no observation enters accepted ingestion state, even while scheduler loop activity may continue (loop catches exceptions and retries).

---

## Required analysis results

### 1) Observation fetch success path

Fetch success path exists only after hydration gate passes:

`_poll_once` -> `_compute_window` -> `_fetch_range_strict` -> `_ingest_obs`.

### 2) Acceptance guards

In `_ingest_obs`, acceptance requires all guards to pass (time window + local trading-day alignment + strict monotonic timestamp).

### 3) Duplicate / ordering rejection logic

Duplicate/out-of-order observations are rejected by:

`if last_seen_iso and obs_dt <= _parse_iso(last_seen_iso): continue`

### 4) Trading-day initialization requirements

Trading-day readiness is enforced *before* ingestion by ladder hydration:

- station must resolve to a Kalshi series ticker
- cached market snapshot must exist
- cache day must equal station local day (or previous-day rollover grace)

If these invariants are not met, poll path returns/acts as non-hydrated and ingestion is bypassed.

### 5) Persistence dependencies

Accepted-ingestion state (`last_obs`, `last_seen_iso`) is authoritative in-memory state with cache-file persistence. Transition/alert DB writes are best-effort side effects and do not gate acceptance in `_ingest_obs`.

### 6) Behavior when `ALERT_DB_PATH` does not exist

Missing DB does **not** block `_ingest_obs` acceptance. DB-dependent readers return empty data or no-op; replay returns zero processed rows when DB is absent.

### 7) Behavior when prior accepted observation is absent

If `last_seen_iso` is absent, duplicate guard is inactive; first eligible observation can be accepted, provided window/trading-day guards pass and execution reaches `_ingest_obs`.

---

## Deterministic conclusion

- **Rejection condition observed:** pre-ingestion hydration prerequisite unavailable due deterministic network failure in Kalshi series discovery.
- **First failing guard:** hydration prerequisite call in `_poll_once` (before fetch/ingest).
- **Missing invariant:** valid hydrated ladder cache for station-day (and, in this runtime, ability to discover series/cache path without proxy failure).
- **Minimal deterministic correction:** make hydration prerequisite resolvable before polling stations (pre-populate/refresh station-day cache and ensure series discovery call path is reachable), so `_poll_once` can enter fetch+`_ingest_obs` acceptance path.
