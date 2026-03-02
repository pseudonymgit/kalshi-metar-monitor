# Kalshi Hydration Proxy Failure Diagnosis (2026-03-02)

## Scope
Diagnose why `ensure_ladder_hydration_prerequisite()` fails during Kalshi series discovery with:

- `requests.exceptions.ProxyError`
- `Tunnel connection failed: 403 Forbidden`

## 1) Exact outbound execution path

From `core/kalshi_monitor.py`:

1. `ensure_ladder_hydration_prerequisite(station)` calls `ensure_series_discovery_loaded()`.
2. `ensure_series_discovery_loaded()` calls `_discover_series_for_stations()` on first load.
3. `_discover_series_for_stations()` performs `_kalshi_public_get("/series?tags=Daily%20temperature")`.
4. `_kalshi_public_get(path)` constructs URL:
   - base URL = `KALSHI_PUBLIC_BASE_URL` env var, else default `https://api.elections.kalshi.com/trade-api/v2`
   - normalized path = `/series?tags=Daily%20temperature`
   - final request URL (default):
     `https://api.elections.kalshi.com/trade-api/v2/series?tags=Daily%20temperature`
5. `_kalshi_public_get` uses `requests.get(..., timeout=10)` with no explicit proxy/session override.

## 2) HTTP client configuration used by failing path

- Client: top-level `requests.get`.
- No `proxies=` argument.
- No custom `Session` object.
- No `trust_env=False` override.
- Therefore: `requests` inherits proxy-related environment variables by default.

## 3) Proxy/env variables observed in analysis container

Observed runtime environment:

- `HTTP_PROXY=http://proxy:8080`
- `HTTPS_PROXY=http://proxy:8080`
- `http_proxy=http://proxy:8080`
- `https_proxy=http://proxy:8080`
- `NO_PROXY=browser`
- `no_proxy=localhost,127.0.0.1,::1`

`api.elections.kalshi.com` is **not** present in `NO_PROXY` / `no_proxy`.

## 4) Requests inheritance confirmation

Runtime check confirms inheritance:

- `requests.Session().trust_env == True`
- `merge_environment_settings(...)["proxies"]` resolves `https`/`http` to `http://proxy:8080`

So the Kalshi HTTPS call is sent as an HTTP `CONNECT` tunnel request to proxy authority `proxy:8080`.

## 5) Deterministic failing call + failing authority

Deterministically failing outbound call:

- `GET https://api.elections.kalshi.com/trade-api/v2/series?tags=Daily%20temperature`

Failing network authority:

- upstream proxy at `proxy:8080` (not Kalshi origin)

Observed failure:

- `OSError: Tunnel connection failed: 403 Forbidden`
- raised as `urllib3.exceptions.ProxyError`
- surfaced as `requests.exceptions.ProxyError`

This failure occurs before TLS session establishment with `api.elections.kalshi.com`; origin request is never completed.

## 6) TLS interception / block determination

- Because `CONNECT` is denied with HTTP 403 by proxy, tunnel establishment is blocked upstream.
- This is a proxy policy/access denial on CONNECT, not an application-layer Kalshi API response.
- Kalshi request headers (including any authentication headers) are not causal for this failure mode.

## 7) Local vs Render vs analysis container

### Analysis container (this environment)
- Forced proxy env is present (`proxy:8080`).
- Direct no-proxy HTTPS egress attempt (`trust_env=False`) fails with network unreachable.
- Therefore this container depends on proxy for egress, but proxy denies HTTPS CONNECT.

### Local execution (developer workstation)
- Not directly measured here.
- Outcome depends on local proxy config; if no restrictive proxy is injected, call should reach Kalshi normally.

### Render production execution
- Not directly measured here.
- Repository docs specify Render runtime (`gunicorn app:app -t 180`) but do not require outbound proxy.
- If Render service inherits `HTTP_PROXY/HTTPS_PROXY` pointing to a denying proxy, it will reproduce the same failure.
- If Render runs without those proxy vars (or with correct bypass/routing), series discovery should proceed.

## 8) Required determinations (explicit)

- Proxy configuration exists: **Yes** (`HTTP_PROXY/HTTPS_PROXY` set to `http://proxy:8080`).
- Outbound HTTPS blocked: **Yes, at proxy CONNECT stage (403)**.
- Missing authentication headers cause: **No evidence; failure occurs before origin request completion**.
- Requests session differs from working endpoints: **No special session; default env-inheriting requests path**.

## 9) Minimal deterministic correction (environment/config only)

Apply environment correction in the runtime that executes ingestion:

1. Ensure Kalshi destination bypasses denying proxy, e.g. add host to no-proxy list:
   - `NO_PROXY=api.elections.kalshi.com,...`
   - `no_proxy=api.elections.kalshi.com,...`

or

2. Remove/override injected `HTTP_PROXY` and `HTTPS_PROXY` for the monitor process if direct egress is allowed in that runtime.

The key deterministic fix is: **do not route Kalshi HTTPS (`api.elections.kalshi.com:443`) through proxy authority `proxy:8080` that denies CONNECT with 403**.
