# Settlement provenance boundary (Q-PROV-001)

## Inventory

- `data/kalshi_settlements.json`: cached Kalshi outcome artifacts (`event_ticker`, `target_date`, `kalshi_temp`).
- `data/phase0_settlement_data.json`: station/date comparison cache containing Kalshi and NWS/METAR-derived values; mixed artifact, so fields must not be treated as interchangeable.
- `data/phaseB_settlement_validation.json`: validation report, not a source of outcomes.
- `core/settlement_epoch_logger.py` and `core/db_schema.py`: `settlement_epochs` schema (`station`, `local_trading_date`, `settlement_bucket`, timestamps, epoch lifecycle/transition metrics).
- METAR schema artifacts: `data/backfill_metar.py` and `scripts/metar_collect_live.py` define `metar_observations`; derived daily/epoch values are observations.
- GHCN use: `core/signals/ghcn_calibration_signal.py` and `scripts/intraday_runner.py` identify GHCN calibration/proxy inputs; no GHCN value is authoritative settlement.

## Adapter boundary

`core/settlement_provenance.py` exposes three explicit adapters:

1. **Kalshi JSON → `KALSHI_AUTHORITATIVE`**: uses `expiration_value` (or existing cache `kalshi_temp`) and preserves the raw payload. This is the only adapter whose record can be an authoritative market outcome.
2. **METAR settlement epoch → `OBSERVED_EPOCH`**: uses `settlement_bucket` and epoch identity/timestamps. It represents a locally derived observation and cannot be promoted to a Kalshi outcome.
3. **GHCN daily value → `PROXY`**: uses a GHCN station/date daily value for calibration/comparison only. It is explicitly not settlement.

All records retain `source`, `role`, `source_id`, date, station, market type, value, and the original payload. Missing required identity/value fields fail closed. No adapter writes a database, calls an API, relabels source data, or synthesizes outcomes.

## Acceptance test

Run the focused test:

```text
python3 -m pytest -q tests/test_settlement_provenance.py
```

Next acceptance test: feed one representative artifact from each source (including a Kalshi payload with `expiration_value`, an actual `settlement_epochs` row, and a GHCN daily record), assert exact `source`/`role`/`source_id` preservation, and assert downstream settlement scoring accepts only `KALSHI_AUTHORITATIVE` records.
