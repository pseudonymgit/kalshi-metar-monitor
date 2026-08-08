#!/usr/bin/env python3
"""
GEFS Paper Trading Cron — Ensemble-based trading using GEFS 31-member.

Pipeline:
  1. Read GEFS ensemble from data/gefs_archive.db
  2. Compute ensemble fraction (members predicting up vs down)
  3. Compute directional confidence from spread
  4. Compare ensemble fraction vs Kalshi market price
  5. Edge = model_prob - market_price - fee_equivalent
  6. If edge > threshold → trade signal
  7. Edge-dependent Kelly sizing
  8. Log to data/paper_trading_dev.db

Fees: kalshi_real (ceil(0.07 × qty × P × (1-P)) per side)
Per-contract binary P&L. No AI/ML.

Usage:
    python3 scripts/gefs_paper_trading_cron.py [--days 7] [--start 2026-07-28]

Output:
    data/paper_trading_dev.db  — SQLite trade database
    data/gefs_paper_trading.log — Timestamped log

Version: v1.0 2026-07-31 (GEFS rewrite)
"""

import json
import math
import os
import sqlite3
import sys
import time
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

# ─── Path setup ─────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(SCRIPT_DIR))


# ─── Load .env for Kalshi API
ENV_FILE = REPO_ROOT / '.env'
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            os.environ.setdefault(k, v)

# ─── Risk Controls ─────────────────────────────────────────────────────────

from core.risk_controls import RiskManager, RiskConfig, TradeResult as RCTradeResult
from core.stop_loss import StopLossMonitor, PRIMARY_DAY_LIMIT
from core.market_cost_model import MARKET_COST_MODEL
from core.station_registry import STATION_LIQUIDITY_TIERS, get_liquidity_tier, STATION_CLUSTERS, get_cluster_for_station

# ─── Bias Correction Module (GR12 Phase A) ─────────────────────────────
from core.ensemble_fraction import (
    load_bias_corrections as _load_bias_table,
    apply_bias_correction as _apply_bias_correction,
    compute_ensemble_fraction as _compute_ensemble_fraction,
)
from core.forecast_confidence_modulator import ForecastConfidenceModulator


# ─── Paths
GEFS_DB = str(REPO_ROOT / "data" / "gefs_archive.db")
NWP_DB = str(REPO_ROOT / "data" / "nwp_forecasts.db")
SETTLEMENTS_DB = str(REPO_ROOT / "data" / "kalshi_settlements.db")
TRADE_DB = str(REPO_ROOT / "data" / "paper_trading_dev.db")
LOG_FILE = str(REPO_ROOT / "data" / "gefs_paper_trading.log")
UHI_BIAS_TABLE = str(REPO_ROOT / "data" / "uhi_bias_table.json")
CALIBRATION_TABLE = str(REPO_ROOT / "data" / "calibration_curves.json")

# ─── UHI bias correction (P1.2) ────────────────────────────────────────────
# Per-station × per-month bias (GEFS_°F - Kalshi_°F), loaded from
# data/uhi_bias_table.json. Positive bias means GEFS overpredicts.
# Corrected GEFS temp = GEFS_°F - bias. Applied before direction computation.
_uhi_bias = None

# ─── Module-level ensemble bias corrections (GR12 Phase A) ────────────────
_BIAS_TABLE = None


def _load_ensemble_bias() -> dict:
    """Load ensemble bias correction table once at module level.
    Falls back to UHI table if ensemble corrections unavailable.
    """
    global _BIAS_TABLE
    if _BIAS_TABLE is not None:
        return _BIAS_TABLE
    try:
        bias_path = str(REPO_ROOT / "data" / "ensemble_fraction_bias_corrections.json")
        _BIAS_TABLE = _load_bias_table(bias_path)
        _LOGGER.info(
            "Loaded ensemble bias corrections: %d stations, %d matched pairs",
            len(_BIAS_TABLE.get("bias_table", {})),
            _BIAS_TABLE.get("matched_pairs", 0),
        )
    except Exception as e:
        _LOGGER.warning(f"Ensemble bias correction table not loaded ({e}); falling back to UHI")
        _BIAS_TABLE = {}
    return _BIAS_TABLE


def _load_uhi_bias():
    global _uhi_bias
    if _uhi_bias is not None:
        return _uhi_bias
    try:
        with open(UHI_BIAS_TABLE) as f:
            table = json.load(f)
        _uhi_bias = {s: {int(k): float(v) for k, v in months.items()}
                     for s, months in table.items()}
        _LOGGER.info("Loaded UHI bias table: %d stations", len(_uhi_bias))
    except Exception as e:
        _LOGGER.warning(f"UHI bias table not loaded ({e}); correction disabled")
        _uhi_bias = {}
    return _uhi_bias

def apply_uhi_correction(station: str, gefs_mean_f: float, target_date: str) -> float:
    """Subtract per-station monthly UHI bias from GEFS temp (°F)."""
    table = _load_uhi_bias()
    if station not in table:
        return gefs_mean_f
    month = int(target_date.split("-")[1])
    bias = table[station].get(month)
    if bias is None:
        return gefs_mean_f
    return gefs_mean_f - bias


# ─── Calibration curves (P1.5) ─────────────────────────────────────────────
# Per-station empirical calibration: maps raw ensemble fraction confidence
# to actual win rate. Replaces overconfident raw confidence with calibrated
# values for position sizing and edge calculation.
_calibration = None

def _load_calibration():
    global _calibration
    if _calibration is not None:
        return _calibration
    try:
        with open(CALIBRATION_TABLE) as f:
            _calibration = json.load(f)
        _LOGGER.info("Loaded calibration curves: %d stations",
                     len([k for k in _calibration if not k.startswith('_')]))
    except Exception as e:
        _LOGGER.warning(f"Calibration table not loaded ({e}); using raw confidence")
        _calibration = {}
    return _calibration

def calibrate_confidence(station: str, raw_confidence: float, target_date: str,
                           pred_direction: str = "up") -> float:
    """
    Map raw ensemble fraction confidence to empirically calibrated win rate.
    
    Uses DIRECTION-SPECIFIC per-station calibration (up/down bins).
    Falls back to direction-specific global calibration, then to raw confidence.
    
    Args:
        station: ICAO station code
        raw_confidence: Raw ensemble fraction confidence (0.50-1.00)
        target_date: Not used for direction-specific lookup (kept for API compat)
        pred_direction: "up" or "down" — which calibration curve to use
    """
    table = _load_calibration()
    if not table:
        return raw_confidence
    
    confidence_bins = [0.50 + i * 0.05 for i in range(11)]
    
    # Find the bin label for this confidence value
    def _find_bin_label(c):
        for i in range(len(confidence_bins) - 1):
            if confidence_bins[i] <= c < confidence_bins[i + 1]:
                return f"{confidence_bins[i]:.2f}-{confidence_bins[i+1]:.2f}"
        return None
    
    bin_label = _find_bin_label(raw_confidence)
    if bin_label is None:
        return raw_confidence
    
    # Direction must be valid
    direction = pred_direction if pred_direction in ("up", "down") else "up"
    
    # 1. Per-station, per-direction calibration
    cal = table.get(station, {})
    dir_cal = cal.get(direction, {})
    dir_bins = dir_cal.get("bins", {})
    bin_data = dir_bins.get(bin_label, {})
    if bin_data.get("win_rate") is not None:
        return bin_data["win_rate"]
    
    # 2. Fallback: global per-direction calibration
    global_cal = table.get("_global", {})
    global_dir = global_cal.get(direction, {})
    global_bins = global_dir.get("bins", {})
    bin_data = global_bins.get(bin_label, {})
    if bin_data.get("win_rate") is not None:
        return bin_data["win_rate"]
    
    # 3. Fallback: raw confidence
    return raw_confidence

# ─── Logging ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
_LOGGER = logging.getLogger("gefs_paper_trading")

# ─── Constants ──
INITIAL_BANKROLL = 10000.0
STATIONS = [
    "KATL", "KAUS", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU", "KLAS",
    "KLAX", "KMDW", "KMIA", "KMSP", "KMSY", "KNYC", "KOKC", "KPHL",
    "KPHX", "KSAT", "KSEA", "KSFO",
]

# Config (Gray Room R14 — GLM 5.2 corrected values)
# Edge threshold raised: circular formula fixed, edge is now real market edge
# Gray Room R14: edge threshold and price bounds kept at pre-sweep values
# until we see the real edge distribution with corrected market prices.
# GLM 5.2 recommended 0.03 / 0.25 / 0.75 — revisit after 7 days of data.
DEFAULT_EDGE_THRESHOLD = 0.02
# Half-Kelly on corrected f* = 0.1746 at baseline p=0.67, M=0.50
DEFAULT_KELLY_FRACTION = 0.50
# Price bounds: GLM 5.2 recommended [0.25, 0.75] — tighter, avoids extremes
DEFAULT_ENTRY_PRICE_MIN = 0.25
DEFAULT_ENTRY_PRICE_MAX = 0.75
# Max contracts: reduced from 500 to 175 per corrected Kelly (GLM 5.2)
DEFAULT_MAX_CONTRACTS = 175
DEFAULT_TEMP_DIFF_MIN = 0.5  # °F — edge_threshold is the real quality gate

# GLM 5.2 epoch-based Kelly schedule (P1.4)
# Cycle tracking: init_cycle column in gefs_archive (default '12Z' for existing rows)
EPOCH_MULTIPLIERS = {"00Z": 0.70, "06Z": 0.85, "12Z": 1.00, "18Z": 0.55}
EPOCH_CONFIDENCE_THRESHOLDS = {"00Z": 0.55, "06Z": 0.58, "12Z": 0.55, "18Z": 0.62}
EPOCH_ENTRY_PRICE_MIN = 0.25
EPOCH_ENTRY_PRICE_MAX = 0.75
EDGE_TIERS = [
    (0.10, 1.00, "strong_edge"),
    (0.06, 0.75, "moderate_edge"),
    (0.03, 0.50, "weak_edge"),
    (0.00, 0.00, "no_trade"),
]
# Cycle tracking: which GEFS init cycle applies to this forecast
# Defaults to '12Z' when archive lacks init_cycle (historical rows)
DEFAULT_EPOCH_CYCLE = "12Z"


def apply_liquidity_adjustments(
    station: str,
    market_type: str,
    position_size: int,
    bankroll: float,
    fill_probability_threshold: float = 0.7
) -> int:
    """Apply liquidity-based position sizing adjustments per Gap 2 design.
    
    Cap position size using market_cost_model.estimate_slippage to ensure
    fill_probability ≥ 0.7. Fall back to STATION_LIQUIDITY_TIERS when
    market data unavailable.
    
    Args:
        station: ICAO station code
        market_type: "high" or "low"
        position_size: Raw Kelly position size
        bankroll: Current bankroll
        fill_probability_threshold: Minimum acceptable fill probability
        
    Returns:
        Adjusted position size capped by liquidity
    """
    ticker = f"KX{market_type.upper()}{station}"
    
    # Try to get market depth snapshot
    try:
        depth_snapshot = MARKET_COST_MODEL.get_market_depth_snapshot(ticker)
        if depth_snapshot:
            # Use binary search to find max contracts with fill_probability ≥ threshold
            max_contracts = position_size
            min_contracts = 1
            
            for _ in range(10):  # Max 10 iterations
                mid = (min_contracts + max_contracts) // 2
                slippage_est = MARKET_COST_MODEL.estimate_slippage(ticker, mid, is_buy=True)
                
                if slippage_est.fill_probability >= fill_probability_threshold:
                    min_contracts = mid
                else:
                    max_contracts = mid
                
                if max_contracts - min_contracts <= 1:
                    break
            
            # Cap position size at fill probability threshold
            capped_size = min_contracts
            _LOGGER.info(f"Liquidity cap: {station} {market_type} → {capped_size} contracts "
                        f"(fill_prob={slippage_est.fill_probability:.3f})")
            return capped_size
    except Exception as e:
        _LOGGER.warning(f"Market depth unavailable for {ticker}: {e}")
    
    # Fallback to STATION_LIQUIDITY_TIERS
    tier = get_liquidity_tier(station)
    tier_max = {"A": 200, "B": 100, "C": 50, "D": 20}
    fallback_cap = tier_max.get(str(tier), 20)
    
    # For LOW markets, apply additional conservative reduction
    if market_type == "low":
        fallback_cap = max(1, int(fallback_cap * 0.7))
    
    capped_size = min(position_size, fallback_cap)
    _LOGGER.info(f"Fallback liquidity cap: {station} {market_type} tier {tier} → {capped_size} contracts")
    return capped_size


# ═══════════════════════════════════════════════════════════════════════════

def get_gefs_forecast(target_date: str) -> Dict[str, dict]:
    """
    Get GEFS ensemble data for a target date across all stations.
    
    Returns {station: {mean, n_members, min, max, step}}
    Only step=24 (next-day forecast).
    """
    conn = sqlite3.connect(GEFS_DB)
    cur = conn.cursor()
    cur.execute("""
        SELECT station, ensemble_mean, ensemble_min, ensemble_max, n_members, member_values, COALESCE(init_cycle, ?) AS init_cycle
        FROM gefs_archive
        WHERE target_date = ? AND step = 24
    """, (DEFAULT_EPOCH_CYCLE, target_date,))
    rows = cur.fetchall()
    conn.close()
    
    import struct
    result = {}
    for r in rows:
        n_members = r[4] or 31
        member_values = r[5]
        # Decode member offsets: int8 offsets from mean in 0.1°C
        if member_values and len(member_values) >= n_members:
            offsets = list(struct.unpack('b' * n_members, member_values[:n_members]))
            member_temps_c = [r[1] + o * 0.1 for o in offsets]
        else:
            member_temps_c = None
        
        result[r[0]] = {
            "mean": r[1],
            "min": r[2],
            "max": r[3],
            "n_members": n_members,
            "member_temps_c": member_temps_c,
            "init_cycle": r[6],
        }
    
    # Fallback: try nwp_forecasts.db if gefs_archive has no data for this date
    if not result:
        _LOGGER.info(f"  GEFS archive empty for {target_date}, trying NWP DB...")
        result = _get_gefs_from_nwp(target_date)
    
    return result


def _get_gefs_from_nwp(target_date: str) -> Dict[str, dict]:
    """Fallback: get GEFS ensemble data from nwp_forecasts.db gefs_ens."""
    try:
        conn = sqlite3.connect(NWP_DB)
        cur = conn.cursor()
        cur.execute("""
            SELECT station, variable, value
            FROM nwp_forecasts
            WHERE model='gefs_ens' AND target_date = ?
        """, (target_date,))
        rows = cur.fetchall()
        conn.close()
        
        stations = {}
        for r in rows:
            stn = r[0]
            if stn not in stations:
                stations[stn] = []
            stations[stn].append(r[2])
        
        result = {}
        for stn, temps in stations.items():
            if len(temps) < 3:
                continue
            mean_c = sum(temps) / len(temps)
            result[stn] = {
                "mean": mean_c,
                "min": min(temps),
                "max": max(temps),
                "n_members": len(temps),
                "member_temps_c": temps,
                "init_cycle": "12Z",
                "source": "nwp",
            }
        
        if result:
            _LOGGER.info(f"  NWP fallback: {len(result)} stations for {target_date}")
        return result
    except Exception as e:
        _LOGGER.warning(f"NWP fallback failed: {e}")
        return {}


def get_prev_temp(target_date: str, lookback: int = 5) -> Dict[str, Optional[float]]:
    """
    Get the most recent available temperature for each station from GEFS archive.
    Uses the GEFS data as temperature proxy (mean values from nearest available date).
    """
    conn = sqlite3.connect(GEFS_DB)
    cur = conn.cursor()
    # Get the last available temperature before target_date
    cur.execute("""
        SELECT station, ensemble_mean
        FROM gefs_archive
        WHERE target_date < ? AND step = 24 AND ensemble_mean IS NOT NULL
        GROUP BY station
        HAVING target_date = MAX(target_date)
    """, (target_date,))
    rows = cur.fetchall()
    conn.close()
    return {r[0]: r[1] for r in rows}


def get_kalshi_market_price(event_ticker: str) -> Optional[float]:
    """
    Get current market price for a Kalshi event ticker.
    
    In live mode: fetches from Kalshi API via kalshi_monitor.
    In backtest mode: returns None (backfill uses settlement data).
    
    This replaces the old circular formula market_price = 0.5 + confidence * 0.4
    which was deriving artificial prices from the signal itself.
    """
    try:
        from core.kalshi_monitor import _kalshi_get
        path = f"/markets?series_ticker={event_ticker}&limit=1&status=open"
        resp = _kalshi_get(path)
        if resp and "markets" in resp and len(resp["markets"]) > 0:
            m = resp["markets"][0]
            # Use the mid-price: (yes_bid + yes_ask) / 2 or last_price
            yes_bid = m.get("yes_bid", 0)
            yes_ask = m.get("yes_ask", 1)
            if yes_bid > 0 and yes_ask < 1:
                return (yes_bid + yes_ask) / 2.0
            return m.get("last_price", None)
    except ValueError as e:
        if "not configured" in str(e):
            pass  # Kalshi API not configured — expected in backtest mode
        else:
            _LOGGER.warning(f"Kalshi API error for {event_ticker}: {e}")
    except Exception as e:
        _LOGGER.warning(f"Kalshi API price fetch failed for {event_ticker}: {e}")
    return None  # Fallback: caller uses 0.50


# ═══════════════════════════════════════════════════════════════════════════
# Trading Logic
# ═══════════════════════════════════════════════════════════════════════════

def compute_ensemble_signal(
    station: str,
    target_date: str,
    gefs_data: dict,
    settlements: Dict[str, Dict[str, float]],
) -> Optional[dict]:
    """
    Compute GEFS ensemble signal for a station on a given date.
    
    Uses Kalshi settlement data as ground truth (backtest mode).
    """
    if station not in settlements or target_date not in settlements[station]:
        return None
    
    actual_temp_f = settlements[station][target_date]
    mean_c = gefs_data.get("mean")
    if mean_c is None:
        return None
    
    # Get previous temperature for directional prediction
    station_dates = sorted(settlements[station].keys())
    try:
        idx = station_dates.index(target_date)
    except ValueError:
        return None
    if idx == 0:
        return None
    
    prev_date = station_dates[idx - 1]
    prev_temp_f = settlements[station].get(prev_date)
    if prev_temp_f is None:
        return None
    
    gefs_mean_f = mean_c * 9/5 + 32
    
    # P1.2: Bias correction (prefer ensemble seasonal bias over UHI)
    # _apply_bias_correction handles per-station seasonal bias from 29K matched pairs
    # Falls back to UHI correction if station not in bias table
    try:
        mean_f_arr = np.array([gefs_mean_f], dtype=np.float64)
        ensemble_corrected = _apply_bias_correction(mean_f_arr, station, target_date)
        if ensemble_corrected is not None and len(ensemble_corrected) > 0:
            corrected_mean_f = float(ensemble_corrected[0])
            if abs(corrected_mean_f - gefs_mean_f) < 50.0:
                gefs_mean_f = corrected_mean_f
    except Exception:
        gefs_mean_f = apply_uhi_correction(station, gefs_mean_f, target_date)
    
    actual_dir = 1 if actual_temp_f > prev_temp_f else (-1 if actual_temp_f < prev_temp_f else 0)
    pred_dir = 1 if gefs_mean_f > prev_temp_f else (-1 if gefs_mean_f < prev_temp_f else 0)
    
    if pred_dir == 0 or actual_dir == 0:
        return None
    
    temp_diff = abs(gefs_mean_f - prev_temp_f)
    if temp_diff < DEFAULT_TEMP_DIFF_MIN:
        return None
    
    # FIRST PRINCIPLES: Ensemble fraction as confidence
    # The GEFS has 31 members. Each member is a slightly different model run.
    # fraction_up = count(members predicting up) / 31
    # Confidence = max(fraction_up, 1 - fraction_up) — ensemble agreement
    # This is more principled than the old heuristic (0.5 + temp_diff / 20.0)
    # because it uses all 31 members, not just the mean.
    member_temps_c = gefs_data.get("member_temps_c")
    if member_temps_c and len(member_temps_c) >= 3:
        # Use module's ensemble fraction with bias-corrected members
        member_temps_f = np.array([t * 9/5 + 32 for t in member_temps_c], dtype=np.float64)
        try:
            corrected_members = _apply_bias_correction(member_temps_f, station, target_date)
            if corrected_members is not None and len(corrected_members) == len(member_temps_f):
                fraction_up = _compute_ensemble_fraction(corrected_members, prev_temp_f)
            else:
                n_up = int(np.sum(member_temps_f > prev_temp_f))
                fraction_up = n_up / len(member_temps_f)
        except Exception:
            n_up = int(np.sum(member_temps_f > prev_temp_f))
            fraction_up = n_up / len(member_temps_f)
        raw_confidence = max(fraction_up, 1.0 - fraction_up)
        # Keep direction from mean (more stable), confidence from fraction
    else:
        # Fallback: old heuristic if no member data
        raw_confidence = min(0.99, 0.5 + temp_diff / 20.0)
    
    # P1.5: Calibrate confidence — direction-specific lookup
    # UP predictions use the UP calibration curve, DOWN use DOWN curve
    # This is critical because UP accuracy (59.8%) differs from DOWN (56.1%)
    pred_direction_str = "up" if pred_dir == 1 else "down"
    confidence = calibrate_confidence(station, raw_confidence, target_date, pred_direction_str)
    
    # Market price from Kalshi API (backtest mode: 0.50 fair value)
    # Replaced old circular formula: market_price = 0.5 + confidence * 0.4
    # Gap 1: Support both HIGH and LOW markets
    market_type = "high" if pred_dir == 1 else "low"
    event_ticker = f"KX{market_type.upper()}{station}"  # Simplified; actual ticker includes date
    market_price = get_kalshi_market_price(event_ticker)
    if market_price is None:
        market_price = 0.50  # Fair value fallback (no information)
    
    # Gap 1: HIGH/LOW asymmetry — Size from historical depth/spread
    # LOW markets typically have thinner liquidity — apply sizing reduction
    if market_type == "low":
        # Reduce sizing for LOW markets by 40% (empirical estimate)
        # This accounts for thinner liquidity and wider spreads
        confidence *= 0.6  # Reduce confidence/edge for LOW markets
    
    # Gap 5: Geo concentration — Use station_registry liquidity tier
    # Cap bankroll per climate group at 15-20%
    tier = get_liquidity_tier(station)
    # Apply tier-based sizing adjustment
    if tier == 3:  # Thin liquidity
        confidence *= 0.8  # Reduce by 20% for thin markets
    elif tier == 2:  # Moderate liquidity
        confidence *= 0.9  # Reduce by 10% for moderate markets
    
    if pred_dir == 1:
        # Buying YES contract — edge = P(UP) - YES_price = confidence - market_price
        entry_price = market_price
        edge = confidence - market_price
    else:
        # Buying NO contract — edge = P(DOWN) - NO_price
        # P(DOWN) = confidence (since confidence = 1-f_up when pred=-1)
        # NO_price = 1 - market_price
        # edge = confidence - (1 - market_price)
        entry_price = 1.0 - market_price
        edge = confidence - (1.0 - market_price)
    
    # GLM 5.2 epoch-based gates (P1.4)
    # init_cycle from gefs archive (defaults to '12Z' when null)
    init_cycle = gefs_data.get("init_cycle", DEFAULT_EPOCH_CYCLE) or DEFAULT_EPOCH_CYCLE
    epoch_mult = EPOCH_MULTIPLIERS.get(init_cycle, 1.0)
    epoch_conf_thresh = EPOCH_CONFIDENCE_THRESHOLDS.get(init_cycle, 0.55)
    
    # Epoch entry bounds (tighter [0.25,0.75] vs default [0.15,0.70])
    ep_min = EPOCH_ENTRY_PRICE_MIN
    ep_max = EPOCH_ENTRY_PRICE_MAX
    
    if entry_price < ep_min or entry_price > ep_max:
        return None
    if edge < DEFAULT_EDGE_THRESHOLD:
        return None
    if confidence < epoch_conf_thresh:
        return None
    
    # Edge tiers (GLM 5.2): stronger edge → higher Kelly multiplier
    tier_mult = 1.0
    for thresh, mult, _ in EDGE_TIERS:
        if edge >= thresh:
            tier_mult = mult
            break
    if tier_mult <= 0.0:
        return None
    
    # Kelly sizing (correct formula for binary options)
    # f* = edge / (1 - entry_price) for YES buys
    # For NO buys, same formula since we normalize to YES-equivalent
    if edge > 0 and entry_price < 1.0:
        kelly_pct = edge / (1.0 - entry_price)
    else:
        kelly_pct = 0
    
    n_contracts = int(min(DEFAULT_MAX_CONTRACTS, max(1, kelly_pct * DEFAULT_KELLY_FRACTION * epoch_mult * tier_mult * 1000)))
    n_contracts = max(1, n_contracts)
    
    # Gap 2: Position-size-to-depth gate — Use existing market_cost_model infrastructure
    # Cap position where fill_probability < 0.7
    # Use the apply_liquidity_adjustments function that also handles fallback tiers
    n_contracts = apply_liquidity_adjustments(station, market_type, n_contracts, bankroll=10000.0, fill_probability_threshold=0.7)
    
    correct = pred_dir == actual_dir
    gross_pnl = n_contracts * (1.0 if correct else 0.0)
    cost = n_contracts * entry_price
    
    entry_fee = MARKET_COST_MODEL.kalshi_fee(n_contracts, market_price)
    exit_price = 1.0 if correct else 0.0
    exit_fee = MARKET_COST_MODEL.kalshi_fee(n_contracts, exit_price)
    total_fees = entry_fee + exit_fee
    net_pnl = gross_pnl - cost - total_fees
    
    actual_direction_str = "up" if actual_dir == 1 else "down"
    pred_direction_str = "up" if pred_dir == 1 else "down"
    
    return {
        "station": station,
        "target_date": target_date,
        "prev_date": prev_date,
        "pred_direction": pred_direction_str,
        "actual_direction": actual_direction_str,
        "confidence": confidence,
        "raw_confidence": round(raw_confidence, 4),
        "gefs_mean_f": gefs_mean_f,
        "prev_temp_f": prev_temp_f,
        "entry_price": round(entry_price, 4),
        "market_price": round(market_price, 4),
        "edge": round(edge, 4),
        "kelly_pct": round(kelly_pct, 4),
        "contracts": n_contracts,
        "correct": correct,
        "gross_pnl": gross_pnl,
        "cost": cost,
        "entry_fee": entry_fee,
        "exit_fee": exit_fee,
        "total_fees": total_fees,
        "net_pnl": round(net_pnl, 2),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Database
# ═══════════════════════════════════════════════════════════════════════════

def init_trade_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            station TEXT NOT NULL,
            target_date TEXT NOT NULL,
            prev_date TEXT,
            pred_direction TEXT NOT NULL,
            actual_direction TEXT,
            confidence REAL NOT NULL,
            gefs_mean_f REAL,
            prev_temp_f REAL,
            entry_price REAL NOT NULL,
            market_price REAL NOT NULL,
            edge REAL NOT NULL,
            kelly_pct REAL,
            contracts INTEGER NOT NULL,
            correct INTEGER,
            gross_pnl REAL,
            cost REAL,
            entry_fee REAL,
            exit_fee REAL,
            total_fees REAL,
            net_pnl REAL,
            settlement_date TEXT,
        settled_capital_after REAL,
        unsettled_exposure_after REAL,
            timestamp TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_summary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trading_date TEXT NOT NULL UNIQUE,
            total_trades INTEGER NOT NULL,
            correct_trades INTEGER NOT NULL,
            accuracy REAL NOT NULL,
            total_pnl REAL NOT NULL,
            bankroll REAL NOT NULL,
            sharpe REAL,
            max_drawdown REAL,
            timestamp TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def log_trade(conn: sqlite3.Connection, trade: dict, bankroll: float) -> None:
    """Log a single trade result to the database."""
    c = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    c.execute("""
        INSERT INTO trades 
        (station, target_date, prev_date, pred_direction, actual_direction,
         confidence, gefs_mean_f, prev_temp_f, entry_price, market_price,
         edge, kelly_pct, contracts, correct, gross_pnl, cost,
         entry_fee, exit_fee, total_fees, net_pnl, settlement_date,
         settled_capital_after, unsettled_exposure_after, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        trade["station"], trade["target_date"], trade.get("prev_date", ""),
        trade["pred_direction"], trade.get("actual_direction", ""),
        trade["confidence"], trade.get("gefs_mean_f"), trade.get("prev_temp_f"),
        trade["entry_price"], trade["market_price"], trade["edge"],
        trade.get("kelly_pct", 0), trade["contracts"],
        trade.get("correct"), trade.get("gross_pnl", 0),
        trade.get("cost", 0), trade.get("entry_fee", 0),
        trade.get("exit_fee", 0), trade.get("total_fees", 0),
        trade.get("net_pnl", 0), trade.get("settlement_date"),
        trade.get("settled_capital_after"), trade.get("unsettled_exposure_after"),
        now,
    ))
    conn.commit()


def log_daily_summary(conn: sqlite3.Connection, date: str, trades: list,
                      bankroll: float, all_daily_returns: list) -> None:
    """Log daily summary to the database."""
    n = len(trades)
    correct = sum(1 for t in trades if t.get("correct"))
    acc = correct / n if n > 0 else 0
    pnl = sum(t.get("net_pnl", 0) for t in trades)
    
    sharpe = 0.0
    if len(all_daily_returns) > 1:
        daily_mean = np.mean(all_daily_returns)
        daily_std = np.std(all_daily_returns, ddof=1)
        sharpe = (daily_mean / daily_std * math.sqrt(252)) if daily_std > 0 else 0.0
    
    max_dd = 0.0
    cumulative = 0.0
    peak = 0.0
    for r in all_daily_returns:
        cumulative += r
        peak = max(peak, cumulative)
        dd = (peak - cumulative) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)
    
    c = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    c.execute("""
        INSERT OR REPLACE INTO daily_summary
        (trading_date, total_trades, correct_trades, accuracy, total_pnl,
         bankroll, sharpe, max_drawdown, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (date, n, correct, round(acc, 4), round(pnl, 2),
          round(bankroll, 2), round(sharpe, 4), round(max_dd, 4), now))
    conn.commit()


# ═══════════════════════════════════════════════════════════════════════════
# Main Runner
# ═══════════════════════════════════════════════════════════════════════════

def run_paper_trading(start_date: str, days: int, initial_bankroll: float, no_risk_controls: bool = False):
    """Run GEFS-based paper trading simulation."""
    _LOGGER.info("=" * 60)
    _LOGGER.info("GEFS Paper Trading Cron — Starting")
    _LOGGER.info(f"Start: {start_date}, Days: {days}, Bankroll: ${initial_bankroll:.2f}")
    _LOGGER.info(f"Config: edge={DEFAULT_EDGE_THRESHOLD}, kelly={DEFAULT_KELLY_FRACTION}, "
                 f"ep_min={DEFAULT_ENTRY_PRICE_MIN}, ep_max={DEFAULT_ENTRY_PRICE_MAX}, "
                 f"max_contracts={DEFAULT_MAX_CONTRACTS}")

    # ── Pre-load bias correction table (GR12 Phase A) ──
    _load_ensemble_bias()

    # ── Load settlements ──
    _LOGGER.info("Loading Kalshi settlement data...")
    conn_settle = sqlite3.connect(SETTLEMENTS_DB)
    cur = conn_settle.cursor()
    cur.execute("SELECT station, target_date, kalshi_temp FROM kalshi_settlements ORDER BY target_date")
    settlements = defaultdict(dict)
    for s, d, t in cur.fetchall():
        if t is not None:
            settlements[s][d] = float(t)
    conn_settle.close()
    _LOGGER.info(f"  Loaded {sum(len(v) for v in settlements.values())} records")
    
    # ── Init trade DB ──
    trade_conn = init_trade_db(TRADE_DB)
    
    # ── Init risk controls ──
    risk_config = RiskConfig(
        max_daily_loss_percentage=0.05,    # 5% max daily loss
        max_drawdown_percent=0.15,          # 15% max drawdown
        max_consecutive_losses=10,          # 10 consecutive losses max (20 stations × ~40% loss rate)
        initial_capital=initial_bankroll,
    )
    risk_manager = RiskManager(config=risk_config)
    # Disable the day-limit stop for backtest windows > 30 days.
    # The day-limit is a live-trading control (30 calendar days) that uses
    # simulation-relative time; for long backtests it would halt the full window.
    enable_day_limit = days <= PRIMARY_DAY_LIMIT
    stop_loss = StopLossMonitor(budget=initial_bankroll, enable_day_limit=enable_day_limit)
    _LOGGER.info("  StopLossMonitor: day_limit=%s (PRIMARY_DAY_LIMIT=%d)", enable_day_limit, PRIMARY_DAY_LIMIT)
    
    # ── Generate trading window ──
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    date_list = [(start_dt + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
    _LOGGER.info(f"Trading window: {date_list[0]} → {date_list[-1]} ({len(date_list)} days)")
    _LOGGER.info(f"  Risk controls: {'DISABLED' if no_risk_controls else 'ENABLED'}")
    
    # ── Run simulation ──
    all_trades = []
    daily_pnl_tracker = defaultdict(float)
    bankroll = initial_bankroll
    
    # Gap 4: Settlement timing — Track D+1 capital lock
    # Capital is divided into settled (available) and unsettled (locked) amounts
    settled_capital = initial_bankroll
    unsettled_exposure = 0.0
    settlement_tracker = defaultdict(list)  # date -> list of (cost, pnl)
    
    # Gap 5: Geo concentration — Track cluster exposure
    cluster_exposure = defaultdict(float)  # cluster -> total cost exposure
    city_pair_exposure = defaultdict(float)  # station -> total cost exposure (HIGH+LOW)
    
    def update_settlement(target_date: str):
        """Settle trades from previous day (D+1 settlement)."""
        nonlocal settled_capital, unsettled_exposure
        settlement_date = (datetime.strptime(target_date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        if settlement_date in settlement_tracker:
            items = settlement_tracker[settlement_date]
            for item in items:
                cost = item["cost"]
                pnl = item["pnl"]
                station = item["station"]
                cluster = item.get("cluster")
                # Return locked capital plus P&L
                settled_capital += cost + pnl
                unsettled_exposure -= cost
                # Reduce exposure tracking
                if cluster:
                    cluster_exposure[cluster] = max(0, cluster_exposure[cluster] - cost)
                city_pair_exposure[station] = max(0, city_pair_exposure[station] - cost)
            del settlement_tracker[settlement_date]
            _LOGGER.debug(f"Settlement on {target_date}: settled {len(items)} trades")
    
    for day_idx, target_date in enumerate(date_list):
        _LOGGER.debug(f"Day {day_idx + 1}/{len(date_list)}: {target_date}")
        
        # Update settlement for previous day's trades
        update_settlement(target_date)
        
        # Get GEFS forecast for this date
        gefs_forecast = get_gefs_forecast(target_date)
        
        if not gefs_forecast:
            continue
        
        trades_today = []
        
        for station in STATIONS:
            if station not in gefs_forecast:
                continue
            
            # Check risk controls before trading (skipped in baseline mode)
            if not no_risk_controls:
                risk_state = risk_manager.evaluate()
                if risk_state.halted:
                    _LOGGER.warning(f"Risk controls halted trading on {target_date}: {risk_state.halt_reason}")
                    break
                
                # Check stop-loss
                stopped, stop_reason, stop_details = stop_loss.check_stop_conditions()
                if stopped:
                    _LOGGER.warning(f"Stop-loss triggered on {target_date}: {stop_reason}")
                    break
            
            trade = compute_ensemble_signal(station, target_date,
                                            gefs_forecast[station], settlements)
            if trade is not None:
                trades_today.append(trade)
        
        if not trades_today:
            continue
        
        # Apply per-day capital constraint
        day_pnl = 0
        day_net = 0
        day_fees = 0
        for trade in trades_today:
            net_pnl = trade["net_pnl"]
            cost = trade["cost"]
            station = trade["station"]
            
            # Gap 5: Geo concentration — Cap bankroll per climate group at 15-20%
            cluster = get_cluster_for_station(station)
            if cluster:
                # Check cluster exposure
                cluster_cap = bankroll * 0.18  # 18% per cluster
                if cluster_exposure[cluster] + cost > cluster_cap:
                    # Scale down to respect cluster cap
                    max_cost = max(0, cluster_cap - cluster_exposure[cluster])
                    if max_cost <= 0:
                        continue  # Skip trade, cluster already at cap
                    scale = max_cost / cost
                    trade["contracts"] = int(trade["contracts"] * scale)
                    trade["cost"] = trade["contracts"] * trade["entry_price"]
                    trade["gross_pnl"] = trade["contracts"] * (1.0 if trade["correct"] else 0.0)
                    trade["entry_fee"] = MARKET_COST_MODEL.kalshi_fee(trade["contracts"], trade["market_price"])
                    trade["exit_fee"] = MARKET_COST_MODEL.kalshi_fee(trade["contracts"], 1.0 if trade["correct"] else 0.0)
                    trade["total_fees"] = trade["entry_fee"] + trade["exit_fee"]
                    trade["net_pnl"] = trade["gross_pnl"] - trade["cost"] - trade["total_fees"]
                    cost = trade["cost"]
                    net_pnl = trade["net_pnl"]
                # Update cluster exposure
                cluster_exposure[cluster] += cost
            
            # City-pair exposure (HIGH+LOW) cap
            city_pair_cap = bankroll * 0.08  # 8% per station pair
            if city_pair_exposure[station] + cost > city_pair_cap:
                max_cost = max(0, city_pair_cap - city_pair_exposure[station])
                if max_cost <= 0:
                    continue
                scale = max_cost / cost
                trade["contracts"] = int(trade["contracts"] * scale)
                trade["cost"] = trade["contracts"] * trade["entry_price"]
                trade["gross_pnl"] = trade["contracts"] * (1.0 if trade["correct"] else 0.0)
                trade["entry_fee"] = MARKET_COST_MODEL.kalshi_fee(trade["contracts"], trade["market_price"])
                trade["exit_fee"] = MARKET_COST_MODEL.kalshi_fee(trade["contracts"], 1.0 if trade["correct"] else 0.0)
                trade["total_fees"] = trade["entry_fee"] + trade["exit_fee"]
                trade["net_pnl"] = trade["gross_pnl"] - trade["cost"] - trade["total_fees"]
                cost = trade["cost"]
                net_pnl = trade["net_pnl"]
            city_pair_exposure[station] += cost
            
            # Don't allow bet > 25% of SETTLED capital per trade (Gap 4)
            if cost > settled_capital * 0.25:
                if cost > settled_capital:
                    continue
                # Scale down
                scale = (settled_capital * 0.25) / cost
                trade["contracts"] = int(trade["contracts"] * scale)
                trade["cost"] = trade["contracts"] * trade["entry_price"]
                trade["gross_pnl"] = trade["contracts"] * (1.0 if trade["correct"] else 0.0)
                trade["entry_fee"] = MARKET_COST_MODEL.kalshi_fee(trade["contracts"], trade["market_price"])
                trade["exit_fee"] = MARKET_COST_MODEL.kalshi_fee(trade["contracts"], 1.0 if trade["correct"] else 0.0)
                trade["total_fees"] = trade["entry_fee"] + trade["exit_fee"]
                trade["net_pnl"] = trade["gross_pnl"] - trade["cost"] - trade["total_fees"]
                cost = trade["cost"]
                net_pnl = trade["net_pnl"]
            
            # Gap 4: Settlement timing — Move capital from settled to unsettled
            # Record trade for D+1 settlement
            settlement_date = (datetime.strptime(target_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
            settlement_tracker[settlement_date].append({
                "cost": cost,
                "pnl": net_pnl,
                "station": station,
                "cluster": cluster
            })
            settled_capital -= cost
            unsettled_exposure += cost
            
            trade["settlement_date"] = settlement_date
            trade["settled_capital_after"] = round(settled_capital, 2)
            trade["unsettled_exposure_after"] = round(unsettled_exposure, 2)
            trade["bankroll_after"] = round(bankroll + net_pnl, 2)
            
            # Log to DB
            log_trade(trade_conn, trade, bankroll)
            all_trades.append(trade)
            
            # Update risk controls (skip in baseline mode)
            if not no_risk_controls:
                risk_result = risk_manager.update_after_trade(RCTradeResult(
                    trade_id=f"{station}_{target_date}_{trade['contracts']}",
                    pnl=net_pnl,
                    is_profitable=net_pnl > 0,
                    trade_date=target_date,
                ))
                stop_loss.record_trade(pnl=net_pnl, is_profitable=net_pnl > 0, date_str=target_date)
            
            day_pnl += net_pnl
            day_fees += trade.get("total_fees", 0)
        
        n_correct = sum(1 for t in trades_today if t.get("correct"))
        if day_pnl != 0:
            day_return_pct = day_pnl / bankroll if bankroll > 0 else 0.0
            daily_pnl_tracker[target_date] = day_return_pct
            # Gap 4: Settlement timing — Bankroll updates happen after settlement (next day)
            # Update bankroll variable to reflect total capital (settled + unsettled)
            bankroll = settled_capital + unsettled_exposure
            _LOGGER.info(f"  {target_date}: {len(trades_today)} trades, "
                         f"{n_correct} correct, PnL=${day_pnl:+.2f}, "
                         f"Bankroll=${bankroll:.2f} (Settled=${settled_capital:.2f}, Unsettled=${unsettled_exposure:.2f})")
        else:
            _LOGGER.info(f"  {target_date}: {len(trades_today)} trades, "
                         f"{n_correct} correct, PnL=${day_pnl:+.2f}, "
                         f"Bankroll=${bankroll:.2f} (Settled=${settled_capital:.2f}, Unsettled=${unsettled_exposure:.2f})")
    
    # ── Compute final metrics ──
    daily_return_pcts = []
    bankroll2 = initial_bankroll
    for date in sorted(daily_pnl_tracker.keys()):
        day_r = daily_pnl_tracker[date]
        daily_return_pcts.append(day_r)
        bankroll2 *= (1 + day_r)
    
    # Log daily summaries
    for date in sorted(set(t.get("target_date", "") for t in all_trades)):
        day_trades = [t for t in all_trades if t.get("target_date") == date]
        day_returns_before = [daily_pnl_tracker.get(d, 0) for d in sorted(daily_pnl_tracker.keys()) if d <= date]
        log_daily_summary(trade_conn, date, day_trades, bankroll, day_returns_before)
    
    # ── Print summary ──
    total_trades = len(all_trades)
    correct = sum(1 for t in all_trades if t.get("correct"))
    accuracy = correct / total_trades if total_trades > 0 else 0
    total_pnl = sum(t.get("net_pnl", 0) for t in all_trades)
    total_fees = sum(t.get("total_fees", 0) for t in all_trades)
    
    # Sharpe from daily % returns
    if len(daily_return_pcts) > 1:
        daily_mean = np.mean(daily_return_pcts)
        daily_std = np.std(daily_return_pcts, ddof=1)
        sharpe = (daily_mean / daily_std * math.sqrt(252)) if daily_std > 0 else 0.0
    else:
        sharpe = 0.0
    
    # Profit factor
    wins = sum(t.get("net_pnl", 0) for t in all_trades if t.get("net_pnl", 0) > 0)
    losses = abs(sum(t.get("net_pnl", 0) for t in all_trades if t.get("net_pnl", 0) < 0))
    pf = wins / losses if losses > 0 else (999.0 if wins > 0 else 0.0)
    
    print(f"\n{'=' * 72}")
    print(f"  GEFS PAPER TRADING — RESULTS")
    print(f"{'=' * 72}")
    print(f"  Period:      {date_list[0]} → {date_list[-1]} ({len(date_list)} days)")
    print(f"  Total trades: {total_trades}")
    print(f"  Accuracy:    {accuracy * 100:.2f}%")
    print(f"  Total P&L:   ${total_pnl:+.2f}")
    print(f"  Total fees:  ${total_fees:.2f}")
    print(f"  Final bank:  ${bankroll:.2f} (${initial_bankroll:.2f} initial)")
    print(f"  Daily Sharpe: {sharpe:.4f}")
    print(f"  Profit factor: {pf:.3f}")
    print(f"  Daily returns: {len(daily_return_pcts)}")
    print(f"  Average return: {np.mean(daily_return_pcts) * 100:.4f}%/day" if daily_return_pcts else "")
    print(f"  Volatility:  {np.std(daily_return_pcts, ddof=1) * 100:.4f}%/day" if len(daily_return_pcts) > 1 else "")
    
    # Save JSON summary
    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "config": {
            "edge_threshold": DEFAULT_EDGE_THRESHOLD,
            "kelly_fraction": DEFAULT_KELLY_FRACTION,
            "entry_price_min": DEFAULT_ENTRY_PRICE_MIN,
            "entry_price_max": DEFAULT_ENTRY_PRICE_MAX,
            "max_contracts": DEFAULT_MAX_CONTRACTS,
            "initial_bankroll": initial_bankroll,
            "signal_source": "GEFS ensemble (step=24)",
            "fee_model": "kalshi_real (ceil(0.07 × C × P × (1-P)) per side)",
        },
        "results": {
            "start_date": date_list[0],
            "end_date": date_list[-1],
            "trading_days": len(date_list),
            "total_trades": total_trades,
            "correct_trades": correct,
            "accuracy": round(accuracy, 4),
            "total_pnl": round(total_pnl, 2),
            "total_fees": round(total_fees, 2),
            "final_bankroll": round(bankroll, 2),
            "total_return_pct": round((bankroll - initial_bankroll) / initial_bankroll * 100, 2),
            "daily_sharpe": round(sharpe, 4),
            "profit_factor": round(pf, 4),
        },
    }
    
    summary_path = REPO_ROOT / "data" / "gefs_paper_trading_summary.json"
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    _LOGGER.info(f"Summary written to {summary_path}")
    
    # Close
    trade_conn.close()
    _LOGGER.info("GEFS Paper Trading Cron — Complete")
    return 0


def main():
    import argparse
    parser = argparse.ArgumentParser(description="GEFS Paper Trading Cron")
    parser.add_argument("--days", type=int, default=7, help="Number of trading days")
    parser.add_argument("--start", type=str, default=None,
                        help="Start date (YYYY-MM-DD). Default: today-N days")
    parser.add_argument("--bankroll", type=float, default=INITIAL_BANKROLL,
                        help=f"Initial bankroll (default: ${INITIAL_BANKROLL:.0f})")
    parser.add_argument("--no-risk-controls", action="store_true",
                        help="Disable stop-loss and risk manager for baseline backtesting. "
                             "Evaluates pure signal accuracy without live-trading guardrails.")
    args = parser.parse_args()
    
    if not args.start:
        args.start = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%d")
    
    return run_paper_trading(args.start, args.days, args.bankroll, args.no_risk_controls)


if __name__ == "__main__":
    sys.exit(main())