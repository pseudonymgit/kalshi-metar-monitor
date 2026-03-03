# RECONCILIATION SNAPSHOT — KDEN

## Scope
- Task type: production runtime reconciliation diagnostic.
- Target station: `KDEN` (defaulted because no newer mismatch station authority was available in this environment).

## Runtime authority check (this container)

Observed at `2026-03-03T14:26:59Z`:

```bash
test -e /var/data/alerts.db && echo PRESENT || echo MISSING
ls -ld /var/data /var/data/alerts.db
sqlite3 /var/data/alerts.db ".tables"
curl -sS -m 5 http://127.0.0.1:8000/observability/runtime-authority-snapshot?station=KDEN
```

Observed outputs:

- `MISSING`
- `ls: cannot access '/var/data': No such file or directory`
- `ls: cannot access '/var/data/alerts.db': No such file or directory`
- `Error: unable to open database "/var/data/alerts.db": unable to open database file`
- `curl: (7) Failed to connect to 127.0.0.1 port 8000 after 0 ms: Couldn't connect to server`

Deterministic meaning:
- This environment is not the Render production runtime and has no authoritative `/var/data` mount.
- A synchronized transition + market snapshot cannot be extracted here.

---

## Required synchronized snapshot fields

### A) Transition context
| field | value |
|---|---|
| station | `KDEN` |
| transition_type | `UNAVAILABLE_IN_THIS_ENVIRONMENT` |
| decision_type | `UNAVAILABLE_IN_THIS_ENVIRONMENT` |
| pre_state_bucket | `UNAVAILABLE_IN_THIS_ENVIRONMENT` |
| post_state_bucket | `UNAVAILABLE_IN_THIS_ENVIRONMENT` |
| observation_timestamp | `UNAVAILABLE_IN_THIS_ENVIRONMENT` |
| alert_emit_timestamp | `UNAVAILABLE_IN_THIS_ENVIRONMENT` |

### B) Raw market set for station active series
No production market snapshot was reachable from this environment.

| ticker | extracted_strike | market_type | status | best_bid | best_ask | last_price | snapshot_timestamp |
|---|---:|---|---|---:|---:|---:|---|
| `UNAVAILABLE_IN_THIS_ENVIRONMENT` | `UNAVAILABLE_IN_THIS_ENVIRONMENT` | `UNAVAILABLE_IN_THIS_ENVIRONMENT` | `UNAVAILABLE_IN_THIS_ENVIRONMENT` | `UNAVAILABLE_IN_THIS_ENVIRONMENT` | `UNAVAILABLE_IN_THIS_ENVIRONMENT` | `UNAVAILABLE_IN_THIS_ENVIRONMENT` | `UNAVAILABLE_IN_THIS_ENVIRONMENT` |

### C) Eligibility evaluation results per market
| ticker | eligible | rejection_reason |
|---|---|---|
| `UNAVAILABLE_IN_THIS_ENVIRONMENT` | `UNAVAILABLE_IN_THIS_ENVIRONMENT` | `UNAVAILABLE_IN_THIS_ENVIRONMENT` |

### D) Selected market used in alert
| field | value |
|---|---|
| ticker | `UNAVAILABLE_IN_THIS_ENVIRONMENT` |
| strike | `UNAVAILABLE_IN_THIS_ENVIRONMENT` |
| price_used | `UNAVAILABLE_IN_THIS_ENVIRONMENT` |
| price_timestamp | `UNAVAILABLE_IN_THIS_ENVIRONMENT` |

### E) Hydration metadata
| field | value |
|---|---|
| cache_status | `UNAVAILABLE_IN_THIS_ENVIRONMENT` |
| cache_last_updated | `UNAVAILABLE_IN_THIS_ENVIRONMENT` |
| cache_age_seconds | `UNAVAILABLE_IN_THIS_ENVIRONMENT` |

### F) Timing comparison
| field | value |
|---|---|
| snapshot_fetch_timestamp | `UNAVAILABLE_IN_THIS_ENVIRONMENT` |
| alert_emit_timestamp | `UNAVAILABLE_IN_THIS_ENVIRONMENT` |
| delta_seconds | `UNAVAILABLE_IN_THIS_ENVIRONMENT` |

---

## Timeline (observed truth only)
1. `2026-03-03T14:26:59Z` — local check confirmed `/var/data/alerts.db` missing.
2. Immediate follow-up — SQLite open of `/var/data/alerts.db` failed (`unable to open database file`).
3. Immediate follow-up — local observability endpoint was unreachable at `127.0.0.1:8000`.
4. Therefore: production synchronized snapshot not observable from this runtime.

---

## Root cause category determination
**Category:** `other deterministic cause`.

**Deterministic cause identified:** runtime authority boundary / connectivity gap.
- Not enough production-authoritative evidence is available in this environment to classify as stale hydration cache, wrong rung mapping, eligibility exclusion, or snapshot race.

---

## Required explicit statements
- **Alert price derived from [UNAVAILABLE_IN_THIS_ENVIRONMENT].**
- **Live board price derived from [UNAVAILABLE_IN_THIS_ENVIRONMENT].**

---

## Render production execution instructions (required to complete reconciliation)
Run these from a network location that can reach Render and has access to the production app URL:

```bash
BASE_URL="https://kalshi-metar-monitor.onrender.com"
STATION="KDEN"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_DIR="/tmp/reconcile_${STATION}_${TS}"
mkdir -p "$OUT_DIR"

curl -sS "$BASE_URL/observability/transitions?station=$STATION&limit=20" \
  -o "$OUT_DIR/transitions.json"

curl -sS "$BASE_URL/observability/internal-alert-runtime?station=$STATION" \
  -o "$OUT_DIR/internal_alert_runtime.json"

curl -sS "$BASE_URL/observability/market-eligibility-runtime?station=$STATION" \
  -o "$OUT_DIR/market_eligibility_runtime.json"

curl -sS "$BASE_URL/observability/alert-decision-trace?station=$STATION" \
  -o "$OUT_DIR/alert_decision_trace.json"

curl -sS "$BASE_URL/observability/runtime-authority-snapshot?station=$STATION" \
  -o "$OUT_DIR/runtime_authority_snapshot.json"

printf "Captured files in %s\n" "$OUT_DIR"
```

If the mismatch is currently visible on the Kalshi board, execute these calls immediately (same minute) to minimize timing skew, then populate sections A–F with the captured JSON values.
