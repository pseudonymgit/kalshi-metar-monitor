# Calibration producer/consumer contract (Q-CAL-003)

**Decision:** The current directional-bin calibration artifact (`data/calibration_curves.json`)
remains **KEEP-DISABLED** for `HourlyCalibration`.

`HourlyCalibration` consumes a different contract: numeric
`hour_before_settlement` keys whose cells contain Platt parameters (`alpha`,
`beta`, and sample count `n`). The existing artifact contains confidence bins
under explicit `up`/`down` directions. It is therefore not an hourly Platt
artifact and must not be enabled, migrated in place, or treated as a compatible
fallback. The consumer skips it and uses its existing global fallback.

## Future migration/adapter gate

Any future producer migration or adapter must be preregistered with all of the
following before implementation or enablement:

- **market type** (the exact market/contract family and settlement meaning),
- **direction** (for example `up` or `down`, with the meaning defined), and
- **hour semantics** (what the hour value is measured from, its timezone, and
  its bucket boundaries).

The adapter must preserve these fields end-to-end. It must reject or quarantine
records whose fields are absent or ambiguous; it must not infer them from key
names, discard them while reshaping bins into hourly cells, or silently map a
directional-bin result to an hourly Platt cell.

A migration may only proceed after a preregistered contract fixture and
producer/consumer test demonstrate that market type, direction, and hour
semantics survive the transformation without inference or loss. This decision
adds no runtime enablement, artifact migration, database write, or deployment
change.

## Q-CAL-020 producer-side decision

For any **new hourly-Platt producer**, the recommended contract is an explicit top-level
wrapper with `version: 2` and `type: "hourly_platt"` (with the station/hour cell map
under `curves`). These fields identify the producer's intended schema; a producer must
not emit a new hourly-Platt artifact without them.

Consumer compatibility with unwrapped or legacy hourly-Platt shapes remains an explicit
consumer choice: a consumer may retain optional compatibility, or may operate in strict
mode and require the wrapper. In either mode, wrapper metadata is validated strictly
when present, and the directional-bin artifact above remains **KEEP-DISABLED**. Q-CAL-020
therefore records a producer contract recommendation only; it does not change producer
output, consumer behavior, or enable/migrate any directional artifact.

## Q-CAL-021 preregistered synthetic producer fixture

The preregistered fixture at
`tests/fixtures/q-cal-021-hourly-platt-producer.json` is the review fixture for the
recommended wrapper. It uses `version: 2`, `type: "hourly_platt"`, and `curves`, with
explicit top-level `market_type` and `direction` plus explicit `hour_semantics`:
`measured_from: "settlement_time"`, timezone `America/New_York`, and the `["06:00",
"07:00"]` local bucket. Its `curves.KATL` cells use canonical numeric hour keys (`"6"`
and `"7"`) and complete `alpha`/`beta`/`n` Platt parameters.

The Q-CAL-021 gate is read-only: tests load this synthetic JSON, validate the wrapper and
cell shape, and assert that market type, direction, and every hour-semantic field are
preserved as explicit values. The fixture is not copied into `data/`, consumed by runtime,
migrated, or enabled. The directional-bin artifact remains **KEEP-DISABLED**.

## Q-CAL-022 independent rejection review

The Q-CAL-021 fixture was independently reviewed at the semantic boundary. The rejection
drill must reject records with missing or ambiguous `market_type`, `direction`,
`hour_semantics.measured_from`, `hour_semantics.timezone`, or
`hour_semantics.bucket_boundaries`; it must not infer aliases, machine-local timezone
meaning, or bucket boundaries. The drill also verifies that the fixture path is distinct
from `data/calibration_curves.json` and that review execution performs no writes. This is
still a fixture/docs/tests-only gate: it does not enable, migrate, or write any runtime
artifact.
