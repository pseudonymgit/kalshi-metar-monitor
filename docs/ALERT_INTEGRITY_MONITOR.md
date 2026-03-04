# Alert Integrity Monitor

## Purpose

The Alert Integrity Monitor is an observability-only subsystem that checks for structural gaps across transition history, alert audit state, hydration prerequisites, and station market discovery. It surfaces findings to help operators detect silent alert pipeline failures.

The monitor **does not** mutate runtime state and **does not** influence alert delivery decisions.

Endpoint:

- `GET /integrity/alert_pipeline`

## Finding Types

The monitor emits findings with fields:

- `station`
- `timestamp`
- `finding_type`
- `supporting_metrics`

Supported `finding_type` values:

- `ALERT_PIPELINE_GAP`
- `TRANSITION_WITHOUT_EVALUATION`
- `SUPPRESSION_WITHOUT_REASON`
- `HYDRATION_DRIFT`
- `MARKET_DISCOVERY_REGRESSION`
- `STATION_ALERT_SILENCE`

## Integrity Checks

### 1) Transition coverage

If a transition occurred but no market evaluation was recorded in the configured evaluation window, the monitor emits:

- `ALERT_PIPELINE_GAP`
- `TRANSITION_WITHOUT_EVALUATION`

### 2) Suppression reasoning

If the latest evaluation outcome is suppression-class but no suppression reason is present, the monitor emits:

- `SUPPRESSION_WITHOUT_REASON`

### 3) Hydration drift

If ladder cache state exists while hydration prerequisites report `series_discovered = false`, the monitor emits:

- `HYDRATION_DRIFT`

### 4) Market discovery regression

If a station continues producing transitions but is no longer present in the discovered station universe, the monitor emits:

- `MARKET_DISCOVERY_REGRESSION`

### 5) Station alert silence

If transitions are occurring for a station and no recent alerts exist in the silence window, the monitor emits:

- `STATION_ALERT_SILENCE`

## Operator Response Guidance

When findings are present:

1. **Check transition recency and evaluation lag**
   - Inspect transition timestamps and latest evaluation timestamps.
2. **Validate suppression metadata completeness**
   - Ensure suppression outcomes include stable reason tokens.
3. **Inspect hydration prerequisites**
   - Confirm series discovery and ladder cache validity for affected stations.
4. **Verify market universe continuity**
   - Confirm previously active stations remain in discovered market coverage.
5. **Escalate sustained silence conditions**
   - If transitions continue without alerts across multiple windows, treat as a potential pipeline integrity incident.

## Configuration

Optional environment variables:

- `ALERT_INTEGRITY_EVALUATION_WINDOW_SECONDS` (default: `300`)
- `ALERT_INTEGRITY_SILENCE_WINDOW_SECONDS` (default: `3600`)
