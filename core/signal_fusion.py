#!/usr/bin/env python3
"""
<<<<<<< HEAD
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
=======
CORE MODULE: Signal Fusion & Information-Theoretic Aggregation

Implements the three-layer fusion stack:
1. Layer 0: Signal conditioning with isotonic calibration (uses core/calibration_pipeline)
2. Layer 1: Mutual-information-based decorrelation weighting
3. Layer 2: Log-odds linear opinion pool (LLOP)
4. Layer 3: Dempster-Shafer conflict detection

Based on Expert 2 Signal Fusion & Information Theory Analysis (Gray Room Round 3)
"""

import os
import logging
import numpy as np
import math
from collections import defaultdict
from scipy.special import expit, logit
from core.calibration_pipeline import CalibrationPipeline
>>>>>>> origin/main

_logger = logging.getLogger(__name__)


<<<<<<< HEAD
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
=======
def mutual_information_from_boolean_pairs(pairs_i, pairs_j, outcomes):
    """
    Compute mutual information I(X_i; X_j | outcomes) for binary predictions.
    
    Uses outcomes (actual realized directions) to compute conditional mutual
    information: I(X_i; X_j | Y) = sum_y P(Y=y) * I(X_i; X_j | Y=y).
    
    When outcomes are real ('up'/'down' strings or boolean 0/1), the function
    partitions observations by outcome and computes conditional MI.
    When outcomes are placeholder values (e.g., 'unknown'), falls back to
    unconditional MI on prediction patterns.
    
    Args:
        pairs_i: List of (direction_pred_i, confidence_i) 
        pairs_j: List of (direction_pred_j, confidence_j)
        outcomes: List of outcomes (actual_direction strings or 'unknown' placeholders)
        
    Returns:
        Mutual information in bits
    """
    if len(pairs_i) < 50 or len(pairs_j) < 50 or len(outcomes) < 50:
        return 0.0  # Insufficient data
        
    if len(pairs_i) != len(pairs_j) or len(pairs_i) != len(outcomes):
        raise ValueError("All arrays must have same length")

    # Check if we have real outcomes (not placeholder 'unknown' values)
    has_real_outcomes = any(o not in (None, 'unknown') for o in outcomes)

    def _compute_mi_from_preds(pred_i, pred_j):
        """Internal helper: compute unconditional MI from two prediction lists."""
        joint_counts = defaultdict(lambda: defaultdict(int))
        i_counts = defaultdict(int)
        j_counts = defaultdict(int)
        total = len(pred_i)

        for p_i, p_j in zip(pred_i, pred_j):
            joint_counts[p_i][p_j] += 1
            i_counts[p_i] += 1
            j_counts[p_j] += 1

        mi = 0.0
        for x in joint_counts:
            for y in joint_counts[x]:
                px = i_counts[x] / total
                py = j_counts[y] / total
                pxy = joint_counts[x][y] / total
                if px > 0 and py > 0 and pxy > 0:
                    mi += pxy * math.log2(pxy / (px * py))
        return max(0.0, mi)

    def _discretize_pred(dir_val, conf_val):
        """Convert (direction, confidence) to a discretized category."""
        strength = 'strong' if conf_val > 0.7 else 'weak'
        pred = 1 if dir_val == 'up' else 0
        return (strength, pred)

    if has_real_outcomes:
        # Compute conditional mutual information I(X_i; X_j | Y)
        # Partition observations by outcome value
        outcome_groups = defaultdict(list)  # outcome -> [(pred_i, pred_j), ...]
        for (dir_i, conf_i), (dir_j, conf_j), actual in zip(pairs_i, pairs_j, outcomes):
            if actual in (None, 'unknown'):
                continue
            outcome_key = 'up' if actual == 'up' or actual == 1 else 'down'
            p_i = _discretize_pred(dir_i, conf_i)
            p_j = _discretize_pred(dir_j, conf_j)
            outcome_groups[outcome_key].append((p_i, p_j))

        if len(outcome_groups) < 2:
            # Only one outcome class observed — fall back to unconditional MI
            total = len(pairs_i)
            pred_i_disc = [_discretize_pred(d, c) for d, c in pairs_i]
            pred_j_disc = [_discretize_pred(d, c) for d, c in pairs_j]
            return _compute_mi_from_preds(pred_i_disc, pred_j_disc)

        conditional_mi = 0.0
        total_obs = sum(len(v) for v in outcome_groups.values())
        for outcome_key, obs_list in outcome_groups.items():
            if len(obs_list) < 10:
                continue
            p_i_list = [item[0] for item in obs_list]
            p_j_list = [item[1] for item in obs_list]
            mi_given_y = _compute_mi_from_preds(p_i_list, p_j_list)
            weight = len(obs_list) / total_obs
            conditional_mi += weight * mi_given_y

        return max(0.0, conditional_mi)

    else:
        # No real outcomes — unconditional MI on prediction patterns
        pred_i_disc = []
        pred_j_disc = []
        for (dir_i, conf_i), (dir_j, conf_j) in zip(pairs_i, pairs_j):
            pred_i_disc.append(_discretize_pred(dir_i, conf_i))
            pred_j_disc.append(_discretize_pred(dir_j, conf_j))

        return _compute_mi_from_preds(pred_i_disc, pred_j_disc)


def mutual_information_matrix(signals_history, signal_names, n_bins=3, outcomes=None):
    """
    Compute symmetric mutual information matrix for all signal pairs.
    
    Args:
        signals_history: dict {signal_name: [(direction, conf, correct, date), ...]}
        signal_names: list of signal names
        n_bins: number of bins for discretization (to simplify MI calculation)
        outcomes: optional dict {date: actual_direction} or list aligned with common_dates.
                  If None, attempts to extract real outcomes from signals_history (the
                  'correct' boolean in the 3rd position of each entry tuple).
        
    Returns:
        2D array: MI matrix [n_signals x n_signals] with elements > 0 indicating
                  degraded quality when outcomes were unavailable
    """
    # Logger already defined at module level

    n_signals = len(signal_names)
    if n_signals == 0:
        return []
        
    mi_matrix = [[0.0 for _ in range(n_signals)] for _ in range(n_signals)]
    
    # Collect data by date
    all_dates = set()
    signal_predictions = {}
    
    # Extract outcome data from signals_history if needed
    # signals_history entries: (direction, conf, correct, date)
    # The 'correct' boolean (3rd element) is the outcome indicator
    extracted_outcomes = {}
    if outcomes is None:
        for sig_name in signal_names:
            if sig_name in signals_history:
                for entry in signals_history[sig_name]:
                    if len(entry) >= 4:
                        date = entry[3]
                        correct = entry[2]  # boolean: was prediction correct?
                        if isinstance(correct, bool):
                            # Convert boolean to 'up'/'down' based on prediction direction
                            direction = entry[0]  # 'up' or 'down'
                            if date not in extracted_outcomes:
                                extracted_outcomes[date] = direction if correct else ('up' if direction == 'down' else 'down')
        if extracted_outcomes:
            outcomes = extracted_outcomes
            _logger.info(
                f"Extracted {len(outcomes)} real outcomes from signals_history "
                f"for MI computation"
            )
        else:
            outcomes = {}

    # Check if we have real outcomes (not empty dict)
    has_real_outcomes = len(outcomes) > 0

    # Collect date-mapped predictions
    for sig_name in signal_names:
        if sig_name in signals_history:
            sig_hist = signals_history[sig_name]
            # Map date to (direction, confidence) only when signal predicted
            date_mappings = {entry[3]: (entry[0], entry[1]) for entry in sig_hist if len(entry) > 3}
            signal_predictions[sig_name] = date_mappings
            all_dates.update(date_mappings.keys())
    
    # Only consider days where both signals predicted (co-fired)
    degraded_flag = False
    for i, sig_i in enumerate(signal_names):
        for j, sig_j in enumerate(signal_names):
            if i == j:
                mi_matrix[i][j] = 0.0  # MI with itself is 0 in this context
                continue
            
            predictions_i = signal_predictions.get(sig_i, {})
            predictions_j = signal_predictions.get(sig_j, {})
            
            # Find days when both signals fired
            common_dates = set(predictions_i.keys()).intersection(set(predictions_j.keys()))
            
            if len(common_dates) < 50:  # Need min samples
                continue
                
            # Get paired predictions
            pairs_i = [predictions_i[date] for date in common_dates if date in predictions_i]
            pairs_j = [predictions_j[date] for date in common_dates if date in predictions_j]

            if has_real_outcomes:
                # Build outcomes list aligned with common_dates for this pair
                outcome_list = []
                for date in common_dates:
                    if date in outcomes:
                        outcome_list.append(outcomes[date])
                    else:
                        outcome_list.append('unknown')

                # Compute MI using real outcomes (conditional MI)
                mi_value = mutual_information_from_boolean_pairs(pairs_i, pairs_j, outcome_list)
                mi_matrix[i][j] = mi_value
            else:
                # No real outcomes available — log warning once and fall back to pattern-only correlation
                if not degraded_flag:
                    _logger.warning(
                        "mutual_information_matrix: no real outcomes available. "
                        "Falling back to pattern-only correlation. MI matrix will be marked degraded."
                    )
                    degraded_flag = True

                # Use pattern-only correlation (simplified, lower quality)
                mi_value = mutual_information_simple_correlation(pairs_i, pairs_j)
                mi_matrix[i][j] = mi_value

    return mi_matrix


def mutual_information_simple_correlation(pairs_i, pairs_j):
    """
    Simplified measure of correlation between signal predictions.
    
    Args:
        pairs_i: [(direction, confidence), ...]
        pairs_j: [(direction, confidence), ...]
              
    Returns:
        Correlation coefficient as proxy to MI (0-1 range)
    """
    if len(pairs_i) < 10 or len(pairs_j) < 10:
        return 0.0
    
    # Convert to directional agreement: same direction weighted by confidence
    agreements = []
    for (dir_i, conf_i), (dir_j, conf_j) in zip(pairs_i, pairs_j):
        # Agreement = sign based (both up, both down) multiplied by confidence product
        direction_agree = 1.0 if dir_i == dir_j else -1.0
        confidence_product = conf_i * conf_j
        agreements.append(direction_agree * confidence_product)
    
    # Compute correlation-like measure
    mean_agreement = sum(agreements) / len(agreements)
    # Take absolute to get how strongly correlated they are, regardless of direction of correlation
    correlation_strength = abs(mean_agreement)
    
    # Cap to reasonable range based on confidence-weighted agreement
    return min(correlation_strength, 1.0)


def unique_information_fraction(mi_matrix, signal_idx, accuracy_estimates):
    """
    Compute unique information fraction for a signal based on its correlation with others.
    
    Args:
        mi_matrix: mutual information matrix [n_signals x n_signals]
        signal_idx: index of current signal
        accuracy_estimates: list of accuracy estimates for all signals
        
    Returns:
        Unique information fraction (0.1 - 1.0, clamped)
    """
    n = len(mi_matrix) if mi_matrix else 0
    if n <= signal_idx:
        return 0.1  # Very low unique information if not enough data
        
    if n == 1:
        return 1.0  # Only signal, fully unique
        
    # Sum up correlation with other signals
    total_correlation = sum(mi_matrix[signal_idx][j] for j in range(n) if j != signal_idx)
    
    # Normalize by potential maximum correlation - in our correlation proxy system, this is approximately the number of other signals
    max_possible_correlation = (n - 1)  # max if perfectly correlated with all others
    
    # Unique fraction = 1 - (normalized correlation)
    unique_frac = 1.0 - (total_correlation / max_possible_correlation) if max_possible_correlation > 0 else 0.0
    return max(0.1, min(1.0, unique_frac))  # Clamp to [0.1, 1.0]


def compute_weights_from_significance(mi_matrix, accuracies, signal_names):
    """
    Compute final weights using unique information fraction and accuracy.
    
    Args:
        mi_matrix: mutual information matrix
        accuracies: list of accuracy for each signal
        signal_names: list of signal names (for debug)
        
    Returns:
        List of normalized weights [0, 1] that sum to 1
    """
    n_signals = len(signal_names)
    weights = []
    
    for i in range(n_signals):
        acc = max(0.51, min(0.95, accuracies[i])) if i < len(accuracies) else 0.6
        accuracy_logodds = math.log(acc / (1 - acc)) if acc < 0.95 else math.log(0.95 / 0.05)
        
        unique_frac = unique_information_fraction(mi_matrix, i, accuracies)
        raw_weight = unique_frac * accuracy_logodds
        
        weights.append(raw_weight)
    
    # Normalize to sum to 1
    total_weight = sum(weights) if weights else 1.0
    
    if total_weight == 0:
        # Uniform fallback if all weights are 0
        return [1.0 / len(signal_names)] if signal_names else [1.0] if n_signals > 0 else []
    
    return [w / total_weight for w in weights]


def dempster_shafer_conflict(signals_and_confs):
    """
    Compute Dempster's conflict mass K between signals using proper evidence theory.
    
    Each signal is treated as a mass function over {UP, DOWN, UNCERTAIN}:
    - m(UP) = calibrated_confidence if direction='up' else (1 - calibrated_confidence) * 0.5
    - m(DOWN) = calibrated_confidence if direction='down' else (1 - calibrated_confidence) * 0.5
    - m(UNCERTAIN) = 1 - m(UP) - m(DOWN)
    
    Conflict K = Σ_{A∩B=∅} m1(A) * m2(B) — sum over all pairs where evidence
    assigns mass to disjoint hypotheses.
    
    High K means evidence vectors genuinely conflict in hypothesis space,
    not just low confidence.
    
    Args:
        signals_and_confs: [(direction, calibrated_confidence), ...]
        
    Returns:
        Conflict mass K (0.0 to 1.0) - higher = more genuine disagreement
    """
    if len(signals_and_confs) < 2:
        return 0.0
    
    # Build mass functions for each signal
    mass_functions = []
    for direction, conf in signals_and_confs:
        conf = max(0.01, min(0.99, conf))
        if direction == 'up':
            m_up = conf
            m_down = (1 - conf) * 0.3  # Small residual mass on opposite
        else:  # direction == 'down'
            m_down = conf
            m_up = (1 - conf) * 0.3
        m_uncertain = max(0.0, 1.0 - m_up - m_down)
        mass_functions.append({'up': m_up, 'down': m_down, 'uncertain': m_uncertain})
    
    # Compute pairwise conflict: K_ij = Σ_{A∩B=∅} m_i(A) * m_j(B)
    # Disjoint pairs: (up, down), (down, up)
    # Non-disjoint: (up, up), (down, down), (up, uncertain), (down, uncertain), (uncertain, *)
    n = len(mass_functions)
    total_conflict = 0.0
    pair_count = 0
    
    for i in range(n):
        for j in range(i + 1, n):
            mi = mass_functions[i]
            mj = mass_functions[j]
            
            # Conflict = mass on disjoint hypotheses
            # up_i ∩ down_j = ∅, down_i ∩ up_j = ∅
            pair_conflict = (mi['up'] * mj['down']) + (mi['down'] * mj['up'])
            
            total_conflict += pair_conflict
            pair_count += 1
    
    if pair_count == 0:
        return 0.0
    
    # Average pairwise conflict, normalized to [0, 1]
    avg_conflict = total_conflict / pair_count
    
    # Also factor in overall disagreement level
    ups = sum(1 for direction, _ in signals_and_confs if direction == 'up')
    downs = len(signals_and_confs) - ups
    direction_split = 4.0 * (ups / len(signals_and_confs)) * (downs / len(signals_and_confs))
    
    # Combine evidence-space conflict with direction disagreement
    # Weight evidence-space conflict more heavily as it captures confidence-weighted disagreement
    combined_conflict = 0.7 * avg_conflict + 0.3 * direction_split
    
    return min(1.0, max(0.0, combined_conflict))


def apply_conflict_modulation(final_prob, conflict_mass, method='kennedy'):
    """
    Apply conflict-based adjustment to final probability.
    
    Args:
        final_prob: Final probability from ensemble
        conflict_mass: K from DS conflict computation
        method: 'kennedy' or 'traditional' - different approaches
    
    Returns:
        Adjusted final probability (0.0 to 1.0), or None if conflict too high
    """
    if conflict_mass >= 0.95:
        return 0.5  # Suppressed to 50% only if extreme conflict
    elif conflict_mass >= 0.8:
        # Suppress towards 0.5
        suppression_factor = 1.0 - ((conflict_mass - 0.8) / 0.15) * 0.5
        distance_from_half = final_prob - 0.5
        adjusted_distance = distance_from_half * suppression_factor
        return 0.5 + adjusted_distance
    elif conflict_mass < 0.3:
        # Amplify slightly to reflect high coherence
        distance_from_half = final_prob - 0.5
        amplified_distance = distance_from_half * 1.1 # Amplify by 10%
        # Make sure we stay in bounds
        amplified_prob = 0.5 + amplified_distance
        return max(0.01, min(0.99, amplified_prob))
    else:
        # No conflict, return as-is
        return final_prob


class SignalFusionEngine:
    """
    Orchestrate the 4-layer fusion stack:
    0: Signal conditioning (with isotonic regression calibration)
    1: MI-based decorrelation weighting
    2: Log-odds linear opinion pool
    3: Dempster-Shafer conflict detection
    """
    def __init__(self, signal_names, city_codes):
        self.signal_names = signal_names
        self.city_codes = city_codes
        
        # Layer 0: Calibration pipeline
        self.calibration_pipeline = CalibrationPipeline(signal_names, city_codes)
        
        # Layer 1-2: Weights and MI matrix
        self.mi_matrix = None
        self.current_weights = None
        
        # Statistics about calibration for fallback
        self.signal_accuracies = {name: 0.6 for name in signal_names}  # Default estimates

    # ===================================================================
    # CONFIDENCE & AGREEMENT SPEC IMPLEMENTATION (2026-07-06)
    # ===================================================================

    def compute_signal_agreement_score(self, calibrated_signals, fused_direction, mi_weights=None):
        """
        Compute Signal Agreement Score (SAS).

        Formula: SAS = Σ(wᵢ × aᵢ × cᵢ) / Σ(wᵢ)

        where:
          wᵢ = mutual-information-based weight
          aᵢ = agreement indicator (1.0 if signal i agrees with ensemble direction, 0.0 otherwise)
          cᵢ = calibrated confidence quality (clamped to [0.5, 1.0])

        Handles "5 strong agree + 2 weak disagree" vs "5 weak agree + 2 strong disagree"
        correctly because strong-agree signals contribute w×1×high_conf while weak-disagree
        signals contribute w×0×conf = 0, and weak-agree signals contribute w×1×low_conf.

        Range: [0, 1] where 0=full disagreement, 1=full agreement.
        Runs in parallel with LLOP — does NOT replace it.

        Args:
            calibrated_signals: list of (direction, prob_direction, calibrated_prob)
            fused_direction: 'up' or 'down' from LLOP calculation
            mi_weights: optional list of weights; defaults to equal weights

        Returns:
            Signal Agreement Score (0.0 to 1.0)
        """
        n_signals = len(calibrated_signals)
        if n_signals == 0:
            return 0.0

        if mi_weights is None:
            mi_weights = [1.0 / n_signals] * n_signals

        # Pad/truncate weights to match signal count
        if len(mi_weights) < n_signals:
            mi_weights = list(mi_weights) + [1.0 / n_signals] * (n_signals - len(mi_weights))

        total_agreement = 0.0
        total_weight = 0.0

        for i, (direction, prob_direction, calibrated_prob) in enumerate(calibrated_signals):
            # Agreement indicator: 1.0 if signal direction matches fused direction
            agrees = 1.0 if direction == fused_direction else 0.0

            # Confidence quality: clamp calibrated prob to [0.5, 1.0]
            # For 'up' signals, prob_direction is the calibrated probability of up
            # For 'down' signals, prob_direction is 1 - calibrated_prob
            conf_quality = max(0.5, min(1.0, calibrated_prob))

            weight = mi_weights[i]

            contribution = weight * agrees * conf_quality
            total_agreement += contribution
            total_weight += weight

        return total_agreement / total_weight if total_weight > 0 else 0.0

    def compute_local_ece(self, station_code, signal_history, n_bins=10):
        """
        Compute Expected Calibration Error (ECE) for a station's signal history.

        Uses 10-bin equal-width decomposition. ECE = Σ(Nk/N × |mk - uk|)
        where mk is observed frequency in bin k, uk is bin center forecast.

        Args:
            station_code: station/city code
            signal_history: list of dicts with 'forecast_prob' and 'was_correct' keys,
                           or list of (forecast_prob, was_correct) tuples
            n_bins: number of equal-width bins (default 10)

        Returns:
            ECE score (0.0 = perfect calibration, 1.0 = worst)
        """
        if not signal_history:
            return 1.0  # Maximum ECE if no data — conservative

        # Normalize input to (forecast_prob, was_correct) tuples
        pairs = []
        for entry in signal_history:
            if isinstance(entry, dict):
                fp = entry.get('forecast_prob', entry.get('prob', 0.5))
                wc = entry.get('was_correct', entry.get('correct', False))
            elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
                fp, wc = entry[0], entry[1]
            else:
                continue
            pairs.append((float(fp), bool(wc)))

        if not pairs:
            return 1.0

        total = len(pairs)
        ece = 0.0
        bin_edges = [i / n_bins for i in range(n_bins + 1)]

        for i in range(n_bins):
            lo = bin_edges[i]
            hi = bin_edges[i + 1]
            bin_pairs = [(fp, wc) for fp, wc in pairs if lo <= fp < hi]

            if not bin_pairs:
                continue

            n_k = len(bin_pairs)
            bin_center = (lo + hi) / 2.0
            observed_freq = sum(1.0 for _, wc in bin_pairs if wc) / n_k

            ece += (n_k / total) * abs(observed_freq - bin_center)

        return ece

    def compute_conviction_score(self, llop_prob, signal_agreement_score,
                                 calibration_data, signal_history, station_code):
        """
        Compute conviction score that gates trade entry.

        Formula: Conviction = |LLOP_prob - 0.5| × 2 × (0.3 + SAS) × (1 - ECE) × tanh(n/50)

        Components:
          - LLOP_prob_offset = |LLOP_fused_prob - 0.5| * 2.0  (edge magnitude, 0-1 scale)
          - Agreement_factor = 0.3 + SAS  (biased upward: even low agreement gives 0.3)
          - Calibration_quality = max(0.5, min(1.0, 1.0 - ECE))
          - Sample_size_factor = min(1.1, tanh(recent_samples / 50.0))

        Threshold: below 0.20 = reject trade (returns None)

        Args:
            llop_prob: fused probability from LLOP
            signal_agreement_score: SAS value
            calibration_data: unused (reserved for future)
            signal_history: list of historical observations
            station_code: station for ECE lookup

        Returns:
            conviction_score (float 0.0-1.0) or None if trade should be rejected
        """
        # Get ECE for this signal on this station
        ece_score = self.compute_local_ece(station_code, signal_history)

        # LLOP probability offset (edge strength)
        llop_offset = abs(llop_prob - 0.5) * 2.0  # Now 0.0-1.0 scale

        # Agreement factor (bias upward for stability)
        agreement_factor = 0.3 + (signal_agreement_score * 1.0)

        # Calibration quality
        calibration_quality = max(0.5, min(1.0, 1.0 - ece_score))

        # Recent sample size (last 30 days minimum 20 obs for stability)
        recent_samples = len(signal_history) if signal_history else 0
        sample_factor = min(1.1, math.tanh(recent_samples / 50.0))

        conviction_score = llop_offset * agreement_factor * calibration_quality * sample_factor

        # Minimum threshold for trade entry
        if conviction_score < 0.20:
            return None  # Reject trade

        return conviction_score

    def should_take_trade(self, llop_prob, signal_agreement_score,
                         calibration_data, signal_history, station_code):
        """
        Decision function: should we trade based on conviction score?

        Args:
            llop_prob: fused probability from LLOP
            signal_agreement_score: SAS value
            calibration_data: reserved for future
            signal_history: list of historical observations
            station_code: station for ECE lookup

        Returns:
            (should_trade: bool, reason: str, conviction_score: float or None)
        """
        conviction = self.compute_conviction_score(
            llop_prob, signal_agreement_score,
            calibration_data, signal_history, station_code
        )

        if conviction is None:
            return False, "Low Conviction Score", None

        # Additional check: minimum edge threshold
        min_edge_threshold = 0.05  # 5% edge required
        if abs(llop_prob - 0.5) < min_edge_threshold:
            return False, "Insufficient Edge", conviction

        return True, "Approved by Conviction Score", conviction

    def decompose_brier_score(self, forecasts, outcomes):
        """
        Decompose Brier Score into Reliability, Resolution, and Uncertainty.

        Brier Score = Reliability - Resolution + Uncertainty

        where:
          - Reliability = Σ[Nk/N × (mk - uk)²] — how far forecasts deviate from observed frequency
          - Resolution = Σ[Nk/N × (uk - mean_o)²] — distinguishability of outcomes
          - Uncertainty = mean_o × (1 - mean_o) — inherent uncertainty in outcomes

        Uses 10-bin equal-width decomposition as specified.

        Args:
            forecasts: list of forecast probabilities (0.0-1.0)
            outcomes: list of actual outcomes (0 or 1)

        Returns:
            (brier, reliability, resolution, uncertainty)
        """
        if not forecasts or not outcomes:
            return 1.0, 0.0, 0.0, 0.0

        n = len(forecasts)
        if n != len(outcomes):
            raise ValueError(f"forecasts ({n}) and outcomes ({len(outcomes)}) must have same length")

        mean_outcome = sum(outcomes) / n
        uncertainty = mean_outcome * (1.0 - mean_outcome)

        reliability = 0.0
        resolution = 0.0

        bins = [(i * 0.1, (i + 1) * 0.1) for i in range(10)]

        for bin_start, bin_end in bins:
            bin_forecasts = []
            bin_outcomes = []
            for i, f in enumerate(forecasts):
                if bin_start <= f < bin_end:
                    bin_forecasts.append(f)
                    bin_outcomes.append(outcomes[i])

            if bin_forecasts:
                n_k = len(bin_forecasts)
                mk_hat = sum(bin_outcomes) / n_k if bin_outcomes else mean_outcome
                uk = (bin_start + bin_end) / 2.0  # bin center forecast

                reliability += (n_k / n) * ((mk_hat - uk) ** 2)
                resolution += (n_k / n) * ((uk - mean_outcome) ** 2)

        brier = reliability - resolution + uncertainty

        return brier, reliability, resolution, uncertainty

    def adjust_confidence_by_regime(self, raw_confidence, regime_characteristics):
        """
        Adjust confidence based on current regime conditions.

        Regime factors:
          - temperature_volatility: float (0-1+), >0.8 = high volatility
          - season_transition_period: bool, True if spring/fall
          - weather_pattern: str, 'frontal_instability' reduces confidence

        Args:
            raw_confidence: confidence value to adjust
            regime_characteristics: dict with regime data

        Returns:
            Adjusted confidence (may be higher or lower than raw)
        """
        adjustment_factor = 1.0

        # Volatility adjustment
        temp_vol = regime_characteristics.get('temperature_volatility', 0.5)
        if temp_vol > 0.8:
            adjustment_factor *= 0.8  # Downgrade confidence during highly variable periods
        elif temp_vol < 0.2:
            adjustment_factor *= 1.1  # Boost confidence during stable periods

        # Seasonal adjustment
        if regime_characteristics.get('season_transition_period', False):  # Spring/Fall
            adjustment_factor *= 0.9  # Reduce during unstable transition seasons

        # Atmospheric patterns
        if regime_characteristics.get('weather_pattern') == 'frontal_instability':
            adjustment_factor *= 0.85

        return raw_confidence * adjustment_factor

    def update_signal_weights_bayesian(self, historical_performance, recent_days=30):
        """
        Bayesian updating of signal weights based on recent relative performance.

        Uses Beta-binomial posterior: posterior_mean = (successes + 2) / (successes + failures + 3)
        with prior Beta(2, 1) — assumes some inherent predictive ability.

        Scales MI-based weights by Bayesian quality relative to average.
        Preserves rank-order structure from MI.

        Args:
            historical_performance: dict {signal_name: [(date, was_correct), ...]}
            recent_days: window for recent performance lookup

        Returns:
            List of renormalized weights
        """
        bayesian_qualities = {}

        # Compute cutoff date for recent window
        from datetime import datetime, timedelta, timezone
        cutoff = datetime.now(timezone.utc) - timedelta(days=recent_days)

        for signal_name in self.signal_names:
            perf_data = historical_performance.get(signal_name, [])

            # Filter to recent window
            recent_data = []
            for entry in perf_data:
                if isinstance(entry, dict):
                    d = entry.get('date')
                    wc = entry.get('was_correct', False)
                elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
                    d, wc = entry[0], entry[1]
                else:
                    continue

                # Check if within recent window
                try:
                    if isinstance(d, str):
                        entry_date = datetime.fromisoformat(d.replace('Z', '+00:00'))
                    else:
                        entry_date = d
                    if entry_date >= cutoff:
                        recent_data.append(wc)
                except (ValueError, TypeError):
                    # If date parsing fails, include anyway
                    recent_data.append(wc)

            if len(recent_data) >= 10:  # Minimum samples for meaningful update
                successes = sum(1 for wc in recent_data if wc)
                failures = len(recent_data) - successes

                # Beta-binomial posterior mean with Beta(2,1) prior
                posterior_mean = (successes + 2) / (successes + failures + 3)
                bayesian_qualities[signal_name] = posterior_mean
            else:
                # Not enough recent data, keep default
                bayesian_qualities[signal_name] = 0.6  # assumed baseline

        # Get current MI-based weights (or equal if not computed yet)
        base_weights = self.current_weights if self.current_weights else \
                       [1.0 / len(self.signal_names)] * len(self.signal_names)

        # Compute average quality for scaling
        avg_quality = (sum(bayesian_qualities.values()) / len(bayesian_qualities)
                       if bayesian_qualities else 0.6)

        # Scale weights by quality relative to average
        scaled_weights = []
        for i, signal_name in enumerate(self.signal_names):
            bayes_q = bayesian_qualities.get(signal_name, 0.6)
            if avg_quality > 0:
                scaling_factor = bayes_q / avg_quality
            else:
                scaling_factor = 1.0
            new_weight = base_weights[i] * scaling_factor
            scaled_weights.append(new_weight)

        # Renormalize
        total = sum(scaled_weights) if scaled_weights else 1.0
        if total > 0:
            renorm_weights = [w / total for w in scaled_weights]
        else:
            n = len(self.signal_names) if self.signal_names else 1
            renorm_weights = [1.0 / n] * n

        # Update current weights
        self.current_weights = renorm_weights
        return renorm_weights

    def enhance_with_agreement_and_conviction(self, signals, city_code):
        """
        Main pathway adding all new metrics (SAS + Conviction).

        Steps:
          1. Original LLOP fusion (existing fuse_signals method)
          2. Collect calibrated signals for agreement analysis
          3. Compute MI-based weights
          4. Compute Signal Agreement Score
          5. Get recent history for calibration quality metrics
          6. Compute Conviction Score

        Args:
            signals: List of (signal_name, direction, raw_confidence)
            city_code: City code for calibration lookup

        Returns:
            (fused_direction, fused_prob, signal_agreement, conviction_score, raw_llop_prob)
            or (None, 0.0, 0.0, 0.0, 0.0) if DS conflict rejects
        """
        # Step 1: Original LLOP fusion
        fused_direction, fused_prob, _ = self.fuse_signals(signals, city_code, use_ds_conflict=True)

        if fused_direction is None:  # DS conflict rejected trade
            return None, 0.0, 0.0, 0.0, 0.0

        # Step 2: Collect calibrated signals for agreement analysis
        calibrated_signals = []
        for signal_name, direction, raw_conf in signals:
            calibrated_prob = self.calibration_pipeline.calibrate(signal_name, city_code, raw_conf)
            if direction == 'up':
                prob_direction = calibrated_prob
            else:  # direction == 'down'
                prob_direction = 1.0 - calibrated_prob
            calibrated_signals.append((direction, prob_direction, calibrated_prob))

        # Step 3: Compute MI-based weights (use current_weights or equal)
        n_signals = len(calibrated_signals)
        if self.current_weights and len(self.current_weights) == len(self.signal_names):
            # Map weights from signal_names to the provided signals
            mi_weights = []
            for signal_name, _, _ in signals:
                if signal_name in self.signal_names:
                    idx = self.signal_names.index(signal_name)
                    mi_weights.append(self.current_weights[idx])
                else:
                    mi_weights.append(1.0 / n_signals)
        else:
            mi_weights = [1.0 / n_signals] * n_signals

        # Step 4: Compute Signal Agreement Score
        signal_agreement = self.compute_signal_agreement_score(
            calibrated_signals, fused_direction, mi_weights
        )

        # Step 5: Get recent history for calibration quality metrics
        recent_history = self.get_recent_calibration_performance(signals, city_code)

        # Step 6: Compute Conviction Score
        conviction_score = self.compute_conviction_score(
            fused_prob, signal_agreement,
            None,  # calibration_data not fully implemented yet but planned
            recent_history,
            city_code
        )

        return fused_direction, fused_prob, signal_agreement, conviction_score, fused_prob

    def get_recent_calibration_performance(self, signals, city_code, window_days=30):
        """
        Retrieve recent signal vs. outcome data for calibration quality assessment.

        Returns a list of (forecast_prob, was_correct) pairs from the calibration
        pipeline's history for this city.

        Args:
            signals: list of (signal_name, direction, raw_confidence) tuples
            city_code: city code
            window_days: how far back to look

        Returns:
            list of (forecast_prob, was_correct) tuples
        """
        result = []
        from datetime import datetime, timedelta, timezone
        cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)

        for signal_name, _, raw_conf in signals:
            key = (signal_name, city_code)
            history = self.calibration_pipeline.history.get(key, [])
            for raw_c, was_correct in history:
                # Get calibrated probability for this forecast
                cal_prob = self.calibration_pipeline.calibrate(signal_name, city_code, raw_c)
                result.append((cal_prob, was_correct))

        return result

    # ===================================================================
    # END CONFIDENCE & AGREEMENT SPEC IMPLEMENTATION
    # ===================================================================

    def fit_calibration(self, all_station_signal_history):
        """
        Train the calibration pipeline on historical data.
        
        Args:
            all_station_signal_history: dict of {(signal_name, station): [(raw_conf, correct), ...]}
        """
        _logger.info(f"Training calibration pipeline for {len(self.signal_names)} signals, {len(self.city_codes)} cities...")
        
        for (signal, station), history in all_station_signal_history.items():
            for raw_conf, was_correct in history:
                self.calibration_pipeline.update(signal, station, raw_conf, was_correct)
        
        _logger.info(f"Fitting calibration calibrators...")
        self.calibration_pipeline.refit()
        _logger.info("Calibration training complete.")

    def compute_fusion_weights(self, mi_matrix=None, recalibrate_accuracies=False):
        """
        Compute mutual information matrix and final fusion weights.
        """
        if mi_matrix is None:
            _logger.info("Computing mutual information matrix and fusion weights...")
            # We'd need the signal history here, for now use default approach
            # This function assumes weights have been computed elsewhere or use defaults
        
        # Calculate weights using MI-based decorrelation
        # For now just return equally distributed initial weights as placeholder
        # until MI data is provided
        n = len(self.signal_names)
        if n == 0:
            return []
        return [1.0 / n] * n

    def fuse_signals(self, signals, city_code, use_ds_conflict=True):
        """
        Main fusion method integrating all 4 layers.
        
        Args:
            signals: List of (signal_name, direction, raw_confidence)
            city_code: City code for calibration lookup
            use_ds_conflict: Whether to apply Dempster-Shafer conflict detection
            
        Returns:
            (direction, probability, confidence) or (None, 0.0, 0.0) if conflict too high
        """
        if not signals:
            return None, 0.0, 0.0
        
        # Layer 0: Apply calibration to convert raw confidences to probabilities of correctness
        calibrated_signals = []
        for signal_name, direction, raw_conf in signals:
            calibrated_prob = self.calibration_pipeline.calibrate(signal_name, city_code, raw_conf)
            
            # Convert P(correct) to directional probability
            if direction == 'up':
                prob_direction = calibrated_prob
            else:  # direction == 'down'
                prob_direction = 1.0 - calibrated_prob # Probability of down
                
            calibrated_signals.append((direction, prob_direction, calibrated_prob))
        
        # If no calibrated signals, return defaults
        if not calibrated_signals:
            return None, 0.0, 0.0
            
        # Layer 1 & 2: Compute weights and perform Log-Odds Linear Opinion Pooling
        # For now, use equal or estimated weights since we need full training data for MI
        n_signals = len(calibrated_signals)
        equal_weights = [1.0 / n_signals if n_signals > 0 else 1.0 for _ in calibrated_signals]
        
        # Calculate log odds for each signal's directional probability
        log_odds = []
        for direction, prob, _ in calibrated_signals:
            # Convert probability to log-odds (natural log of odds ratio)
            # For a probability p: log-odds = ln(p/(1-p))
            prob = max(0.01, min(0.99, prob))  # Clamp to avoid inf
            odds = prob / (1.0 - prob)
            log_odd = math.log(odds)
            log_odds.append(log_odd)
        
        # Weighted log-odds combination (LLOP)
        weighted_log_odds = 0.0
        for w, lo in zip(equal_weights, log_odds):
            weighted_log_odds += w * lo  
        
        # Convert back to probability with sigmoid
        final_prob = expit(weighted_log_odds)  # expit is sigmoid function
        
        # Determine final direction and confidence
        # Confidence = probability of the predicted direction (not doubled distance from 0.5)
        if final_prob >= 0.5:
            final_direction = 'up'
            confidence = final_prob  # P(up)
        else:
            final_direction = 'down'
            confidence = 1.0 - final_prob  # P(down)
            
        # Layer 3: Apply Dempster-Shafer conflict detection
        ds_conflict_signals_input = [(direction, prob) for direction, prob, _ in calibrated_signals]
        
        conflict_k = dempster_shafer_conflict(ds_conflict_signals_input)
        
        # Layer 3.5: Ensemble Diversity Score
        # diversity = 1 - |2*max_vote_share - 1|
        # If all 7 signals vote up: max_vote_share=1.0, diversity=0.0
        # If 4 up, 3 down: max_vote_share=0.57, diversity=0.86
        # Modifier: final_confidence *= (0.75 + 0.25*diversity)
        # This penalizes unanimous votes (they don't add much information)
        if n_signals >= 2:
            up_count = sum(1 for d, _, _ in calibrated_signals if d == 'up')
            down_count = n_signals - up_count
            max_vote_share = max(up_count, down_count) / n_signals
            diversity = 1.0 - abs(2.0 * max_vote_share - 1.0)
            diversity_modifier = 0.75 + 0.25 * diversity
            _logger = logging.getLogger(__name__)
            _logger.debug(
                f"EnsembleDiversity: up={up_count}, down={down_count}, "
                f"max_vote_share={max_vote_share:.3f}, "
                f"diversity={diversity:.3f}, modifier={diversity_modifier:.3f}"
            )
        else:
            diversity_modifier = 1.0  # No diversity adjustment for single signals

        if use_ds_conflict:
            adjusted_prob = apply_conflict_modulation(final_prob, conflict_k)

            # If conflict suppression sets probability to 0.5, we interpret as no trade signal
            if abs(adjusted_prob - 0.5) < 0.005:  # Very close to chance — raised threshold slightly
                return None, 0.0, 0.0

            # Recompute confidence after adjustment — use probability directly
            if adjusted_prob >= 0.5:
                adjusted_direction = 'up'
                adjusted_confidence = adjusted_prob
            else:
                adjusted_direction = 'down'
                adjusted_confidence = 1.0 - adjusted_prob

            # Apply diversity score modifier to confidence
            adjusted_confidence = adjusted_confidence * diversity_modifier

            return adjusted_direction, adjusted_prob, adjusted_confidence
        else:
            # Apply diversity score modifier to confidence
            confidence = confidence * diversity_modifier
            return final_direction, final_prob, confidence


class TimeDecaySignalManager:
    """
    P1.5: Time-Decay Weighted Signal Reliability Manager
    
    Tracks per-signal per-city performance with exponential forgetting.
    Adjusts signal confidence based on recent reliability.
    
    Key formulas:
    - reliability = exponentially weighted recent accuracy (decay_factor=0.9, window=30 days)
    - adjusted_conf = sqrt(raw_conf * reliability)
    - LOP weights modified by reliability
    """
    def __init__(self, signal_names, city_codes, decay_factor=0.9, window=30):
        self.signal_names = signal_names
        self.city_codes = city_codes
        self.decay_factor = decay_factor
        self.window = window
        
        # Per-signal per-city history: {(signal, city): [(date, correct_bool), ...]}
        self.history = defaultdict(list)
        
        # Cached reliability scores: {(signal, city): reliability_score}
        self.reliability_cache = {}
        
        # Per-signal per-city weighted accuracy for LOP
        self.signal_weights = defaultdict(lambda: defaultdict(float))
    
    def update(self, signal_name, city_code, date, correct):
        """Record a prediction outcome."""
        self.history[(signal_name, city_code)].append((date, correct))
        # Invalidate cache for this pair
        self.reliability_cache.pop((signal_name, city_code), None)
    
    def compute_reliability(self, signal_name, city_code, current_date=None):
        """
        Compute exponentially weighted recent accuracy.
        
        reliability = sum(decay^(t-i) * correct_i) / sum(decay^(t-i))
        where t is the current date index and i ranges over the window.
        """
        key = (signal_name, city_code)
        if key in self.reliability_cache:
            return self.reliability_cache[key]
        
        history = self.history.get(key, [])
        if not history:
            self.reliability_cache[key] = 0.5  # Default: no information
            return 0.5
        
        # Sort by date and take last `window` entries
        history_sorted = sorted(history, key=lambda x: x[0])
        recent = history_sorted[-self.window:]
        
        if not recent:
            self.reliability_cache[key] = 0.5
            return 0.5
        
        # Exponential weighting: most recent = highest weight
        n = len(recent)
        weighted_sum = 0.0
        weight_sum = 0.0
        for i, (date, correct) in enumerate(recent):
            # Weight = decay_factor^(n-1-i), so most recent (i=n-1) gets weight=1
            w = self.decay_factor ** (n - 1 - i)
            weighted_sum += w * (1.0 if correct else 0.0)
            weight_sum += w
        
        reliability = weighted_sum / weight_sum if weight_sum > 0 else 0.5
        
        # Clamp to reasonable range
        reliability = max(0.1, min(1.0, reliability))
        
        self.reliability_cache[key] = reliability
        return reliability
    
    def adjust_confidence(self, signal_name, city_code, raw_conf):
        """
        Adjust signal confidence based on reliability.
        
        adjusted_conf = sqrt(raw_conf * reliability)
        
        This geometric mean penalizes overconfident signals with poor track records
        while preserving well-calibrated signals.
        """
        reliability = self.compute_reliability(signal_name, city_code)
        adjusted = math.sqrt(max(0.0, raw_conf) * reliability)
        return adjusted
    
    def get_lop_weight(self, signal_name, city_code):
        """
        Get reliability-weighted LOP weight for a signal.
        
        Signals with higher recent reliability get proportionally more weight
        in the log-odds linear opinion pool.
        """
        reliability = self.compute_reliability(signal_name, city_code)
        # Convert reliability to weight: log-odds of reliability
        # This ensures that 50% reliability → 0 weight, >50% → positive weight
        if reliability <= 0.01:
            return 0.0
        return math.log(reliability / (1.0 - reliability)) if reliability < 0.99 else 4.6  # log(99)
    
    def get_all_reliabilities(self, city_code):
        """Get reliability scores for all signals at a city."""
        return {sig: self.compute_reliability(sig, city_code) for sig in self.signal_names}
    
    def get_reliability_report(self, city_code):
        """Generate a human-readable reliability report for a city."""
        report = []
        for sig in self.signal_names:
            r = self.compute_reliability(sig, city_code)
            n = len(self.history.get((sig, city_code), []))
            report.append(f"  {sig}: reliability={r:.3f} ({n} observations)")
        return "\n".join(report)


def demonstrate_fusion_stack():
    """
    Demonstrate the complete 4-layer fusion stack with toy data.
    """
    print("DEMONSTRATION: 4-Layer Fusion Stack")
    print("=" * 60)
    
    # Example from Expert 2 report
    signal_names = ['reversion', 'gaussian_v2', 'regime', 'pressure']  
    city_codes = ['KNYC', 'KLAX', 'KATL']
    
    # Create fusion engine
    fusion = SignalFusionEngine(signal_names, city_codes)
    
    # Simulated history to train calibration
    history = {
        ('reversion', 'KNYC'): [(0.75, True), (0.82, True), (0.68, False), (0.77, True)],
        ('reversion', 'KLAX'): [(0.65, False), (0.78, True), (0.85, True), (0.72, True)],
        ('gaussian_v2', 'KNYC'): [(0.70, True), (0.80, True), (0.60, False), (0.75, True)],
        ('gaussian_v2', 'KLAX'): [(0.68, True), (0.75, False), (0.82, True), (0.65, True)],
        # ... add more entries per city-signal combination...
    }
    
    # Add many more to meet MIN_SAMPLES requirement
    for i in range(100):
        sig_idx = i % len(signal_names)
        city_idx = i % len(city_codes)
        signal = signal_names[sig_idx]
        city = city_codes[city_idx]
        raw_conf = 0.55 + (0.3 * (i % 20) / 20)  # Between 0.55 and 0.85
        correct = (i % 3) != 0  # ~66% accuracy
        if (signal, city) not in history:
            history[(signal, city)] = []
        history[(signal, city)].append((raw_conf, correct))
    
    # Train calibration
    fusion.fit_calibration(history)
    
    # Test fusion on example signals
    test_signals = [
        ('reversion', 'up', 0.75),
        ('gaussian_v2', 'up', 0.68),
        ('regime', 'down', 0.80)
    ]
    
    print(f"\nTest Fusion Input:")
    for signal, direction, raw_conf in test_signals:
        print(f"  {signal}: {direction} @ {raw_conf}")
        
    result = fusion.fuse_signals(test_signals, 'KNYC')
    print(f"\nFusion Output: {result}")
    print(f"Result: Direction={result[0]}, Probability={result[1]:.3f}, Confidence={result[2]:.3f}")
    
    print("\nFusion engine initialized and ready for integration with ensemble.")
    return True


if __name__ == "__main__":
    demonstrate_fusion_stack()
>>>>>>> origin/main
