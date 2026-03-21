# CURRENT STATE

## 1. TARGET REALITY
- Current branch: `work`
- Current commit: inspect with `git rev-parse HEAD` at handoff time.
- Working tree status: inspect with `git status --short --branch` at handoff time; the tree was clean before this doc was created.

## 2. COMPLETED RECENT WORK
- Restored hydration polling and observability guardrails.
- Switched alert-path truth retrieval to persisted transition reads.
- Aligned live station universe with market discovery.
- Restored missing-ladder dedupe behavior.

## 3. CONFIRMED OPEN ITEMS
- `/observability/pipeline-truth` is non-authoritative and logic-stubbed.
- `tests/test_pipeline_truth_endpoint.py` is red because the `client` fixture is missing.
- `/observability/market-eligibility-runtime` still documents an unreachable `NO_DIRECTIONAL_LADDER_MATCH` branch.
- Hydration observability semantics remain mixed between persisted and live state.

## 4. CONFIRMED DEFERRED ITEMS
- `runtime-authority-snapshot` adoption is still being completed in practice.
- `legacy.transition_correlation` remains active and must not be removed.
- `legacy.delta_f` is not yet safe to remove.
- `get_latest_metar()` remains a compatibility shim still used by `app.py`.

## 5. LIVE PRODUCTION SYMPTOMS
- Real alerts are occurring again for some stations.
- Ladder-missing alerts are still also occurring.
- Both behaviors appear limited to stations from the original user station list.
- The same city/station family can produce both a real alert and a ladder-missing alert.
- This remains an active runtime symptom, not a closed item.

## 6. NON-AUTHORITATIVE OR RISKY SURFACES
- `/observability/pipeline-truth` should not be treated as authoritative.
- `hydration-prerequisite-runtime` still mixes persisted and live semantics in one payload.

## 7. NEXT RECOMMENDED ACTIONS
- Make `runtime-authority-snapshot` the primary operator diagnosis surface everywhere.
- Fix or remove the red `pipeline-truth` endpoint test by adding the missing fixture or converting it to the active test harness.
- Reconcile hydration observability payloads so persisted vs live authority is explicit per field.
