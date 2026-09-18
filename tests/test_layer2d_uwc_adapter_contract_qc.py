"""Phase 1 QC: UWC adapter contract, no comparison execution.

These tests characterize the existing UWC result and enforce the stop rule:
its confidence is directional posterior confidence, but fired/abstain is gated
by edge/bet-sizing semantics and the engine has a hidden two-signal minimum.
That is not a comparable LLOP adapter contract without a reviewed adapter.
"""

from core.signal_fusion import FusionModeConfig, UncertaintyWeightedCascade


SIGNALS = {"pressure_delta": ("up", 0.80), "seasonal_regime": ("up", 0.70)}


def test_uwc_confidence_is_directional_posterior_not_sizing_value():
    result = UncertaintyWeightedCascade(
        FusionModeConfig(mode="uwc", floor_type="none")
    ).fuse(SIGNALS, station="KATL")

    assert 0.0 <= result["confidence"] <= 1.0
    assert result["confidence"] == result["bayesian_conf"]
    assert result["direction"] in {"up", "down", None}
    # Layer 3 edge/fraction are separate quantities; neither is the adapter
    # confidence and neither may be silently substituted for it.
    assert result["layers"]["layer3"]["fraction"] != result["confidence"]


def test_uwc_firing_is_not_directional_confidence_contract():
    result = UncertaintyWeightedCascade(
        FusionModeConfig(mode="uwc", floor_type="none")
    ).fuse(SIGNALS, station="KATL")

    # This valid posterior can abstain because the bet-sizing edge gate fails.
    assert result["confidence"] > 0.5
    assert result["direction"] is None
    assert result["verdict"] == "NO_TRADE"
    assert "Edge " in result["reason"]
    assert "direction=" in result["reason"]


def test_uwc_has_hidden_minimum_signal_requirement_and_no_contract_version():
    config = FusionModeConfig(mode="uwc", floor_type="none")
    result = UncertaintyWeightedCascade(config).fuse(
        {"pressure_delta": ("up", 0.80)}, station="KATL"
    )

    assert config.min_signals_for_fusion == 2
    assert result["direction"] is None
    assert result["verdict"] == "NO_TRADE"
    assert result["reason"] == "Insufficient signals for fusion"
    assert not hasattr(config, "config_version")


def test_uwc_output_is_deterministic_for_identical_input():
    config = FusionModeConfig(mode="uwc", floor_type="none")
    first = UncertaintyWeightedCascade(config).fuse(SIGNALS, station="KATL")
    second = UncertaintyWeightedCascade(config).fuse(SIGNALS, station="KATL")
    assert first == second
