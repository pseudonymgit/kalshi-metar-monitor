# Layer 2D UWC Adapter Contract — 2026-08-15

## Disposition
**BLOCKED / KILL FOR CURRENT COMPARISON.** UWC is not semantically comparable to the locked LLOP directional-prediction contract without a new reviewed adapter design. No LLOP/UWC A/B artifact was produced.

## QC evidence
Focused contract tests: `python3 -m pytest -q tests/test_layer2d_uwc_adapter_contract_qc.py --disable-warnings` → **4 passed**.

The current UWC implementation (`core/signal_fusion.py`) establishes:

1. `confidence` is the Bayesian posterior (`bayesian_conf`), not a trade-size or edge value. That value is bounded, but this alone does not make the engine comparable.
2. `fired/abstain` is not a directional-confidence decision. With two valid same-direction signals, UWC can return `direction=None`, `verdict=NO_TRADE`, and an `Edge ...` reason because its layer-3 bet-sizing/edge gate rejects the output.
3. The default configuration has `min_signals_for_fusion=2`; one signal returns `NO_TRADE` with `Insufficient signals for fusion`.
4. There is no explicit UWC adapter/config version contract.
5. UWC returns a diagnostic dictionary; LLOP consumes/returns a different tuple-oriented contract. Rejection semantics and confidence/firing meanings are not equivalent.

## Decision
Do not fabricate a translation from UWC posterior/edge/NO_TRADE semantics into an LLOP-style directional prediction. Do not tune UWC thresholds to force comparability. Do not run the Layer 2D A/B comparison under the current contract.

Layer 2D can be reopened only under a separately specified and reviewed adapter that declares whether the endpoint is directional accuracy or trade/no-trade utility, preserves abstentions, and proves identical input/output semantics.

## Next roadmap
Proceed with LLOP-only independent review of the matched signal result, while keeping UWC as a parked architecture experiment. A future UWC resurrection requires a new spec and fresh label-safe comparison; it is not part of the current promotion path.

## Accuracy improvement suggestion
Keep the current pair evaluation on the LLOP directional endpoint and separately evaluate UWC as a trade/no-trade/P&L policy. Forcing UWC into directional accuracy would conflate posterior belief with execution abstention and could hide or manufacture apparent accuracy gains.
