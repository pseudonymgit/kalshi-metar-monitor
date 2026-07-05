# Alert Schema v1.0 — Frozen Specification

**Status:** FROZEN (2026-07-05)  
**Version:** 1.0  
**Schema Version String:** `"1.0"` (replaces legacy `"2"` integer)  
**Supersedes:** `ALERT_SCHEMA_VERSION = "2"` (retained as alias for backward compat)

---

## 1. Schema Versioning Policy

### Rule
- Alert schema is **frozen at v1.0**. No breaking changes to field names or semantics.
- Additive changes (new optional fields) are permitted without version bump.
- Breaking changes require a new major version (v2.0) and a migration plan.
- Every alert payload MUST include `schema_version: "1.0"`.
- The legacy integer `2` is accepted on ingress for backward compatibility; it is normalized to `"1.0"` internally.

### Migration
- `core/alert_schema.py` exports `ALERT_SCHEMA_VERSION = "1.0"` and `LEGACY_SCHEMA_VERSION = "2"`.
- All new code uses `"1.0"`.
- Ingest code that sees `"2"` treats it as `"1.0"`.

---

## 2. Canonical Event Log

### Storage
- **Table:** `transition_events` (SQLite, in `alerts-prod.db` or per-instance DB)
- **Structure:** `id`, `created_utc`, `station`, `transition_type`, `instant_bucket_before`, `instant_bucket_after`, `settlement_bucket`, `running_max`, `current_temp`, `metadata_json`
- `metadata_json` contains: `alert_schema_version`, `alert_classification`, `signal_context`, `suppression_reason`, `market_eligibility_runtime`

### Canonical Fields (v1.0)
Every event in the log MUST have:
| Field | Type | Description |
|-------|------|-------------|
| `created_utc` | TEXT (ISO 8601) | UTC timestamp of event |
| `station` | TEXT (ICAO) | Uppercase ICAO code |
| `transition_type` | TEXT | One of: `instant_up`, `instant_down`, `settlement_up`, `reversion_after_settlement`, `goldilocks_reversion` |
| `instant_bucket_before` | INTEGER | Floor temp before transition |
| `instant_bucket_after` | INTEGER | Floor temp after transition |
| `settlement_bucket` | INTEGER | Settlement bucket at time of event |
| `running_max` | REAL | Running daily max temp |
| `current_temp` | REAL | Temperature at event |
| `metadata_json` | TEXT (JSON) | Schema-versioned metadata blob |

### Event Log Integrity
- Events are append-only.
- `id` is auto-increment; no UUID collisions possible.
- `_AUDIT_LOCK` serializes writes.
- `transition_event_id` is propagated through correlation to link alerts to events.

---

## 3. Alert Payload Structure (v1.0)

### Structural Alerts (transitions)
```json
{
  "schema_version": "1.0",
  "timestamp_utc": "2026-07-05T12:00:00Z",
  "station": "KDEN",
  "classification": "STRUCTURAL",
  "summary": {
    "headline": "KDEN transition detected",
    "transition": "settlement_up",
    "temp_f": 87.3,
    "instant_bucket": 87,
    "settlement_bucket": 87
  },
  "transition_context": { ... },
  "market_context": { ... },
  "eligibility_evaluation": { ... },
  "suppression": { ... },
  "execution_context": { ... }
}
```

### Signal Alerts
```json
{
  "schema_version": "1.0",
  "timestamp_utc": "2026-07-05T12:00:00Z",
  "station": "KDEN",
  "classification": "SIGNAL",
  "summary": {
    "headline": "KDEN signal alert",
    "transition": "near_boundary_momentum_up",
    "temp_f": 87.3,
    "instant_bucket": 87,
    "settlement_bucket": 86
  },
  "signal_context": { ... }
}
```

---

## 4. Alert Type Catalog (v1.0)

### Category: transition
| Type | Direction | Description |
|------|-----------|-------------|
| `instant_up` | UP | Temperature crossed integer boundary upward |
| `instant_down` | DOWN | Temperature crossed integer boundary downward |
| `settlement_up` | UP | Settlement bucket increased |
| `reversion_after_settlement` | REVERSAL | Temperature reverted after settlement |
| `goldilocks_reversion` | REVERSAL | Goldilocks reversion pattern detected |

### Category: signal
| Type | Direction | Description |
|------|-----------|-------------|
| `near_boundary_momentum_up` | UP | Near-boundary upward momentum |
| `near_boundary_momentum_down` | DOWN | Near-boundary downward momentum |
| `goldilocks_momentum_down` | DOWN | Goldilocks downward momentum |

### Category: ladder_missing
| Type | Direction | Description |
|------|-----------|-------------|
| `missing_ladder` | N/A | No ladder markets cached |
| `missing_directional_ladder` | N/A | No directional ladder match |

---

## 5. Distribution Topology

### Decision: Per-City Distribution (Unified Ingestion)

**Ingestion:** Unified — single poller collects all stations.
**Distribution:** Per-city — alerts are routed to per-city channels.

### Rationale
- Kalshi markets are per-city; alert relevance is city-specific.
- Per-city distribution allows downstream consumers to subscribe to specific cities.
- Unified ingestion minimizes API calls to NWS/IEM.
- A single `ALERT_WEBHOOK_URL` can fan out to per-city Discord channels via a router.

### Implementation
- Alert payload includes `station` field for routing.
- A `DISCORD_CHANNEL_MAP` env var maps station → Discord channel ID.
- If no map is configured, all alerts go to the default webhook URL.
- The paper trading runner (P0.9) uses per-instance webhooks, not per-city.

### Channel Routing
```
Station → Channel mapping (configurable via env):
  KDEN → #weather-denver
  KLAX → #weather-la
  KNYC → #weather-nyc
  ...
Default: #weather-alerts
```

---

## 6. Outcome Classification Taxonomy

Every signal evaluation results in one of:

| Outcome | Description | Alert Fired? |
|---------|-------------|--------------|
| `ALERT_SENT` | Signal detected, market eligible, alert delivered | ✅ |
| `ELIGIBLE_NOT_ALERTABLE` | Signal detected, market exists, but suppressed (cooldown, already emitted, etc.) | ❌ |
| `NO_ELIGIBLE_MARKET` | Signal detected, but no market exists for station | ❌ |
| `HYDRATION_BLOCKED` | Signal detected, hydration cache invalid | ❌ |
| `NO_SIGNAL_CONDITION_MATCH` | No signal condition met | ❌ |

---

## 7. Discord Alert Format (Additive Extension — 2026-07-05)

This is an additive extension to Schema v1.0. No breaking changes to the core schema.

### Discord Message Format
Every Discord alert follows this format:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[DEV] 📍 Station: KDEN
📊 Market: HIGH
📈 Direction: UP
💰 Size: $46.25
🌡️ Current bucket: 87
🎯 Trading bucket: 88
📉 Market odds: 0.55
✅ Trade Conf: HIGH (0.85)
🔝 Top signals: near_boundary_momentum_up (conf=0.85), late_day_momentum_hourly (conf=0.70)
📊 Sharpe: 1.2 | Coverage: 12/20 stations
💵 Running P&L: +$127.50
🔗 Market: https://kalshi.com/markets/KXHIGHDEN-26JUL05
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### Fields
| Field | Description |
|-------|-------------|
| Instance tag | `[PROD]`, `[DEV]`, or `[SBOX]` — prepended to station line |
| `🔗 Market` | Direct clickable link to the Kalshi market page |
| `━━━` delimiters | Visual separation between alerts in high-volume channels |
| `username` | `Weather Engine [DEV]` / `Weather Engine [SBOX]` / `Weather Engine [PROD]` |

### Market URL Construction
- Primary: `https://kalshi.com/markets/{event_ticker}` (from Kalshi API response)
- Fallback: `https://kalshi.com/markets/{series_ticker}` (e.g., `KXHIGHDEN`)
- The URL is built by `core/kalshi_price_fetcher.py:build_market_url()` — a pure deterministic function

### Payload Extension
The alert JSON payload includes an additional `market_url` field:
```json
{
  "content": "...",
  "username": "Weather Engine [DEV]",
  "embeds": [],
  "market_url": "https://kalshi.com/markets/KXHIGHDEN-26JUL05"
}
```

---

**Frozen by:** Gilfoyle (P0 batch, 2026-07-05)  
**Approved by:** Dan (via Donna dispatch)
