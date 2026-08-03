#!/usr/bin/env python3
"""
B-Mode P1 Full Backtest Runner — GEFS ensemble, config-driven.

Replaces the 7-day cron window with a full historical backtest (up to all
available dates) and computes per-station tradability stats. All corrections
(UHI, epoch Kelly, calibration) are implemented as walk-forward transforms so
no lookahead bias enters the results.

Pipeline per (station, target_date):
  1. Load GEFS step=24 forecast (ensemble mean + 31 member values).
  2. Optional UHI correction: adjusted_mean_f = gefs_mean_f - trailing_bias
     (trailing 120-day mean of (GEFS - settlement actual), min 30 samples).
  3. Direction: predicted vs previous settlement temp.
  4. Confidence: ensemble fraction max(f_up, 1-f_up). Optional empirical
     calibration: confidence -> empirical win rate from trailing 180-day
     per-station calibration curve.
  5. Market price: 0.50 fair value (backtest; live cron uses Kalshi API).
  6. Edge = |confidence - market_price|, gated by edge threshold / epoch
     confidence threshold / entry price bounds.
  7. Kelly sizing: f* = edge/(1-entry_price), x kelly_fraction x epoch
     multiplier x edge-tier multiplier, capped at max_contracts.
  8. Kalshi fee model (per-contract ceil), binary P&L.

Risk: bankroll tracked; the production cron's live-only stop-loss monitors
(30-day clock, win-rate, etc.) are NOT applied in backtest mode by default —
use --risk to enable RiskManager halts.

Usage:
    python3 scripts/bmode_p1_backtest.py --days 365 --start 2025-08-03 --tag baseline
    python3 scripts/bmode_p1_backtest.py --days 365 --start 2025-08-03 --tag uhi --uhi
    python3 scripts/bmode_p1_backtest.py --days 365 --start 2025-08-03 --tag epoch --epoch
    python3 scripts/bmode_p1_backtest.py --days 365 --start 2025-08-03 --tag calib --calib

Output:
    docs/weather-engine/backtests/<tag>_<YYYYMMDD>.json
    data/bmode_p1_backtest.db (separate from live paper_trading_dev.db)

Version: v1.0 2026-08-03 (B-mode P1.1/P1.2/P1.4/P1.5)
"""

import argparse
import json
import math
import sqlite3
import sys
import struct
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

GEFS_DB = str(REPO_ROOT / "data" / "gefs_archive.db")
SETTLEMENTS_DB = str(REPO_ROOT / "data" / "kalshi_settlements.db")
TRADE_DB = str(REPO_ROOT / "data" / "bmode_p1_backtest.db")
OUTPUT_DIR = REPO_ROOT / "docs" / "weather-engine" / "backtests"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

INITIAL_BANKROLL = 10000.0
STATIONS = [
    "KATL", "KAUS", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU", "KLAS",
    "KLAX", "KMDW", "KMIA", "KMSP", "KMSY", "KNYC", "KOKC", "KPHL",
    "KPHX", "KSAT", "KSEA", "KSFO",
]

# GLM 5.2 (Gray Room R14 Expert A) epoch config
EPOCH_MULTIPLIERS = {"00Z": 0.70, "06Z": 0.85, "12Z": 1.00, "18Z": 0.55}
EPOCH_CONFIDENCE_THRESHOLDS = {"00Z": 0.55, "06Z": 0.58, "12Z": 0.55, "18Z": 0.62}
EDGE_TIERS = [
    {"threshold": 0.10, "multiplier": 1.00, "label": "strong_edge"},
    {"threshold": 0.06, "multiplier": 0.75, "label": "moderate_edge"},
    {"threshold": 0.03, "multiplier": 0.50, "label": "weak_edge"},
    {"threshold": 0.00, "multiplier": 0.00, "label": "no_trade"},
]


@dataclass
class BacktestConfig:
    tag: str = "baseline"
    edge_threshold: float = 0.02
    kelly_fraction: float = 0.50
    entry_price_min: float = 0.15
    entry_price_max: float = 0.70
    temp_diff_min: float = 0.5
    max_contracts: int = 175
    market_price: float = 0.50          # backtest fair-value fallback
    use_uhi: bool = False               # P1.2 walk-forward UHI correction
    uhi_window_days: int = 120
    uhi_min_samples: int = 30
    use_epoch: bool = False             # P1.4 GLM 5.2 epoch schedule
    epoch_cycle: str = "12Z"            # assumed cycle when archive lacks init_cycle
    use_calibration: bool = False       # P1.5 empirical per-station, per-direction calibration
    calib_window_days: int = 180       # (unused; uses full-archive pre-computed curves)
    calib_min_samples: int = 40        # (unused; uses full-archive pre-computed curves)
    use_risk_halts: bool = False        # production RiskManager halts on/off
    bankroll: float = INITIAL_BANKROLL
    station_sizing: Optional[Dict[str, float]] = None  # per-station contract multiplier, e.g. {"KLAS": 0.5}


# ═══════════════════════════════════════════════════════════════════════════
# Pre-computed Direction-Specific Calibration (full archive, no walk-forward)
# ═══════════════════════════════════════════════════════════════════════════

CALIBRATION_FILE = str(REPO_ROOT / "data" / "calibration_curves.json")

# Cached calibration table: {station: {up: {bins:..., global:...}, down: {...}}}
_calibration_table = None

def _load_calibration_table():
    global _calibration_table
    if _calibration_table is not None:
        return _calibration_table
    try:
        with open(CALIBRATION_FILE) as f:
            _calibration_table = json.load(f)
    except Exception as e:
        print(f"  ⚠ Could not load calibration curves: {e}")
        _calibration_table = {}
    return _calibration_table


def calibrate_confidence(station: str, raw_confidence: float, pred_direction: str) -> float:
    """
    Map raw ensemble fraction confidence to empirically calibrated win rate
    using pre-computed full-archive, direction-specific calibration curves.
    
    Lookup order:
      1. Per-station, per-direction bin
      2. Global per-direction bin
      3. Raw confidence (fallback)
    """
    table = _load_calibration_table()
    if not table:
        return raw_confidence
    
    # Find bin label
    confidence_bins = [0.50 + i * 0.05 for i in range(11)]
    bin_label = None
    for i in range(len(confidence_bins) - 1):
        if confidence_bins[i] <= raw_confidence < confidence_bins[i + 1]:
            bin_label = f"{confidence_bins[i]:.2f}-{confidence_bins[i+1]:.2f}"
            break
    if bin_label is None:
        return raw_confidence
    
    direction = pred_direction if pred_direction in ("up", "down") else "up"
    
    # 1. Per-station, per-direction
    cal = table.get(station, {})
    dir_cal = cal.get(direction, {})
    dir_bins = dir_cal.get("bins", {})
    bin_data = dir_bins.get(bin_label, {})
    if bin_data.get("win_rate") is not None:
        return bin_data["win_rate"]
    
    # 2. Global per-direction
    global_cal = table.get("_global", {})
    global_dir = global_cal.get(direction, {})
    global_bins = global_dir.get("bins", {})
    bin_data = global_bins.get(bin_label, {})
    if bin_data.get("win_rate") is not None:
        return bin_data["win_rate"]
    
    # 3. Raw fallback
    return raw_confidence


# ═══════════════════════════════════════════════════════════════════════════
# Fee Model (mirrors GEFS cron — kalshi_real per-contract ceil)
# ═══════════════════════════════════════════════════════════════════════════

def kalshi_fee(contracts: int, price: float) -> float:
    if price <= 0.0 or price >= 1.0:
        return 0.0
    fee_per_contract = math.ceil(0.07 * price * (1.0 - price) * 100.0) / 100.0
    return fee_per_contract * contracts


# ═══════════════════════════════════════════════════════════════════════════
# Data Access
# ═══════════════════════════════════════════════════════════════════════════

def load_gefs_all() -> Dict[str, Dict[str, dict]]:
    """Load all GEFS step=24 forecasts. Returns {station: {target_date: {...}}}."""
    conn = sqlite3.connect(GEFS_DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT station, target_date, ensemble_mean, ensemble_min, ensemble_max,
               n_members, member_values
        FROM gefs_archive WHERE step = 24
    """)
    result = defaultdict(dict)
    for r in cur.fetchall():
        n_members = r["n_members"] or 31
        member_values = r["member_values"]
        member_temps_c = None
        if member_values and len(member_values) >= n_members:
            offsets = list(struct.unpack('b' * n_members, member_values[:n_members]))
            member_temps_c = [r["ensemble_mean"] + o * 0.1 for o in offsets]
        result[r["station"]][r["target_date"]] = {
            "mean_c": r["ensemble_mean"],
            "min_c": r["ensemble_min"],
            "max_c": r["ensemble_max"],
            "n_members": n_members,
            "member_temps_c": member_temps_c,
        }
    conn.close()
    return {k: dict(v) for k, v in result.items()}


def load_settlements() -> Dict[str, Dict[str, float]]:
    """Load Kalshi settlements. Returns {station: {target_date: temp_f}}."""
    conn = sqlite3.connect(SETTLEMENTS_DB)
    cur = conn.cursor()
    cur.execute("SELECT station, target_date, kalshi_temp FROM kalshi_settlements ORDER BY target_date")
    settlements = defaultdict(dict)
    for s, d, t in cur.fetchall():
        if t is not None and s != "TEST":
            settlements[s][d] = float(t)
    conn.close()
    return {k: dict(v) for k, v in settlements.items()}


# ═══════════════════════════════════════════════════════════════════════════
# Walk-forward correction state
# ═══════════════════════════════════════════════════════════════════════════

class WalkForwardCorrections:
    """Trailing-window UHI bias + calibration curves. No lookahead: state for a
    date only includes data from strictly earlier dates."""

    def __init__(self, cfg: BacktestConfig, gefs: Dict, settlements: Dict):
        self.cfg = cfg
        self.gefs = gefs
        self.settlements = settlements
        # History of (date, station, gefs_f, actual_f, confidence, win) events
        self._history: List[Tuple[str, str, float, float, float, int]] = []
        # Trailing per-station bias estimates: {station: bias_f}
        self.uhi_bias: Dict[str, float] = {}
        # Trailing per-station calibration: {station: {bin_center: win_rate}}
        self.calibration: Dict[str, Dict[float, float]] = {}
        self._calib_events: Dict[str, List[Tuple[str, float, int]]] = defaultdict(list)
        self._bias_events: Dict[str, List[Tuple[str, float]]] = defaultdict(list)

    def update(self, date: str, station: str, gefs_f: float, actual_f: float,
               confidence: float, won: int) -> None:
        """Record an observed outcome for use by FUTURE dates."""
        if self.cfg.use_uhi:
            self._bias_events[station].append((date, gefs_f - actual_f))
            self._refresh_uhi_bias(station, date)
        if self.cfg.use_calibration:
            self._calib_events[station].append((date, confidence, won))
            self._refresh_calibration(station, date)

    def _refresh_uhi_bias(self, station: str, current_date: str) -> None:
        cutoff = (datetime.strptime(current_date, "%Y-%m-%d")
                  - timedelta(days=self.cfg.uhi_window_days)).strftime("%Y-%m-%d")
        diffs = [diff for d, diff in self._bias_events[station] if d < cutoff]
        if len(diffs) >= self.cfg.uhi_min_samples:
            self.uhi_bias[station] = float(np.mean(diffs))
        elif station in self.uhi_bias:
            del self.uhi_bias[station]

    def _refresh_calibration(self, station: str, current_date: str) -> None:
        cutoff = (datetime.strptime(current_date, "%Y-%m-%d")
                  - timedelta(days=self.cfg.calib_window_days)).strftime("%Y-%m-%d")
        events = [(c, w) for d, c, w in self._calib_events[station] if d < cutoff]
        if len(events) < self.cfg.calib_min_samples:
            self.calibration.pop(station, None)
            return
        # Bin by 0.05 confidence
        bins: Dict[int, List[int]] = defaultdict(list)
        for c, w in events:
            bins[int(round(c * 20))].append(w)
        curve = {}
        for bin_key, wins in bins.items():
            if len(wins) >= 5:
                curve[bin_key / 20.0] = sum(wins) / len(wins)
        if len(curve) >= 2:
            self.calibration[station] = curve

    def bias_for(self, station: str) -> float:
        return self.uhi_bias.get(station, 0.0)

    def calibrate(self, station: str, confidence: float) -> float:
        curve = self.calibration.get(station)
        if not curve:
            return confidence
        keys = sorted(curve.keys())
        if confidence <= keys[0]:
            return curve[keys[0]]
        if confidence >= keys[-1]:
            return curve[keys[-1]]
        # Linear interpolation between bin centers
        for i in range(len(keys) - 1):
            if keys[i] <= confidence <= keys[i + 1]:
                frac = (confidence - keys[i]) / (keys[i + 1] - keys[i])
                return curve[keys[i]] + frac * (curve[keys[i + 1]] - curve[keys[i]])
        return confidence


# ═══════════════════════════════════════════════════════════════════════════
# Signal computation
# ═══════════════════════════════════════════════════════════════════════════

def epoch_for_date(date_str: str, cfg: BacktestConfig) -> str:
    """Map a target date to a GEFS init cycle. The archive lacks init_cycle for
    historical rows, so we use the configured assumption (default 12Z)."""
    return cfg.epoch_cycle


def compute_signal(
    station: str, target_date: str, prev_date: str,
    gefs: dict, prev_temp_f: float, cfg: BacktestConfig,
    wf: WalkForwardCorrections,
) -> Optional[dict]:
    """Compute a single trade signal. Returns trade dict or None."""
    actual_temp_f = wf.settlements[station][target_date]
    mean_c = gefs.get("mean_c")
    if mean_c is None:
        return None

    gefs_mean_f = mean_c * 9 / 5 + 32

    # P1.2: UHI correction (walk-forward trailing bias)
    if cfg.use_uhi:
        bias = wf.bias_for(station)
        if abs(bias) >= 0.05:  # only correct when there's real signal
            gefs_mean_f = gefs_mean_f - bias

    actual_dir = 1 if actual_temp_f > prev_temp_f else (-1 if actual_temp_f < prev_temp_f else 0)
    pred_dir = 1 if gefs_mean_f > prev_temp_f else (-1 if gefs_mean_f < prev_temp_f else 0)
    if pred_dir == 0 or actual_dir == 0:
        return None

    temp_diff = abs(gefs_mean_f - prev_temp_f)
    if temp_diff < cfg.temp_diff_min:
        return None

    # Confidence from ensemble fraction
    member_temps_c = gefs.get("member_temps_c")
    if member_temps_c and len(member_temps_c) >= 3:
        member_temps_f = [t * 9 / 5 + 32 for t in member_temps_c]
        n_up = sum(1 for t in member_temps_f if t > prev_temp_f)
        fraction_up = n_up / len(member_temps_f)
        confidence = max(fraction_up, 1.0 - fraction_up)
    else:
        confidence = min(0.99, 0.5 + temp_diff / 20.0)

    # P1.5: empirical calibration (direction-specific, full-archive curves)
    if cfg.use_calibration:
        pred_dir_str = "up" if pred_dir == 1 else "down"
        confidence = calibrate_confidence(station, confidence, pred_dir_str)

    market_price = cfg.market_price

    # P1.4: epoch-based gates
    epoch_ceil = None
    epoch_conf_thresh = None
    if cfg.use_epoch:
        epoch = epoch_for_date(target_date, cfg)
        epoch_ceil = EPOCH_MULTIPLIERS.get(epoch, 1.0)
        epoch_conf_thresh = EPOCH_CONFIDENCE_THRESHOLDS.get(epoch, 0.55)
        # GLM entry bounds [0.25, 0.75]
        cfg_ep_min, cfg_ep_max = 0.25, 0.75
    else:
        cfg_ep_min, cfg_ep_max = cfg.entry_price_min, cfg.entry_price_max

    if pred_dir == 1:
        # Buying YES: P(UP) = confidence, YES_price = market_price
        entry_price = market_price
        edge = confidence - market_price
    else:
        # Buying NO: P(DOWN) = confidence (since confidence = 1-f_up when pred=-1)
        # NO_price = 1 - market_price(YES)
        # Edge = P(DOWN) - NO_price = confidence - (1 - market_price)
        entry_price = 1.0 - market_price
        edge = confidence - (1.0 - market_price)

    if entry_price < cfg_ep_min or entry_price > cfg_ep_max:
        return None
    if edge < cfg.edge_threshold:
        return None
    if epoch_conf_thresh is not None and confidence < epoch_conf_thresh:
        return None

    # Kelly sizing: f* = edge / (1 - entry_price)
    if edge > 0 and entry_price < 1.0:
        kelly_pct = edge / (1.0 - entry_price)
    else:
        kelly_pct = 0.0

    # Edge tiers (GLM 5.2)
    tier_mult = 1.0
    if cfg.use_epoch:
        for tier in EDGE_TIERS:
            if edge >= tier["threshold"]:
                tier_mult = tier["multiplier"]
                break
        if tier_mult <= 0.0:
            return None

    epoch_mult = epoch_ceil if cfg.use_epoch else 1.0

    n_contracts = int(min(cfg.max_contracts,
                          max(1, kelly_pct * cfg.kelly_fraction * epoch_mult * tier_mult * 1000)))
    n_contracts = max(1, n_contracts)
    
    # Per-station sizing multiplier (sweep config 5)
    if cfg.station_sizing and station in cfg.station_sizing:
        n_contracts = max(1, int(n_contracts * cfg.station_sizing[station]))

    correct = pred_dir == actual_dir
    gross_pnl = n_contracts * (1.0 if correct else 0.0)
    cost = n_contracts * entry_price

    entry_fee = kalshi_fee(n_contracts, market_price)
    exit_price = 1.0 if correct else 0.0
    exit_fee = kalshi_fee(n_contracts, exit_price)
    total_fees = entry_fee + exit_fee
    net_pnl = gross_pnl - cost - total_fees

    raw_gefs_f = mean_c * 9 / 5 + 32  # un-corrected (for walk-forward bias)
    raw_confidence = confidence       # un-calibrated (for calibration curves)

    return {
        "station": station,
        "target_date": target_date,
        "prev_date": prev_date,
        "pred_direction": "up" if pred_dir == 1 else "down",
        "actual_direction": "up" if actual_dir == 1 else "down",
        "confidence": round(confidence, 4),
        "gefs_mean_f": round(gefs_mean_f, 2),
        "prev_temp_f": round(prev_temp_f, 2),
        "entry_price": round(entry_price, 4),
        "market_price": round(market_price, 4),
        "edge": round(edge, 4),
        "kelly_pct": round(kelly_pct, 4),
        "epoch_mult": round(epoch_mult, 4),
        "tier_mult": round(tier_mult, 4),
        "contracts": n_contracts,
        "correct": correct,
        "gross_pnl": gross_pnl,
        "cost": cost,
        "entry_fee": entry_fee,
        "exit_fee": exit_fee,
        "total_fees": total_fees,
        "net_pnl": round(net_pnl, 2),
        "raw_gefs_f": round(raw_gefs_f, 2),
        "raw_confidence": round(raw_confidence, 4),
    }


# ═══════════════════════════════════════════════════════════════════════════
# DB
# ═══════════════════════════════════════════════════════════════════════════

def init_db():
    conn = sqlite3.connect(TRADE_DB, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bmode_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_tag TEXT NOT NULL,
            station TEXT NOT NULL,
            target_date TEXT NOT NULL,
            prev_date TEXT,
            pred_direction TEXT,
            actual_direction TEXT,
            confidence REAL,
            gefs_mean_f REAL,
            prev_temp_f REAL,
            entry_price REAL,
            market_price REAL,
            edge REAL,
            kelly_pct REAL,
            epoch_mult REAL,
            tier_mult REAL,
            contracts INTEGER,
            correct INTEGER,
            net_pnl REAL,
            total_fees REAL,
            timestamp TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def save_run(conn, run_tag: str, trades: List[dict]):
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.cursor()
    for t in trades:
        cur.execute("""
            INSERT INTO bmode_trades
            (run_tag, station, target_date, prev_date, pred_direction, actual_direction,
             confidence, gefs_mean_f, prev_temp_f, entry_price, market_price, edge,
             kelly_pct, epoch_mult, tier_mult, contracts, correct, net_pnl, total_fees, timestamp)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            run_tag, t["station"], t["target_date"], t.get("prev_date", ""), t.get("pred_direction", ""), t.get("actual_direction", ""),
            t["confidence"], t.get("gefs_mean_f"), t.get("prev_temp_f"),
            t["entry_price"], t["market_price"], t["edge"],
            t.get("kelly_pct", 0), t.get("epoch_mult", 1.0), t.get("tier_mult", 1.0),
            t["contracts"], t.get("correct"), t.get("net_pnl", 0),
            t.get("total_fees", 0), now,
        ))
    conn.commit()


# ═══════════════════════════════════════════════════════════════════════════
# Statistics
# ═══════════════════════════════════════════════════════════════════════════

def wilson_ci(k: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    """Wilson score interval for binomial proportion."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def per_station_stats(trades: List[dict]) -> Dict[str, dict]:
    by_station = defaultdict(list)
    for t in trades:
        by_station[t["station"]].append(t)
    stats = {}
    for st, ts in sorted(by_station.items()):
        n = len(ts)
        correct = sum(1 for t in ts if t.get("correct"))
        acc = correct / n if n else 0.0
        pnl = sum(t.get("net_pnl", 0) for t in ts)
        fees = sum(t.get("total_fees", 0) for t in ts)
        # Per-trade returns (pnl / cost) for Sharpe
        rets = []
        for t in ts:
            cost = t.get("cost", 0)
            if cost > 0:
                rets.append(t["net_pnl"] / cost)
        sharpe = 0.0
        if len(rets) > 2:
            m = np.mean(rets)
            s = np.std(rets, ddof=1)
            sharpe = (m / s * math.sqrt(252)) if s > 0 else 0.0
        wins = sum(t.get("net_pnl", 0) for t in ts if t.get("net_pnl", 0) > 0)
        losses = abs(sum(t.get("net_pnl", 0) for t in ts if t.get("net_pnl", 0) < 0))
        pf = wins / losses if losses > 0 else (999.0 if wins > 0 else 0.0)
        lo, hi = wilson_ci(correct, n)
        stats[st] = {
            "trades": n,
            "correct": correct,
            "accuracy": round(acc, 4),
            "accuracy_ci": [round(lo, 4), round(hi, 4)],
            "pnl": round(pnl, 2),
            "fees": round(fees, 2),
            "sharpe": round(sharpe, 4),
            "profit_factor": round(pf, 4),
            "avg_edge": round(float(np.mean([t.get("edge", 0) for t in ts])), 4),
            "avg_confidence": round(float(np.mean([t.get("confidence", 0) for t in ts])), 4),
        }
    return stats


def overall_stats(trades: List[dict], bankroll: float) -> Dict:
    n = len(trades)
    correct = sum(1 for t in trades if t.get("correct"))
    acc = correct / n if n else 0.0
    total_pnl = sum(t.get("net_pnl", 0) for t in trades)
    total_fees = sum(t.get("total_fees", 0) for t in trades)
    lo, hi = wilson_ci(correct, n)
    # Daily returns by target date
    by_date = defaultdict(list)
    for t in trades:
        by_date[t["target_date"]].append(t)
    daily_rets = []
    for d in sorted(by_date.keys()):
        day_pnl = sum(t.get("net_pnl", 0) for t in by_date[d])
        daily_rets.append(day_pnl / INITIAL_BANKROLL)
    sharpe = 0.0
    if len(daily_rets) > 2:
        m = np.mean(daily_rets)
        s = np.std(daily_rets, ddof=1)
        sharpe = (m / s * math.sqrt(252)) if s > 0 else 0.0
    # Max drawdown on bankroll curve
    peak = INITIAL_BANKROLL
    max_dd = 0.0
    bal = INITIAL_BANKROLL
    for t in trades:
        bal += t.get("net_pnl", 0)
        peak = max(peak, bal)
        if peak > 0:
            max_dd = max(max_dd, (peak - bal) / peak)
    wins = sum(t.get("net_pnl", 0) for t in trades if t.get("net_pnl", 0) > 0)
    losses = abs(sum(t.get("net_pnl", 0) for t in trades if t.get("net_pnl", 0) < 0))
    pf = wins / losses if losses > 0 else (999.0 if wins > 0 else 0.0)
    return {
        "trades": n,
        "correct": correct,
        "accuracy": round(acc, 4),
        "accuracy_ci": [round(lo, 4), round(hi, 4)],
        "total_pnl": round(total_pnl, 2),
        "total_fees": round(total_fees, 2),
        "final_bankroll": round(bankroll, 2),
        "return_pct": round((bankroll - INITIAL_BANKROLL) / INITIAL_BANKROLL * 100, 2),
        "daily_sharpe": round(sharpe, 4),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "profit_factor": round(pf, 4),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Main runner
# ═══════════════════════════════════════════════════════════════════════════

def run_backtest(cfg: BacktestConfig, start_date: str, days: int) -> Dict:
    gefs = load_gefs_all()
    settlements = load_settlements()
    wf = WalkForwardCorrections(cfg, gefs, settlements)

    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    date_list = [(start_dt + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]

    all_trades = []
    bankroll = cfg.bankroll
    risk_halted = False

    # Optional risk manager (production-like halts)
    risk_manager = None
    if cfg.use_risk_halts:
        from core.risk_controls import RiskManager, RiskConfig, TradeResult as RCTradeResult
        risk_manager = RiskManager(config=RiskConfig(
            max_daily_loss_percentage=0.05,
            max_drawdown_percent=0.15,
            max_consecutive_losses=10,
            initial_capital=cfg.bankroll,
        ))

    for target_date in date_list:
        if risk_halted:
            break
        for station in STATIONS:
            if station not in settlements or target_date not in settlements[station]:
                continue
            if risk_manager is not None:
                state = risk_manager.evaluate()
                if state.halted:
                    risk_halted = True
                    break
            sdates = sorted(settlements[station].keys())
            try:
                idx = sdates.index(target_date)
            except ValueError:
                continue
            if idx == 0:
                continue
            prev_date = sdates[idx - 1]
            prev_temp_f = settlements[station].get(prev_date)
            if prev_temp_f is None:
                continue
            gefs_data = gefs.get(station, {}).get(target_date)
            if gefs_data is None:
                continue

            trade = compute_signal(station, target_date, prev_date, gefs_data,
                                   prev_temp_f, cfg, wf)
            if trade is None:
                continue

            # Per-trade bankroll cap (25%)
            cost = trade["cost"]
            if cost > bankroll * 0.25:
                if cost > bankroll:
                    continue
                scale = (bankroll * 0.25) / cost
                trade["contracts"] = int(trade["contracts"] * scale)
                trade["cost"] = trade["contracts"] * trade["entry_price"]
                trade["gross_pnl"] = trade["contracts"] * (1.0 if trade["correct"] else 0.0)
                trade["entry_fee"] = kalshi_fee(trade["contracts"], trade["market_price"])
                trade["exit_fee"] = kalshi_fee(trade["contracts"], 1.0 if trade["correct"] else 0.0)
                trade["total_fees"] = trade["entry_fee"] + trade["exit_fee"]
                trade["net_pnl"] = trade["gross_pnl"] - trade["cost"] - trade["total_fees"]

            bankroll += trade["net_pnl"]
            trade["bankroll_after"] = round(bankroll, 2)
            all_trades.append(trade)

            # Record for walk-forward corrections (only uses PAST data)
            wf.update(target_date, station,
                      trade.get("raw_gefs_f", 0.0) or 0.0,
                      settlements[station][target_date],
                      trade.get("raw_confidence", 0.5) or 0.5,
                      1 if trade.get("correct") else 0)

            if risk_manager is not None:
                risk_manager.update_after_trade(RCTradeResult(
                    trade_id=f"{station}_{target_date}_{trade['contracts']}",
                    pnl=trade["net_pnl"],
                    is_profitable=trade["net_pnl"] > 0,
                    trade_date=target_date,
                ))

    stats = overall_stats(all_trades, bankroll)
    station_stats = per_station_stats(all_trades)

    # UHI bias coverage snapshot (what was actually applied)
    uhi_factors = {}
    if cfg.use_uhi:
        for st in STATIONS:
            if st in wf.uhi_bias:
                uhi_factors[st] = round(wf.uhi_bias[st], 2)
            else:
                uhi_factors[st] = None

    result = {
        "run_tag": cfg.tag,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "config": {
            "start_date": start_date,
            "days": days,
            "edge_threshold": cfg.edge_threshold,
            "kelly_fraction": cfg.kelly_fraction,
            "entry_price_min": cfg.entry_price_min,
            "entry_price_max": cfg.entry_price_max,
            "temp_diff_min": cfg.temp_diff_min,
            "max_contracts": cfg.max_contracts,
            "market_price": cfg.market_price,
            "use_uhi": cfg.use_uhi,
            "uhi_window_days": cfg.uhi_window_days,
            "use_epoch": cfg.use_epoch,
            "epoch_cycle": cfg.epoch_cycle,
            "use_calibration": cfg.use_calibration,
            "calib_type": "full-archive direction-specific" if cfg.use_calibration else None,
            "calib_window_days": cfg.calib_window_days,
            "use_risk_halts": cfg.use_risk_halts,
            "signal_source": "GEFS ensemble step=24, ensemble-fraction confidence",
            "fee_model": "kalshi_real per-contract ceil",
            "calibration_notes": "Direction-specific calibration: UP predictions use UP curve, DOWN use DOWN curve. Full archive (no walk-forward) because GEFS is a physical model." if cfg.use_calibration else None,
            "station_sizing": cfg.station_sizing,
        },
        "results": stats,
        "per_station": station_stats,
        "uhi_factors": uhi_factors,
    }
    return result


def main():
    parser = argparse.ArgumentParser(description="B-Mode P1 Full Backtest Runner")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--start", type=str, default="2025-08-03")
    parser.add_argument("--tag", type=str, default="baseline")
    parser.add_argument("--uhi", action="store_true", help="Enable walk-forward UHI correction (P1.2)")
    parser.add_argument("--epoch", action="store_true", help="Enable GLM 5.2 epoch Kelly schedule (P1.4)")
    parser.add_argument("--epoch-cycle", type=str, default="12Z", choices=["00Z", "06Z", "12Z", "18Z"])
    parser.add_argument("--calib", action="store_true", help="Enable direction-specific calibration from full-archive curves (P1.5)")
    parser.add_argument("--risk", action="store_true", help="Enable RiskManager halts (production-like)")
    parser.add_argument("--edge", type=float, default=0.02)
    parser.add_argument("--max-contracts", type=int, default=175)
    parser.add_argument("--kelly", type=float, default=0.50)
    parser.add_argument("--market-price", type=float, default=0.50)
    parser.add_argument("--json", type=str, default=None, help="Explicit JSON output path")
    parser.add_argument("--station-sizing", type=str, default=None,
                        help='Per-station contract multiplier JSON, e.g. \'{"KLAS": 0.5, "KNYC": 1.2}\'')
    args = parser.parse_args()

    station_sizing = None
    if args.station_sizing:
        station_sizing = json.loads(args.station_sizing)

    cfg = BacktestConfig(
        tag=args.tag,
        edge_threshold=args.edge,
        kelly_fraction=args.kelly,
        max_contracts=args.max_contracts,
        market_price=args.market_price,
        use_uhi=args.uhi,
        use_epoch=args.epoch,
        epoch_cycle=args.epoch_cycle,
        use_calibration=args.calib,
        use_risk_halts=args.risk,
        station_sizing=station_sizing,
    )

    result = run_backtest(cfg, args.start, args.days)

    # JSON output
    json_path = args.json
    if json_path is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        json_path = OUTPUT_DIR / f"bmode_p1_{args.tag}_{stamp}.json"
    else:
        json_path = Path(json_path)
        json_path.parent.mkdir(parents=True, exist_ok=True)
    with open(json_path, "w") as f:
        json.dump(result, f, indent=2)

    # DB output
    conn = init_db()
    save_run(conn, args.tag, _load_trades_from_result(result, args.start, args.days))
    conn.close()

    # Console summary
    r = result["results"]
    print(f"\n{'=' * 72}")
    print(f"  B-MODE P1 BACKTEST — {args.tag.upper()}")
    print(f"{'=' * 72}")
    print(f"  Period:      {args.start} → "
          f"{(datetime.strptime(args.start, '%Y-%m-%d') + timedelta(days=args.days - 1)).strftime('%Y-%m-%d')}")
    print(f"  Config:      UHI={cfg.use_uhi} epoch={cfg.use_epoch} calib={cfg.use_calibration} "
          f"risk={cfg.use_risk_halts} edge={cfg.edge_threshold}")
    print(f"  Trades:      {r['trades']}")
    print(f"  Accuracy:    {r['accuracy'] * 100:.2f}%  (95% CI [{r['accuracy_ci'][0]*100:.1f}%, {r['accuracy_ci'][1]*100:.1f}%])")
    print(f"  P&L:         ${r['total_pnl']:+.2f}  (fees ${r['total_fees']:.2f})")
    print(f"  Final bank:  ${r['final_bankroll']:.2f} ({r['return_pct']:+.2f}%)")
    print(f"  Daily Sharpe: {r['daily_sharpe']:.4f}")
    print(f"  Max drawdown: {r['max_drawdown_pct']:.2f}%")
    print(f"  Profit factor: {r['profit_factor']:.3f}")
    print(f"\n  PER-STATION:")
    print(f"  {'Station':<6} {'Trades':>7} {'Acc%':>6} {'P&L':>10} {'Sharpe':>8} {'PF':>6}  Flag")
    print(f"  {'-'*62}")
    for st, s in sorted(result["per_station"].items(), key=lambda kv: kv[1]["pnl"]):
        flag = ""
        if s["trades"] >= 10:
            if s["accuracy"] < 0.40 or s["pnl"] < 0:
                flag = "⚠ LOSER"
        elif s["trades"] < 10 and s["trades"] > 0:
            flag = "(low n)"
        print(f"  {st:<6} {s['trades']:>7} {s['accuracy']*100:>6.1f} "
              f"${s['pnl']:>+9.2f} {s['sharpe']:>8.2f} {s['profit_factor']:>6.2f}  {flag}")
    print(f"\n  JSON: {json_path}")
    print(f"{'=' * 72}")

    # Stop-condition check (P1.1)
    if r["accuracy"] < 0.55 or r["daily_sharpe"] < 0.15:
        print("\n  ⛔ STOP CONDITION MET: accuracy <55% or Sharpe <0.15 — report to Donna before continuing P1.2-P1.5")
        return 2
    print("\n  ✅ Baseline healthy: accuracy ≥55% and Sharpe ≥0.15 — proceed with P1.2-P1.5")
    return 0


def _load_trades_from_result(result: Dict, start_date: str, days: int) -> List[dict]:
    """Re-run trade generation to get full trade rows for DB persistence.
    Note: trades are regenerated here (deterministic given same config) so we
    can persist detailed per-trade rows without keeping them all in the JSON."""
    cfg = BacktestConfig(
        tag=result["run_tag"],
        edge_threshold=result["config"]["edge_threshold"],
        kelly_fraction=result["config"]["kelly_fraction"],
        entry_price_min=result["config"]["entry_price_min"],
        entry_price_max=result["config"]["entry_price_max"],
        temp_diff_min=result["config"]["temp_diff_min"],
        max_contracts=result["config"]["max_contracts"],
        market_price=result["config"]["market_price"],
        use_uhi=result["config"]["use_uhi"],
        uhi_window_days=result["config"]["uhi_window_days"],
        use_epoch=result["config"]["use_epoch"],
        epoch_cycle=result["config"]["epoch_cycle"],
        use_calibration=result["config"]["use_calibration"],
        calib_window_days=result["config"]["calib_window_days"],
        use_risk_halts=result["config"]["use_risk_halts"],
        station_sizing=result["config"].get("station_sizing", None),
    )
    gefs = load_gefs_all()
    settlements = load_settlements()
    wf = WalkForwardCorrections(cfg, gefs, settlements)
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    date_list = [(start_dt + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
    trades = []
    bankroll = cfg.bankroll
    risk_manager = None
    if cfg.use_risk_halts:
        from core.risk_controls import RiskManager, RiskConfig, TradeResult as RCTradeResult
        risk_manager = RiskManager(config=RiskConfig(
            max_daily_loss_percentage=0.05, max_drawdown_percent=0.15,
            max_consecutive_losses=10, initial_capital=cfg.bankroll))
    risk_halted = False
    for target_date in date_list:
        if risk_halted:
            break
        for station in STATIONS:
            if station not in settlements or target_date not in settlements[station]:
                continue
            if risk_manager is not None:
                if risk_manager.evaluate().halted:
                    risk_halted = True
                    break
            sdates = sorted(settlements[station].keys())
            try:
                idx = sdates.index(target_date)
            except ValueError:
                continue
            if idx == 0:
                continue
            prev_date = sdates[idx - 1]
            prev_temp_f = settlements[station].get(prev_date)
            gefs_data = gefs.get(station, {}).get(target_date)
            if prev_temp_f is None or gefs_data is None:
                continue
            trade = compute_signal(station, target_date, prev_date, gefs_data,
                                   prev_temp_f, cfg, wf)
            if trade is None:
                continue
            cost = trade["cost"]
            if cost > bankroll * 0.25:
                if cost > bankroll:
                    continue
                scale = (bankroll * 0.25) / cost
                trade["contracts"] = int(trade["contracts"] * scale)
                trade["cost"] = trade["contracts"] * trade["entry_price"]
                trade["gross_pnl"] = trade["contracts"] * (1.0 if trade["correct"] else 0.0)
                trade["entry_fee"] = kalshi_fee(trade["contracts"], trade["market_price"])
                trade["exit_fee"] = kalshi_fee(trade["contracts"], 1.0 if trade["correct"] else 0.0)
                trade["total_fees"] = trade["entry_fee"] + trade["exit_fee"]
                trade["net_pnl"] = trade["gross_pnl"] - trade["cost"] - trade["total_fees"]
            bankroll += trade["net_pnl"]
            trades.append(trade)
            wf.update(target_date, station, trade.get("raw_gefs_f", 0.0) or 0.0,
                      settlements[station][target_date],
                      trade.get("raw_confidence", 0.5) or 0.5,
                      1 if trade.get("correct") else 0)
            if risk_manager is not None:
                risk_manager.update_after_trade(RCTradeResult(
                    trade_id=f"{station}_{target_date}_{trade['contracts']}",
                    pnl=trade["net_pnl"], is_profitable=trade["net_pnl"] > 0,
                    trade_date=target_date))
    return trades


if __name__ == "__main__":
    sys.exit(main())
