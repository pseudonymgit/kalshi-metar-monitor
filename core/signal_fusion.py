#!/usr/bin/env python3
"""
CORE MODULE: Uncertainty-Weighted Cascade (UWC) — Multi-Signal Fusion Engine

Implements the 4-layer Uncertainty-Weighted Cascade per MULTI-SIGNAL-FUSION-SPEC.md:

Layer 1: Pool-of-pools temperature belief (Family A via Beta-binomial)
Layer 2: Settlement belief — Goldilocks + Family B modulators (likelihood ratio)
Layer 3: Bet sizing — Kelly + Family C microstructure conviction
Layer 4: Regime/State modulators (Family D as precision modulators)

Correlation correction via effective-sample-size (n_eff):
  |rho| >= 0.7 → halved weight (conservative within-pool)

Output: (direction, confidence, bayesian_conf, n_signals_agree) tuple

Preserves backward compatibility — re-exports existing fusion classes.
"""

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import numpy as np
from scipy.special import expit, logit
from scipy.stats import beta as beta_dist

from core.signal_families import (
    POOL_DEFINITIONS,
    POOL_NAMES,
    CROSS_POOL_RHO,
    FRESHNESS_HALF_LIFE_HOURS,
    AGREEMENT_GATE,
    CROSS_MODEL_DIVERGENCE,
    SPATIAL_COHERENCE,
    DEWPOINT_MODULATION,
    REGIME_MODULATION,
    GOLDILOCKS,
    SignalEntry,
    get_active_signals,
    get_pool_members,
    compute_n_eff,
    compute_cross_pool_n_eff,
    compute_pool_beta,
    get_freshness_weight,
)

# Re-export existing fusion classes from fusion_logic for backward compatibility
from core.fusion_logic import (
    SignalFusionEngine as LegacyFusionEngine,
    TimeDecaySignalManager,
    mutual_information_from_boolean_pairs,
    mutual_information_matrix,
    mutual_information_simple_correlation,
    unique_information_fraction,
    compute_weights_from_significance,
    dempster_shafer_conflict,
    apply_conflict_modulation,
)

from core.trajectory_lane import (
    TrajectoryLane,
    apply_trajectory_lane_to_probability,
)

_logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════════════════════════════

@dataclass
class FusionModeConfig:
    """Fusion mode and hyperparameters for the UWC engine."""
    mode: str = "uwc"  # "none", "uwc", "majority", "weighted"
    n_agreement_gate: int = 4                    # N-of-M pools threshold
    uncertainty_discount_cv: float = 1.0         # variance penalty coefficient
    allow_ai_signals: bool = False               # GATE for AI composite
    freshness_half_life_multiplier: float = 1.0
    correlation_penalty_threshold: float = 0.7   # |rho| >= this → halved weight
    min_signals_for_fusion: int = 2
    min_edge_threshold: float = 0.01             # minimum edge to trade
    kelly_fraction_cap: float = 0.25             # max fraction of bankroll


DEFAULT_FUSION_CONFIG = FusionModeConfig()


# ══════════════════════════════════════════════════════════════════════
# Layer 1: Pool-of-Pools Temperature Belief
# ══════════════════════════════════════════════════════════════════════

class PoolOfPools:
    """
    Layer 1: Hierarchical Beta-binomial combination of temperature signals.

    Each pool (GEFS, ECMWF, NWP-direct, Climatology, HRRR, METAR, Market)
    produces a Beta posterior via within-pool n_eff correction, then pools
    are combined with cross-pool correlation correction.
    """

    def __init__(self, config: Optional[FusionModeConfig] = None):
        self.config = config or DEFAULT_FUSION_CONFIG
        self.pool_members = get_pool_members()

    def evaluate(self,
                 signal_predictions: Dict[str, Tuple[Union[str, int], float]],
                 hours_since_update: Optional[Dict[str, float]] = None,
                 modulators: Optional[Dict[str, Any]] = None,
                 ) -> Dict[str, Any]:
        """
        Run the full pool-of-pools fusion.

        Args:
            signal_predictions: {signal_name: (direction, confidence)}
                direction: 'up'/1 or 'down'/-1
                confidence: P(direction) in [0, 1]
            hours_since_update: optional {pool_name: hours} for freshness decay
            modulators: optional dict with keys:
                'cross_model_divergence': {'agreement': float}
                'spatial_coherence': {'region_agreement': float}
                'dewpoint_depression': {'dpd': float}
                'regime': {'type': str}
                'trajectory_lane': {'lane_direction': str|None,
                                    'lane_probability': float,
                                    'lane_weight': float}

        Returns:
            Dict with pool posteriors, combined posterior, modulator logs,
            agreement gate status, and combined direction)
            combined_variance *= (1.0 - lane_weight * 0.5)

            combined["mean"] = combined_mean
            combined["variance"] = combined_variance

            modulators_applied.append({
                "modulator": "trajectory_lane",
                "lane_direction": lane_dir,
                "lane_probability": lane_prob,
                "lane_weight": lane_weight,
                "applied": True,
            })
        else:
            modulators_applied.append({
                "modulator": "trajectory_lane",
                "applied": False,
            })

        # Step 5: Agreement gate
        combined_direction = "up" if combined_mean >= 0.5 else "down"
        n_agreeing = sum(
            1 for pp in pool_posteriors.values()
            if pp["direction"] == combined_direction
        )
        n_threshold = min(self.config.n_agreement_gate, len(pool_posteriors))
        gate_passed = n_agreeing >= n_threshold

        return {
            "pool_posteriors": pool_posteriors,
            "combined": combined,
            "modulators_applied": modulators_applied,
            "agreement_gate_passed": gate_passed,
            "n_pools_agreeing": n_agreeing,
            "direction": combined_direction,
            "mean_probability": combined_mean,
        }
        """
        # Step 1: Organize predictions into pools
        pool_data: Dict[str, List[Tuple[Union[str, int], float]]] = {}
        for pool_name in POOL_NAMES:
            member_names = self.pool_members.get(pool_name, [])
            predictions = []
            for sig_name in member_names:
                if sig_name in signal_predictions:
                    predictions.append(signal_predictions[sig_name])
            if predictions:
                pool_data[pool_name] = predictions

        if not pool_data:
            _logger.warning("PoolOfPools: no pools have active signals")
            null = {"alpha": 1.0, "beta": 1.0, "mean": 0.5,
                    "n_eff_total": 0.0, "variance": 0.0833}
            return {
                "pool_posteriors": {},
                "combined": null,
                "modulators_applied": [],
                "agreement_gate_passed": False,
                "n_pools_agreeing": 0,
                "direction": None,
                "mean_probability": 0.5,
            }

        # Step 2: Compute pool-level posteriors
        pool_posteriors = {}
        for pool_name, predictions in pool_data.items():
            pool_def = POOL_DEFINITIONS.get(pool_name, {})
            rho_within = pool_def.get("rho_within", 0.5)
            alpha, beta_val, pool_n_eff = compute_pool_beta(
                pool_name, predictions, rho_within=rho_within
            )

            # Apply freshness decay
            f_mult = self.config.freshness_half_life_multiplier
            if hours_since_update and pool_name in hours_since_update:
                freshness_w = get_freshness_weight(
                    hours_since_update[pool_name], pool_name, f_mult
                )
                pool_n_eff *= freshness_w

            mean = alpha / (alpha + beta_val) if (alpha + beta_val) > 0 else 0.5
            variance = (
                (alpha * beta_val) /
                ((alpha + beta_val) ** 2 * (alpha + beta_val + 1))
            ) if (alpha + beta_val) > 0 else 0.0833

            pool_posteriors[pool_name] = {
                "alpha": alpha,
                "beta": beta_val,
                "mean": mean,
                "n_eff": pool_n_eff,
                "variance": variance,
                "direction": "up" if mean >= 0.5 else "down",
            }

        # Step 3: Apply meta-modulators as n_eff adjustments
        modulators_applied = self._apply_modulators(
            pool_posteriors, modulators or {}
        )

        # Step 4: Cross-pool correlation correction
        pool_n_effs = {}
        for pool_name, pp in pool_posteriors.items():
            k_val = pp["alpha"] - 1.0
            pool_n_effs[pool_name] = {"n_eff": pp["n_eff"], "k": k_val}

        combined_n_eff, combined_k = compute_cross_pool_n_eff(pool_n_effs)
        combined_alpha = 1.0 + combined_k
        combined_beta = 1.0 + combined_n_eff - combined_k
        if combined_beta < 0.01:
            combined_beta = 0.01
        denom = combined_alpha + combined_beta
        combined_mean = combined_alpha / denom if denom > 0 else 0.5
        combined_variance = (
            (combined_alpha * combined_beta) /
            (denom ** 2 * (denom + 1))
        ) if denom > 0 else 0.0833

        combined = {
            "alpha": combined_alpha,
            "beta": combined_beta,
            "mean": combined_mean,
            "n_eff_total": combined_n_eff,
            "variance": combined_variance,
        }

        # Step 4.5: Trajectory Lane precision modulation (if enabled)
        tl = (modulators or {}).get("trajectory_lane", {})
        if tl and tl.get("lane_direction") is not None and tl.get("lane_weight", 0.0) > 0:
            lane_dir = tl["lane_direction"]
            lane_prob = tl.get("lane_probability", 0.5)
            lane_weight = tl["lane_weight"]

            # Apply as precision modulator:
            # fused_prob = (1 - w_traj) * ensemble_prob + w_traj * trajectory_prob
            original_mean = combined_mean
            combined_mean = (
                (1.0 - lane_weight) * combined_mean
                + lane_weight * lane_prob
            )
            combined_mean = max(0.01, min(0.99, combined_mean))

            # Recompute alpha/beta to match adjusted mean while preserving
            # approximate n_eff
            denom_ab = combined_alpha + combined_beta
            if denom_ab > 0:
                combined_alpha = combined_mean * denom_ab
                combined_beta = (1.0 - combined_mean) * denom_ab

            # Variance increases slightly with lane uncertainty
            combined_variance = (
                (combined_alpha * combined_beta) /
                (denom_ab ** 2 * (denom_ab + 1))
            ) if denom_ab > 0 else 0.0833

            combined["mean"] = combined_mean
            combined["variance"] = combined_variance

            modulators_applied.append({
                "modulator": "trajectory_lane",
                "original_mean": original_mean,
                "lane_direction": lane_dir,
                "lane_probability": lane_prob,
                "lane_weight": lane_weight,
            })
        else:
            modulators_applied.append({
                "modulator": "trajectory_lane",
                "applied": False,
            })

        # Step 5: Agreement gate
        combined_direction = "up" if combined_mean >= 0.5 else "down"
        n_agreeing = sum(
            1 for pp in pool_posteriors.values()
            if pp["direction"] == combined_direction
        )
        n_threshold = min(self.config.n_agreement_gate, len(pool_posteriors))
        gate_passed = n_agreeing >= n_threshold

        return {
            "pool_posteriors": pool_posteriors,
            "combined": combined,
            "modulators_applied": modulators_applied,
            "agreement_gate_passed": gate_passed,
            "n_pools_agreeing": n_agreeing,
            "direction": combined_direction,
            "mean_probability": combined_mean,
        }

    def _apply_modulators(
        self,
        pool_posteriors: Dict[str, Dict],
        modulators: Dict[str, Any],
    ) -> List[Dict]:
        """Apply n_eff precision modulators to pool posteriors."""
        applied = []
        pools = list(pool_posteriors.keys())

        # 4.1 Cross-model divergence
        cmd = modulators.get("cross_model_divergence", {})
        agreement = cmd.get("agreement", 0.5)
        if agreement >= CROSS_MODEL_DIVERGENCE["high_agreement_threshold"]:
            mult = CROSS_MODEL_DIVERGENCE["high_agreement_boost"]
            for p in pools:
                pool_posteriors[p]["n_eff"] *= mult
        elif agreement < CROSS_MODEL_DIVERGENCE["low_agreement_threshold"]:
            mult = CROSS_MODEL_DIVERGENCE["low_agreement_penalty"]
            for p in pools:
                pool_posteriors[p]["n_eff"] *= mult
        else:
            mult = 1.0
        applied.append({"modulator": "cross_model_divergence",
                        "agreement": agreement, "multiplier": mult})

        # 4.2 Spatial coherence
        sc = modulators.get("spatial_coherence", {})
        region_agreement = sc.get("region_agreement", 0.6)
        if region_agreement >= SPATIAL_COHERENCE["high_agreement_threshold"]:
            mult = SPATIAL_COHERENCE["high_agreement_boost"]
            for p in pools:
                pool_posteriors[p]["n_eff"] *= mult
        elif region_agreement <= SPATIAL_COHERENCE["low_agreement_threshold"]:
            mult = SPATIAL_COHERENCE["low_agreement_penalty"]
            for p in pools:
                pool_posteriors[p]["n_eff"] *= mult
        else:
            mult = 1.0
        applied.append({"modulator": "spatial_coherence",
                        "region_agreement": region_agreement, "multiplier": mult})

        # 4.4 Dewpoint depression
        dd = modulators.get("dewpoint_depression", {})
        dpd = dd.get("dpd", 10.0)
        if dpd < DEWPOINT_MODULATION["humid_threshold_c"]:
            mult = DEWPOINT_MODULATION["humid_penalty"]
            for p in pools:
                pool_posteriors[p]["n_eff"] *= mult
        elif dpd > DEWPOINT_MODULATION["dry_threshold_c"]:
            mult = DEWPOINT_MODULATION["dry_boost"]
            for p in pools:
                pool_posteriors[p]["n_eff"] *= mult
        else:
            mult = 1.0
        applied.append({"modulator": "dewpoint_depression",
                        "dpd": dpd, "multiplier": mult})

        # 4.5 Regime modulation
        reg = modulators.get("regime", {})
        regime_type = reg.get("type", "normal")
        if regime_type == "seasonal_transition":
            mult = REGIME_MODULATION["seasonal_transition_penalty"]
        elif regime_type == "frontal_instability":
            mult = REGIME_MODULATION["frontal_instability_penalty"]
        elif regime_type == "blocking_pattern":
            mult = REGIME_MODULATION["blocking_pattern_boost"]
        else:
            mult = 1.0
        if mult != 1.0:
            for p in pools:
                pool_posteriors[p]["n_eff"] *= mult
        applied.append({"modulator": "regime",
                        "regime_type": regime_type or "normal", "multiplier": mult})

        # 4.6 AI composite (GATED)
        if self.config.allow_ai_signals:
            ai_conf = cmd.get("ai_confidence", 0.0)
            if ai_conf > 0.80:
                for p in pools:
                    pool_posteriors[p]["n_eff"] *= 1.10
                applied.append({"modulator": "ai_composite", "llm_confidence": ai_conf,
                                "multiplier": 1.10, "gated": False})
            else:
                applied.append({"modulator": "ai_composite", "llm_confidence": ai_conf,
                                "multiplier": 1.0, "gated": True})

        return applied


# ══════════════════════════════════════════════════════════════════════
# Layer 2: Settlement Belief (Goldilocks + Family B Modulators)
# ══════════════════════════════════════════════════════════════════════

class SettlementBelief:
    """
    Layer 2: Adjust the Layer 1 posterior for Goldilocks spike risk and
    Family B modulator signals (frontals, advection, pressure).

    P(S > B) = (1-P_G) * Beta(a1,b1) + P_G * Beta(a1_shifted,b1_shifted)
    """

    def __init__(self, config: Optional[FusionModeConfig] = None):
        self.config = config or DEFAULT_FUSION_CONFIG

    def evaluate(self,
                 layer1_result: Dict[str, Any],
                 goldilocks_probability: float = 0.0,
                 goldilocks_epsilon_f: float = 3.2,
                 hours_to_settlement: float = 24.0,
                 family_b_signals: Optional[List[Dict]] = None,
                 ) -> Dict[str, Any]:
        """Run Layer 2 settlement belief adjustment."""
        combined = layer1_result.get("combined", {})
        alpha_1 = combined.get("alpha", 1.0)
        beta_1 = combined.get("beta", 1.0)
        mean_1 = combined.get("mean", 0.5)

        # Goldilocks gate
        p_g = max(0.0, min(1.0, goldilocks_probability))
        goldilocks_gated = True
        goldilocks_applied = False

        if (p_g >= GOLDILOCKS["min_p_g"]
                and hours_to_settlement <= GOLDILOCKS["max_hours_to_settlement"]):
            goldilocks_gated = False
            eps_bins = GOLDILOCKS["epsilon_bins_f"]
            eps_w = GOLDILOCKS["epsilon_weights"]
            mean_epsilon = sum(e * w for e, w in zip(eps_bins, eps_w))
            eps_scaled = mean_epsilon / 3.2 * goldilocks_epsilon_f
            shift = eps_scaled * 0.15
            shifted_mean = (mean_1 - shift * p_g) if mean_1 > 0.5 else (mean_1 + shift * p_g)
            shifted_mean = max(0.01, min(0.99, shifted_mean))

            var_1 = combined.get("variance", 0.0833)
            mix_var = var_1 + p_g * (1 - p_g) * (shift * 2) ** 2
            mix_var = max(0.0001, mix_var)
            total = (shifted_mean * (1 - shifted_mean) / mix_var) - 1.0
            total = max(0.1, total)
            alpha_1 = shifted_mean * total
            beta_1 = (1 - shifted_mean) * total
            goldilocks_applied = True

        # Family B via likelihood ratio
        frontal_lr = 1.0
        if family_b_signals:
            for fb in family_b_signals:
                direction = fb.get("direction", "up")
                conf = fb.get("confidence", 0.5)
                ensemble_dir = layer1_result.get("direction", "up")
                agrees = direction == ensemble_dir
                lr = 1.0 + 0.2 * (conf - 0.5) if agrees else 1.0 - 0.3 * (conf - 0.5)
                frontal_lr *= max(0.1, lr)

        # Apply LR to posterior odds
        prior_odds = mean_1 / (1 - mean_1) if mean_1 < 0.999 else 500.0
        posterior_odds = prior_odds * frontal_lr
        adjusted_mean = posterior_odds / (1 + posterior_odds)

        final_mean = adjusted_mean
        total_alpha, total_beta = alpha_1, beta_1

        if adjusted_mean != mean_1:
            denom_ab = total_alpha + total_beta
            var = (total_alpha * total_beta / (denom_ab ** 2 * (denom_ab + 1))
                   ) if denom_ab > 0 else 0.0833
            var = max(0.0001, var)
            t = (final_mean * (1 - final_mean) / var) - 1.0
            t = max(0.1, t)
            total_alpha = final_mean * t
            total_beta = (1 - final_mean) * t

        denom_ab = total_alpha + total_beta
        final_var = (total_alpha * total_beta / (denom_ab ** 2 * (denom_ab + 1))
                     ) if denom_ab > 0 else 0.0833

        return {
            "posterior": {"alpha": total_alpha, "beta": total_beta,
                          "mean": final_mean, "variance": final_var},
            "goldilocks_gated": goldilocks_gated,
            "goldilocks_applied": goldilocks_applied,
            "frontal_likelihood_ratio": frontal_lr,
        }


# ══════════════════════════════════════════════════════════════════════
# Layer 3: Kelly Bet Sizing with Market Microstructure Conviction
# ══════════════════════════════════════════════════════════════════════

class BetSizing:
    """
    Layer 3: Compute edge, Kelly fraction, adjust with Family C signals.

    f* = Edge_effective / (1 - m)
    conviction_mult = bayes_factor(whale_anomaly, direction_alignment)
    f_adjusted = f* * conviction_mult * tier_discount * fill_discount
    """

    def __init__(self, config: Optional[FusionModeConfig] = None):
        self.config = config or DEFAULT_FUSION_CONFIG

    def evaluate(self,
                 layer2_result: Dict[str, Any],
                 market_price: float = 0.5,
                 fee_rate: float = 0.07,
                 station_tier: int = 1,
                 bankroll: float = 10000.0,
                 family_c_signals: Optional[List[Dict]] = None,
                 ) -> Dict[str, Any]:
        """Run Layer 3 bet sizing."""
        posterior = layer2_result.get("posterior", {})
        mean_p = posterior.get("mean", 0.5)
        variance = posterior.get("variance", 0.0833)

        edge = mean_p - market_price - fee_rate
        c_v = self.config.uncertainty_discount_cv
        edge_effective = edge - c_v * math.sqrt(max(0.0, variance))

        if 0.001 < market_price < 0.999:
            raw_kelly = max(0.0, edge_effective / (1.0 - market_price))
        else:
            raw_kelly = 0.0

        conviction_mult = self._compute_conviction_mult(family_c_signals)
        tier_discount = max(0.0, (station_tier - 1) * 0.05)
        fill_discount = 0.0

        f_adjusted = raw_kelly * conviction_mult * (1.0 - tier_discount) * (1.0 - fill_discount)
        f_final = min(f_adjusted, self.config.kelly_fraction_cap)
        position = f_final * bankroll

        return {
            "edge": edge,
            "edge_effective": edge_effective,
            "kelly_raw": raw_kelly,
            "kelly_adjusted": f_adjusted,
            "conviction_multiplier": conviction_mult,
            "position": position,
            "fraction": f_final,
        }

    def _compute_conviction_mult(self, family_c_signals: Optional[List[Dict]]) -> float:
        """Compute conviction multiplier from Family C signals."""
        mult = 1.0
        if not family_c_signals:
            return mult
        for sig in family_c_signals:
            name = sig.get("signal_name", "")
            aligned = sig.get("direction_aligned", True)
            if name == "volume_momentum_signal" and aligned:
                mult *= 1.0 + 0.3 * min(abs(sig.get("z_score", 0.0)), 2.0)
            elif name == "settlement_arbitrage_signal":
                mult *= 1.0 + 0.2 * min(abs(sig.get("price_deviation", 0.0)), 1.0)
            elif name == "spike_reversion_signal" and not aligned:
                mult *= 1.0 - 0.15 * min(abs(sig.get("spike_magnitude", 0.0)), 1.0)
            elif name == "fogr_reversion_signal" and aligned:
                mult *= 1.0 + 0.1 * min(abs(sig.get("z_score", 0.0)), 2.0)
            elif name == "simple_trend_signal" and aligned:
                mult *= 1.0 + 0.25 * min(abs(sig.get("trend_strength", 0.0)), 2.0)
        return max(0.1, mult)


# ══════════════════════════════════════════════════════════════════════
# Full Cascade Orchestrator
# ══════════════════════════════════════════════════════════════════════

class UncertaintyWeightedCascade:
    """Full 4-layer UWC orchestration: L1 (pools) -> L2 (settlement) -> L3 (Kelly)."""

    def __init__(self, config: Optional[FusionModeConfig] = None):
        self.config = config or DEFAULT_FUSION_CONFIG
        self.layer1 = PoolOfPools(config)
        self.layer2 = SettlementBelief(config)
        self.layer3 = BetSizing(config)

    def fuse(self,
             signal_predictions: Dict[str, Tuple[Union[str, int], float]],
             market_price: float = 0.5,
             fee_rate: float = 0.07,
             bankroll: float = 10000.0,
             station_tier: int = 1,
             hours_to_settlement: float = 24.0,
             hours_since_update: Optional[Dict[str, float]] = None,
             modulators: Optional[Dict[str, Any]] = None,
             goldilocks_probability: float = 0.0,
             goldilocks_epsilon_f: float = 3.2,
             family_b_signals: Optional[List[Dict]] = None,
             family_c_signals: Optional[List[Dict]] = None,
             ) -> Dict[str, Any]:
        """Run full UWC cascade. Returns diagnostic ledger with verdict."""
        if not signal_predictions or len(signal_predictions) < self.config.min_signals_for_fusion:
            return self._null_result("Insufficient signals for fusion")

        # Layer 1
        l1 = self.layer1.evaluate(
            signal_predictions=signal_predictions,
            hours_since_update=hours_since_update,
            modulators=modulators,
        )
        if not l1["agreement_gate_passed"]:
            return self._null_result(
                f"Agreement gate failed: {l1['n_pools_agreeing']}/"
                f"{len(l1['pool_posteriors'])} pools agree"
            )

        # Layer 2
        l2 = self.layer2.evaluate(
            layer1_result=l1,
            goldilocks_probability=goldilocks_probability,
            goldilocks_epsilon_f=goldilocks_epsilon_f,
            hours_to_settlement=hours_to_settlement,
            family_b_signals=family_b_signals,
        )

        # Layer 3
        l3 = self.layer3.evaluate(
            layer2_result=l2,
            market_price=market_price,
            fee_rate=fee_rate,
            bankroll=bankroll,
            station_tier=station_tier,
            family_c_signals=family_c_signals,
        )

        # Output
        posterior_mean = l2["posterior"]["mean"]
        direction = "up" if posterior_mean >= 0.5 else "down"
        confidence = posterior_mean if direction == "up" else 1.0 - posterior_mean

        n_agree = sum(
            1 for (sd, _) in signal_predictions.values()
            if (isinstance(sd, str) and sd.lower() == direction)
            or (isinstance(sd, (int, float)) and sd > 0 and direction == "up")
            or (isinstance(sd, (int, float)) and sd < 0 and direction == "down")
        )
        edge_eff = l3.get("edge_effective", 0.0)
        verdict = "TRADE" if (edge_eff > self.config.min_edge_threshold) else "NO_TRADE"
        reason = (
            f"Edge {edge_eff:.4f} > {self.config.min_edge_threshold}, "
            f"frac {l3['fraction']:.4f}, "
            f"{l1['n_pools_agreeing']}/{len(l1['pool_posteriors'])} pools agree"
        ) if verdict == "TRADE" else (
            f"Edge {edge_eff:.4f} <= threshold"
        )

        return {
            "direction": direction if verdict == "TRADE" else None,
            "confidence": confidence,
            "bayesian_conf": posterior_mean,
            "n_signals_agree": n_agree,
            "total_signals": len(signal_predictions),
            "layers": {
                "layer1": {"pools": l1["pool_posteriors"],
                           "combined": l1["combined"],
                           "modulators_applied": l1["modulators_applied"]},
                "layer2": {"goldilocks_gated": l2["goldilocks_gated"],
                           "goldilocks_applied": l2["goldilocks_applied"],
                           "frontal_likelihood_ratio": l2["frontal_likelihood_ratio"],
                           "posterior": l2["posterior"]},
                "layer3": l3,
            },
            "verdict": verdict,
            "reason": reason,
        }

    def _null_result(self, reason: str) -> Dict[str, Any]:
        """Return a null result when fusion cannot proceed."""
        return {
            "direction": None,
            "confidence": 0.0,
            "bayesian_conf": 0.5,
            "n_signals_agree": 0,
            "total_signals": 0,
            "layers": {},
            "verdict": "NO_TRADE",
            "reason": reason,
        }


# ══════════════════════════════════════════════════════════════════════
# Simplified Fusion Wrappers (for big_sweep integration)
# ══════════════════════════════════════════════════════════════════════

def fuse_majority_vote(signals: Dict[str, Tuple[Union[str, int], float]]
                       ) -> Tuple[Optional[str], float, int]:
    """Simple majority vote fusion (no confidence weighting)."""
    if not signals:
        return None, 0.0, 0
    ups = sum(1 for d, _ in signals.values()
              if (isinstance(d, str) and d.lower() == 'up')
              or (isinstance(d, (int, float)) and d > 0))
    downs = len(signals) - ups
    if ups == downs:
        return None, 0.5, len(signals)
    direction = "up" if ups > downs else "down"
    confidence = max(ups, downs) / len(signals)
    return direction, confidence, len(signals)


def fuse_weighted_vote(signals: Dict[str, Tuple[Union[str, int], float]]
                       ) -> Tuple[Optional[str], float, int]:
    """Confidence-weighted vote fusion."""
    if not signals:
        return None, 0.0, 0
    total_weight = 0.0
    up_weight = 0.0
    for d, c in signals.values():
        w = max(0.01, min(0.99, c))
        total_weight += w
        if (isinstance(d, str) and d.lower() == 'up') or (isinstance(d, (int, float)) and d > 0):
            up_weight += w
    if total_weight <= 0:
        return None, 0.5, len(signals)
    prob = up_weight / total_weight
    direction = "up" if prob >= 0.5 else "down"
    confidence = prob if direction == "up" else 1.0 - prob
    return direction, confidence, len(signals)
