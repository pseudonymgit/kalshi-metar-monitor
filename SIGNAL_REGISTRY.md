# Weather Engine Signal Registry

**Generated:** 2026-08-05 01:06 UTC
**Source:** Analysis of core/signals/ directory and WEATHER-ENGINE-APP-MAP.md

## Signal Modules Status

### Core Signals Directory (`core/signals/`)

| Signal Module | Description | Wiring Status | Notes |
|---------------|-------------|---------------|-------|
| `ai_composite_signal.py` | AI composite signal | PENDING | Experimental |
| `base_signal.py` | Base signal class | ACTIVE | Foundation class |
| `calendar_climatology_signal.py` | Calendar climatology signal | PENDING | Experimental |
| `dewpoint_depression_modulator.py` | Dewpoint depression modulation | PENDING | Experimental |
| `dual_polarity_signal.py` | Dual polarity signal | PENDING | Experimental |
| `esdr_signal.py` | ESDR signal | PENDING | Experimental |
| `fogr_reversion_signal.py` | FOG reversion signal | PENDING | Experimental |
| `forecast_disagreement_signal.py` | Forecast disagreement signal | PENDING | Needs Edge 20 |
| `frontal_detector_signal.py` | Frontal detector signal | PENDING | Experimental |
| `frontal_passage_detector.py` | Frontal passage detector | PENDING | Experimental |
| `frontal_passage_intraday_signal.py` | Frontal passage intraday signal | PENDING | Experimental |
| `frontal_passage_nowcast_signal.py` | Frontal passage nowcast signal | PENDING | Experimental |
| `gaussian_signal.py` | Gaussian signal | PENDING | Experimental |
| `gaussian_v2_signal.py` | Gaussian v2 signal | PENDING | Experimental |
| `goldilocks_signal.py` | Goldilocks signal | PENDING | Gray Room pending |
| `hrrr_bias_corrected_signal.py` | HRRR bias-corrected signal | PENDING | Experimental |
| `intraday_metar_confirmation.py` | Intraday METAR confirmation | PENDING | Experimental |
| `intraday_metar_confirmation_signal.py` | Intraday METAR confirmation signal | PENDING | Experimental |
| `metar_dtdt_signal.py` | METAR dT/dt signal | PENDING | Experimental |
| `metar_nowcast_signal.py` | METAR nowcast signal | PENDING | Experimental |
| `nwp_analog_signal.py` | NWP analog signal | PENDING | Experimental |
| `nwp_direct_signal.py` | NWP direct signal | PENDING | Experimental |
| `nwp_dtdt_fusion_signal.py` | NWP dT/dt fusion signal | PENDING | Experimental |
| `persistence_signal.py` | Persistence signal | PENDING | Experimental |
| `pressure_delta_signal.py` | Pressure delta signal | PENDING | Experimental |
| `pressure_tendency_signal.py` | Pressure tendency signal | PENDING | Experimental |
| `regime_signal.py` | Regime signal | ACTIVE | In cron path |
| `settlement_arbitrage_signal.py` | Settlement arbitrage signal | PENDING | Experimental |
| `simple_trend_signal.py` | Simple trend signal | PENDING | Experimental |
| `spike_reversion_signal.py` | Spike reversion signal | PENDING | Experimental |
| `spread_based_entry_signal.py` | Spread-based entry signal | PENDING | Experimental |
| `temperature_advection_signal.py` | Temperature advection signal | PENDING | Experimental |
| `volume_momentum_signal.py` | Volume momentum signal | PENDING | Experimental |
| `wind_direction_shift.py` | Wind direction shift signal | PENDING | Experimental |

### Core Directory Signal-Related Modules

| Module | Description | Wiring Status | Notes |
|--------|-------------|---------------|-------|
| `ensemble_fraction.py` | GEFS 31-member directional probability | ACTIVE | Core pipeline |
| `forecast_disagreement.py` | GFS vs ECMWF disagreement signal | PENDING | Needs Edge 20 |
| `lane2_goldilocks.py` | Fleeting temp-tick alerts | PENDING | Gray Room pending |
| `goldilocks_predictive.py` | Goldilocks ML variant | KILLED | Reverted Aug 3 |
| `late_day_momentum.py` | Late-day temp momentum | KILLED | 48% accuracy |
| `late_day_momentum_hourly.py` | Hourly variant | KILLED | 48% accuracy |
| `multi_model_ensemble.py` | Edge 20 multi-model (GFS/ECMWF/ICON/GEM) | PENDING | Data ready, not wired |
| `nine_signal_ensemble.py` | 9-signal fusion | PENDING | Needs calibration |
| `climatology_pillar.py` | Seasonal climatology baseline | PARK | Standalone |
| `rdae_mos.py` | Analog ensemble + MOS | PARK | Gray Room round 3 |
| `cloud_cover_modulation.py` | Cloud cover temp modulation | PARK | Experimental |
| `dewpoint_modulator.py` | Dewpoint ceiling cap | PARK | Experimental |
| `enso_regime_bias.py` | ENSO regime bias | PARK | Experimental |
| `radiational_cooling.py` | Night cooling signal | PARK | Experimental |
| `spread_momentum_signal.py` | Spread momentum | PARK | Experimental |
| `time_decay.py` | Forecast time decay | PARK | Tested, not in ensemble |
| `round_number_anchoring.py` | Price anchor effects | PARK | Experimental |
| `liquidity_weighted_ensemble.py` | Liquidity-weighted fusion | PARK | Experimental |
| `low_liquidity_traps.py` | Low-liquidity detection | PARK | Experimental |
| `station_rank_selective.py` | Station skill ranking | PARK | Experimental |
| `seasonal_diurnal_curve.py` | Seasonal diurnal calibration | PARK | Experimental |

## Status Definitions

- **ACTIVE**: Currently wired into production pipeline
- **PENDING**: Built but not yet wired into production
- **PARK**: Experimental, needs further development
- **KILLED**: Failed testing, not suitable for production

## Wiring Priority

1. **High Priority**: `multi_model_ensemble.py` (Edge 20) - Data ready, needs wiring
2. **Medium Priority**: `forecast_disagreement.py` - Needs Edge 20 integration
3. **Gray Room**: `lane2_goldilocks.py`, `goldilocks_signal.py`
4. **Archive**: KILLED modules should be moved to archive

## Total Signals: 30+ modules across core/signals/ and core/