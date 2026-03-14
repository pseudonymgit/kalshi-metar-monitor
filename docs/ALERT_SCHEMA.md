# Alert Schema v2

## Alert lifecycle

1. A deterministic METAR transition is emitted by `core/metar_monitor.py`.
2. `_emit_alert` constructs a schema-versioned alert payload (`schema_version: "2"`).
3. `_send_alert` evaluates market eligibility and updates suppression + hydration fields.
4. Eligible transitions are forwarded to Kalshi-composed alert delivery (`send_composed_weather_market_alert`).
5. Replay-domain execution does not deliver live alerts and preserves deterministic transition history.


## Canonical causal chain

observation -> transition detection -> transition persistence -> alert payload assembly -> market evaluation -> suppression/emission classification -> webhook delivery

## Schema fields

```json
{
  "schema_version": "2",
  "timestamp_utc": "...",
  "station": "...",
  "classification": "STRUCTURAL | MARKET_ELIGIBLE | MARKET_SUPPRESSED | HYDRATION_BLOCKED",
  "summary": {
    "headline": "...",
    "transition": "...",
    "temp_f": 0,
    "instant_bucket": 0,
    "settlement_bucket": 0
  },
  "transition_context": {
    "transition_type": "...",
    "instant_before": 0,
    "instant_after": 0,
    "settlement_bucket": 0,
    "running_max": 0,
    "obs_time": "..."
  },
  "market_context": {
    "series_ticker": "...",
    "event_ticker": "...",
    "market_type": "...",
    "strike": 0,
    "proximity_regime": "...",
    "hydrated": true
  },
  "eligibility_evaluation": {
    "markets_considered": 0,
    "eligible_markets": 0,
    "rejected_markets": 0,
    "rejection_breakdown": {}
  },
  "suppression": {
    "suppressed": false,
    "reason": "...",
    "reason_category": "NO_TRANSITION | NO_ELIGIBLE_MARKET | SETTLEMENT_MISMATCH | EXPIRED_MARKET | HYDRATION_BLOCK"
  },
  "execution_context": {
    "execution_domain": "...",
    "hydration_state": {},
    "scheduler_poll_count": 0
  }
}
```

## Classifications

- `STRUCTURAL`: deterministic transition detected before market-level eligibility fires.
- `MARKET_ELIGIBLE`: at least one market alert is emitted.
- `MARKET_SUPPRESSED`: transition evaluated but suppressed by market checks.
- `HYDRATION_BLOCKED`: market hydration state prevented eligibility evaluation.

> Runtime also emits classification `SIGNAL` for signal-layer alerts via the signal alert emission path.
> Signal alerts use a different payload structure and are not part of the transition alert schema contract.

## Suppression categories

- `NO_TRANSITION`: no ladder transition qualified for market alert.
- `NO_ELIGIBLE_MARKET`: hydration/evaluation found no eligible ladder market.
- `SETTLEMENT_MISMATCH`: transition suppressed due to terminal or settlement mismatch state.
- `EXPIRED_MARKET`: documented as a suppression category but is not currently emitted as `suppression.reason_category` by runtime logic. Expired markets are instead tracked in `eligibility_evaluation.rejection_breakdown.expired_market`.
- `HYDRATION_BLOCK`: hydration prerequisites blocked market evaluation.

## Replay delivery clarification

- Replay execution reconstructs transition/state history deterministically and does not deliver live webhook alerts.

## Operator troubleshooting

- Check `classification` first to identify structural vs market suppression paths.
- Use `transition_context` to verify causality (`instant_before` -> `instant_after`, `settlement_bucket`).
- Inspect `eligibility_evaluation.rejection_breakdown` to identify reject drivers.
- Review `suppression.reason_category` before escalating market-data incidents.
- For hydration incidents, inspect `execution_context.hydration_state` and poll telemetry.
