# z8 producer price contract (Q-B2-PROD-PRICE)

## Finding

The z8 producer does **not** currently expose a complete authoritative unit/provenance
contract for the persisted `market_snapshots.yes_bid`, `yes_ask`, and `mid_price`
columns.

The producer prefers Kalshi's explicitly named dollar fields, but falls back to the
unsuffixed fields without recording which source was used:

- `scripts/z8_kalshi_market_cron.py:190-196` selects
  `yes_bid_dollars`/`yes_ask_dollars`, falling back to `yes_bid`/`yes_ask`, casts
  both to `float`, and computes `mid_price` as their arithmetic mean.
- `scripts/z8_kalshi_market_cron.py:140-156` declares all three destination columns
  as SQLite `REAL` with no unit, source-field, API-version, or provenance column.
- `scripts/z8_kalshi_market_cron.py:211-218` persists those values without a unit
  or source marker.

Therefore the authoritative evidence supports only this conditional statement:
when the suffixed fields are present, the producer intends to persist dollar-valued
prices; when it falls back to an unsuffixed field, the persisted row has no
machine-readable proof of whether that value was cents or dollars. `mid_price`
has the same unresolved unit as the selected bid/ask pair and is producer-derived,
not an independently sourced API field.

This is a provenance gap, not a basis for magnitude-based inference or conversion.
No existing rows may be rewritten by this contract note.

## Corroborating consumer evidence (not producer authority)

`core/kalshi_price_fetcher.py:203-210` documents Kalshi's paired API naming as
`yes_bid`/`yes_ask` in cents and `yes_bid_dollars`/`yes_ask_dollars` in dollars.
Its normalization logic at `:227-240` and `:244-265` still uses magnitude checks,
so it cannot establish provenance for already-persisted z8 rows.

`b2_input_schema_gate.py:4-7,19-26` correctly requires a caller-declared unit and
rejects `auto`; `tests/test_b2_input_schema_gate.py:12-16` verifies that behavior.
That gate is a consumer-side guard and does not repair the producer schema.

## Missing owner/input and next action

**Missing owner/input:** the z8 producer owner must provide and approve a versioned
Kalshi response-field contract for every producer input (`yes_bid_dollars`,
`yes_ask_dollars`, and the unsuffixed fallback fields), including whether fallback
is permitted. The owner must also define the persisted representation and provenance
fields (at minimum `price_unit`, `bid_source_field`, `ask_source_field`, and a
contract/API version) before B2 can treat existing or new z8 rows as authoritative.

**Next action:** obtain that owner-approved contract, then add a read-only fixture/test
covering each permitted source-field path and asserting unit/source metadata survives
into the snapshot record. Until then, keep B2 gated and require an explicit external
unit declaration; do not infer, convert, or rewrite live data.
