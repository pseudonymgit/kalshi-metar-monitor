# Layer 5 Adjacent Runtime Remediation Spec — 2026-08-15

## Scope and safety boundary

Bounded cleanup for the four residual failures in `tests/test_hydration_queue_integration_paths.py` after the Layer 5 hardening batch. This work is limited to the alert-path runtime guard and certification evidence. No broad cleanup, module deletion, deployment, production configuration, paper/shadow trading, or live-trading changes.

Production/domain guards remain mandatory: only the canonical `production` domain may deliver production alerts or perform production hydration; non-production and unknown domains remain blocked.

## Prior receipt and baseline

Prior receipt: `.meta/continuity/weather-engine/2026-08-15-layer5-runtime-hardening.md`.

It recorded:

- focused Layer 5 suite: 49 passed;
- extended adjacent runtime suite: 65 passed, 4 failed;
- all four residual failures were in `tests/test_hydration_queue_integration_paths.py`;
- an unrelated paper-trading `UnboundLocalError` traceback was emitted by existing app autostart behavior.

## QC / failure classification

Command:

```text
pytest -q tests/test_hydration_queue_integration_paths.py
```

Initial result: **6 passed, 4 failed**.

The four failures were:

1. `HydrationQueueIntegrationPathTests.test_alert_path_enqueues_hydration_without_inline_execution`
2. `TransitionEvaluationWindowTests.test_alert_evaluation_proceeds_for_prior_cycle_transition_within_window`
3. `TransitionEvaluationWindowTests.test_alert_suppressed_when_prior_transition_outside_window`
4. `TransitionEvaluationWindowTests.test_window_comparison_deterministic_for_identical_inputs`

Classification: one shared source guard defect, not four independent test defects. `core/metar_monitor.py::_send_alert` used stale short-form allow-list values (`prod`, `sbox`, `dev`) while `_current_kalshi_execution_domain()` and the Layer 5 contract use canonical long-form values (`production`, `sandbox`, `development`). The guard returned before queueing/evaluation, so the tests' mocked queue and annotation paths were never reached. This was confirmed by the first failure's immediate `mock_enqueue.called == False` assertion and the remaining tests' missing annotation calls.

## Smallest fix

Changed exactly one source block in `core/metar_monitor.py`:

```python
allowed_domains = {"production", "sandbox", "development"}
```

No test changes, queue behavior changes, hydration implementation changes, or guard weakening were made. Unknown domains remain blocked. The existing guard still rejects every domain outside the explicit canonical set.

## Verification plan and results

Focused Layer 5 command:

```text
pytest -q tests/test_kalshi_connectivity_snapshot.py tests/test_hydration_queue.py tests/test_execution_domain_endpoint.py tests/test_structural_hardening.py
```

Result: **49 passed in 0.79s**.

Residual integration command:

```text
pytest -q tests/test_hydration_queue_integration_paths.py
```

Result after fix: **10 passed in 0.27s**; residuals: **0**.

Adjacent runtime command:

```text
pytest -q tests/test_kalshi_connectivity_snapshot.py tests/test_hydration_queue.py tests/test_execution_domain_endpoint.py tests/test_structural_hardening.py tests/test_hydration_queue_integration_paths.py tests/test_hydration_stall_signal.py tests/test_observability_hydration_runtime.py tests/test_observability_kalshi_connectivity_runtime.py tests/test_transition_persistence_execution_domain.py
```

Result: **69 passed in 0.93s**.

The adjacent run emitted the pre-existing paper-trading traceback described below; it did not fail the test process.

## Full-suite collection triage (no broad cleanup)

Command:

```text
python3 -m pytest --collect-only -q
```

Result: **912 tests collected, 8 collection errors**. The eight stale import/module errors are:

| Collector | Error | Classification |
|---|---|---|
| `scripts/single_combo_test.py` | `ModuleNotFoundError: core.dewpoint_modulator` | stale script import; module absent |
| `scripts/test_alert_builder.py` | `ModuleNotFoundError: alert_builder` | stale script-local import/path |
| `scripts/test_fused_ensemble.py` | via `scripts.bmode_p1_backtest`: `ModuleNotFoundError: core.forecast_confidence_modulator` | stale dependency import; module absent |
| `scripts/test_polymarket_whale.py` | `ModuleNotFoundError: core.polymarket_whale_db` | stale script import; module absent |
| `tests/integration_test.py` | via `scripts.bmode_p1_backtest`: `ModuleNotFoundError: core.forecast_confidence_modulator` | same stale dependency path |
| `tests/test_p3_prediction.py` | `ImportError: cannot import name 'p3_feature_extractor' from 'core'` | retired/missing P3 module |
| `tests/test_position_sizing.py` | `ModuleNotFoundError: core.position_sizing` | stale module name; current code is not this import path |
| `tests/test_signals.py` | `AttributeError: module 'core.signals' has no attribute 'PersistenceSignal'` | stale API expectation |

These are collection-only findings. No stale modules were recreated, deleted, or broad-cleaned in this bounded task. Recommended follow-up is a separately scoped test inventory/retirement decision, not an incidental fix during Layer 5 certification.

## Existing paper-trading traceback

Captured during the adjacent runtime run:

```text
Traceback (most recent call last):
  File "app.py", line 3921, in _paper_trading_loop
    daily_paper_run()
  File "core/paper_trading_engine.py", line 3115, in daily_paper_run
    run_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
               ^^^^^^^^
UnboundLocalError: cannot access local variable 'datetime' where it is not associated with a value
```

Classification: pre-existing paper-trading runtime defect, outside this task's scope. It was not modified because the request explicitly excludes paper/shadow trading changes. It remains a named residual and must not be interpreted as runtime certification of the paper-trading path.

## Changed files

- `core/metar_monitor.py` — canonicalized the `_send_alert` execution-domain allow-list only.
- `docs/plans/LAYER5-ADJACENT-RUNTIME-REMEDIATION-SPEC-2026-08-15.md` — this spec and triage report.
- `.meta/continuity/weather-engine/2026-08-15-layer5-adjacent-runtime.md` — completion receipt.

## Accuracy-focused suggestion

Add a contract test that parameterizes every canonical execution domain and asserts both sides of the boundary: `production` reaches the alert evaluation/queue path, while `sandbox`, `development`, and an unknown token remain blocked. This would detect future alias drift at the trust boundary without touching trading behavior or production configuration.
