# SWEEP-AUDIT-SPEC: Full Fire-Rate Audit

## Objective
Update `scripts/audit_all_signals.py` to test the **full date range** per station (all ~2,047 days each, ~40,893 total across 20 stations), include **all signal files** from `core/signals/`, and produce a comprehensive audit.

## Key Changes

### 1. Full Date Range (Not 100 Samples)
- Remove `samples_per_station` parameter (or default to 0 = all)
- Iterate ALL days from `min_lookback` to `len(days)` for each station
- Total expected: ~40,893 samples per signal × 32+ signals = ~1.3M evaluations

### 2. Add ALL Signal Files From `core/signals/`
Files currently missing from the audit registry (12 files):

| File | Class | Status |
|------|-------|--------|
| `ai_composite_signal.py` | `AiCompositeSignal` | Extends BaseSignal, has evaluate() |
| `base_signal.py` | `BaseSignal` | ABC — NOT a signal, skip |
| `dewpoint_depression_modulator.py` | Module-level functions | NOT a signal class, skip |
| `esdr_signal.py` | `EsdrSignal` | Extends BaseSignal, has evaluate() |
| `frontal_detector_signal.py` | `FrontalDetectorSignal` | Extends BaseSignal, has evaluate() |
| `frontal_passage_detector.py` | Module-level functions | NOT a signal class, skip |
| `intraday_metar_confirmation.py` | `IntradayMETARConfirmation` | NOT BaseSignal subclass, has get_intraday_confirmation() |
| `metar_nowcast_signal.py` | `MetarNowcastSignal` | Extends BaseSignal, has evaluate() |
| `nwp_analog_signal.py` | `NwpAnalogSignal` | Extends BaseSignal, has evaluate() |
| `spread_based_entry_signal.py` | `SpreadBasedEntryDetector` | Extends BaseSignal, has evaluate() |
| `temperature_advection_signal.py` | `TemperatureAdvectionSignal` | Extends BaseSignal, has evaluate() |
| `weatherapi_base_signal.py` | `WeatherAPIBaseSignal` | Extends BaseSignal, has evaluate() |

**Classification:**
- **Base class (skip):** `base_signal.py`
- **Module-level functions (skip):** `dewpoint_depression_modulator.py`, `frontal_passage_detector.py`, `intraday_metar_confirmation.py`
- **Add to audit:** `ai_composite`, `esdr`, `frontal_detector`, `metar_nowcast`, `nwp_analog`, `spread_based_entry`, `temperature_advection`, `weatherapi_base`

### 3. Performance
- Run without `--samples` flag → full date range
- Estimate: ~40,893 × 32 signals = ~1.3M evaluate() calls
- Use try/except per signal, log timing
- Option to use `--samples N` for quick mode

### 4. Output
- `data/signal_fire_audit.json` with full results
- Summary: fire rate, direction, confidence, errors per signal
- Signal-level timing

### 5. Correlation Matrix Update
- `scripts/compute_signal_correlation_matrix.py` currently uses `BACKTEST_SIGNALS` (15 signals)
- Update to use all firing signals from the audit
- Set `--samples 0` or default to full range

## QC Check
- Full audit runs to completion
- Covers all signal files (excluding base/module-only)
- At least 36,000 total samples per signal
- Reports fire rates and error counts for each signal
- Timing information logged