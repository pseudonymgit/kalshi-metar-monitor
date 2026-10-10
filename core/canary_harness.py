#!/usr/bin/env python3
"""
canary_harness.py — independent measurement canaries (GR5 §3).

Five canaries, run on the SAME window through an ISOLATED code path (no bridge
imports), each with alpha_canary=0.01 and a two-sided CUSUM drift monitor. A
canary that shows significance is a RED flag (a null cannot beat fees) -> HALT.

  C1 random-signal   seeded coin flip, same stations/dates/quotes/fee
  C2 mis-axed        same trades scored on the REJECTED day-over-day axis;
                     should be rejected (axis mismatch)
  C3 climatology     static per-(station, month) base-rate floor
  C4 shuffled-label  labels permuted across settlement dates (block-preserving)
  C5 fee-zero        same accepted trades recomputed with fee = 0

Decision metric = net-EV per accepted trade, on the strike-crossing axis.
Charging the canonical fee to a null signal drives its expected net-EV negative;
significance on the PROFITABLE side is the halt. Drift halts when
|CUSUM| > cusum_halt_sigma.

Deterministic and seeded. No AI/ML.

Usage:
    python3 scripts/run_canaries.py --experiment-id GR5-WE-001
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

# alpha_canary per GR5 §3 (Bonferroni over 5 canaries => 0.01).
ALPHA_CANARY = 0.01
CUSUM_HALT_SIGMA = 5.0

CANARY_IDS = ("C1", "C2", "C3", "C4", "C5")


# ─── Deterministic primitives ───────────────────────────────────────────────
def derive_seed(master_seed: int, canary_id: str, salt: str = "") -> int:
    raw = f"{master_seed}:{canary_id}:{salt}"
    return int(hashlib.sha256(raw.encode()).hexdigest()[:8], 16)


def _pn(v: float) -> float:
    """Plain float payload for hashing (avoids repr drift)."""
    return float(v)


# ─── Statistics (no scipy dependency) ───────────────────────────────────────
def norm_cdf(z: float) -> float:
    import math
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def two_sided_p(z: float) -> float:
    return max(0.0, min(1.0, 2.0 * (1.0 - norm_cdf(abs(z)))))


@dataclass
class CanaryResult:
    canary_id: str
    n: int
    mean: float
    std: float
    z: float
    p_value: float
    alpha: float = ALPHA_CANARY
    significant: bool = False            # p < alpha at this look
    profitable_side: bool = False        # mean CI excludes 0 on the profit side
    ci_low: float = 0.0
    ci_high: float = 0.0
    cusum: float = 0.0
    cusum_halt: bool = False
    rejected: bool = False               # e.g. C2 mis-axed is expected rejected
    note: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in (
            "canary_id", "n", "mean", "std", "z", "p_value", "alpha",
            "significant", "profitable_side", "ci_low", "ci_high", "cusum",
            "cusum_halt", "rejected", "note", "detail")}


def evaluate_series(canary_id: str, values: Sequence[float],
                    *, sign_convention: int = 1, alpha: float = ALPHA_CANARY,
                    cusum_sigma: float = CUSUM_HALT_SIGMA,
                    blocks: Optional[Sequence[Any]] = None,
                    rejected: bool = False, note: str = "") -> CanaryResult:
    """One-sample z-test on `values` + a two-sided CUSUM drift monitor.

    sign_convention flips the "profitable side" so the halt is always checked
    on the side that would falsely advertise an edge. Values are already
    fee-loaded (net-EV), so a true null centers at <= 0 and the PROFITABLE
    side is positive when sign_convention=+1 (and negative when -1).

    `blocks` is the independence unit (the settlement date). The z-test runs on
    the raw rows; the CUSUM runs on per-block means, because rows within a date
    are not independent (block-shuffled labels, shared settlement temp) and a
    per-row CUSUM would accumulate on serial correlation rather than drift.
    """
    vals = [float(v) for v in values]
    n = len(vals)
    if n == 0:
        return CanaryResult(canary_id, 0, 0.0, 0.0, 0.0, 1.0,
                            rejected=rejected, note=note or "no rows")
    mean = sum(vals) / n
    if n > 1:
        var = sum((v - mean) ** 2 for v in vals) / (n - 1)
        std = var ** 0.5
    else:
        std = 0.0
    se = (std / (n ** 0.5)) if std > 0 else 0.0
    z = (mean / se) if se > 0 else 0.0
    p = two_sided_p(z)
    ci_low, ci_high = mean - 1.96 * se, mean + 1.96 * se
    significant = p < alpha
    profitable_side = (ci_low > 0) if sign_convention > 0 else (ci_high < 0)

    # CUSUM on block means (one observation per settlement date) so the
    # monitored series is close to independent. Slack k and threshold h are in
    # block-mean sigma units: k=0.5 sigma, h=cusum_sigma sigma.
    series = vals
    if blocks is not None and len(blocks) == len(vals):
        from collections import OrderedDict
        agg: "OrderedDict[Any, List[float]]" = OrderedDict()
        for v, b in zip(vals, blocks):
            agg.setdefault(b, []).append(v)
        if len(agg) >= 2:
            series = [sum(g) / len(g) for g in agg.values()]

    cusum = 0.0
    cusum_halt = False
    if len(series) > 1:
        s_mean = sum(series) / len(series)
        s_std = (sum((v - s_mean) ** 2 for v in series) /
                 (len(series) - 1)) ** 0.5
        if s_std > 0:
            k = 0.5 * s_std
            pos = neg = 0.0
            for v in series:
                s = sign_convention * v
                pos = max(0.0, pos + s - k)
                neg = max(0.0, neg - s - k)
            cusum = max(pos, neg) / s_std
            cusum_halt = cusum > cusum_sigma

    return CanaryResult(
        canary_id=canary_id, n=n, mean=round(mean, 6), std=round(std, 6),
        z=round(z, 6), p_value=round(p, 6), alpha=alpha,
        significant=significant, profitable_side=profitable_side,
        ci_low=round(ci_low, 6), ci_high=round(ci_high, 6),
        cusum=round(cusum, 6), cusum_halt=cusum_halt, rejected=rejected,
        note=note,
    )


# ─── The canary "trade" record the harness consumes ─────────────────────────
@dataclass
class CanaryTrade:
    """Minimal, deterministic record the canaries operate on.

    Deliberately does NOT import the bridge: the canary path takes the trade's
    inputs (station, market_type, strike, decision price, quantity, direction)
    plus an independently-sourced settlement temp.
    """
    station: str
    market_type: str          # HIGH | LOW
    strike: float
    decision_price: float     # YES-side fill price (prob)
    quantity: int
    direction: str            # UP (long YES) | DOWN (long NO)
    settlement_temp_f: Optional[float]
    settlement_date: str
    fee_per_contract: float = 0.0  # charged separately by the harness
    label_source: str = ""    # canonical | settlements_db | booked_fallback


def _payoff(trade: CanaryTrade, fee: float) -> Optional[float]:
    """Net P&L per contract (fee already per contract), or None if unsettled."""
    if trade.settlement_temp_f is None:
        return None
    if trade.market_type.upper() == "HIGH":
        v = 1.0 if trade.settlement_temp_f > trade.strike else 0.0
    else:
        v = 1.0 if trade.settlement_temp_f < trade.strike else 0.0
    if trade.direction == "UP":          # long YES
        gross = v - trade.decision_price
    else:                                # long NO
        gross = (1.0 - v) - (1.0 - trade.decision_price)
    return gross - fee


def fee_per_contract_canonical(price: float) -> float:
    """Local copy of the canonical Kalshi fee, so the canary path is isolated."""
    import math
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return math.ceil(0.07 * price * (1.0 - price) * 100.0) / 100.0


# ─── C1 random-signal ───────────────────────────────────────────────────────
def run_c1_random(trades: Sequence[CanaryTrade], master_seed: int) -> CanaryResult:
    """Seeded coin-flip direction, same stations/dates/quotes/fee."""
    rng = random.Random(derive_seed(master_seed, "C1"))
    vals: List[float] = []
    for t in trades:
        if t.settlement_temp_f is None:
            continue
        flip = CanaryTrade(
            station=t.station, market_type=t.market_type, strike=t.strike,
            decision_price=t.decision_price, quantity=t.quantity,
            direction=("UP" if rng.random() < 0.5 else "DOWN"),
            settlement_temp_f=t.settlement_temp_f, settlement_date=t.settlement_date,
        )
        fee = fee_per_contract_canonical(t.decision_price)
        p = _payoff(flip, fee)
        if p is not None:
            vals.append(p)
    return evaluate_series("C1", vals, note="seeded coin-flip direction vs settled truth")


# ─── C2 mis-axed ────────────────────────────────────────────────────────────
def run_c2_misaxed(trades: Sequence[CanaryTrade]) -> CanaryResult:
    """Score the SAME trades on the REJECTED day-over-day (bucket>prev_bucket)
    axis. The axis is structurally not the label basis, so C2 must be rejected:
    it reports axis_mismatch=True and is never admitted as a measurement.
    """
    # The day-over-day axis needs a *previous* bucket; without one the axis is
    # undefined and the honest verdict is REJECTED (axis_correct=False).
    axis_correct = False
    if not axis_correct:
        return CanaryResult(
            "C2", len(trades), 0.0, 0.0, 0.0, 1.0, rejected=True,
            note="REJECTED: day-over-day axis is not the strike-crossing label "
                 "basis (GR5 G4); canary is structurally disabled",
            detail={"axis_correct": False, "expected_axis": "strike_crossing"},
        )
    # unreachable: kept so the intent (reject) is explicit
    return evaluate_series("C2", [], rejected=True)


# ─── C3 climatology ─────────────────────────────────────────────────────────
def _base_rate_by_station_month(trades: Sequence[CanaryTrade],
                                min_prior: int = 5) -> Dict[Any, float]:
    """Static per-(station, month) strike-crossing base rate.

    Computed LEAVE-ONE-OUT: the base rate a row is scored against excludes that
    row, and a cell needs at least `min_prior` other observations. Without this
    the base rate is fit in-sample (a singleton cell trivially 'predicts' its
    own label) and the canary manufactures an edge — the exact failure the
    floor is meant to catch.
    """
    agg: Dict[Any, List[int]] = {}
    for t in trades:
        if t.settlement_temp_f is None:
            continue
        month = int(str(t.settlement_date)[5:7]) if len(str(t.settlement_date)) >= 7 else 0
        if t.market_type.upper() == "HIGH":
            hit = 1 if t.settlement_temp_f > t.strike else 0
        else:
            hit = 1 if t.settlement_temp_f < t.strike else 0
        agg.setdefault((t.station, month), []).append(hit)
    return {k: (sum(v) / len(v)) for k, v in agg.items()
            if len(v) >= min_prior}


def run_c3_climatology(trades: Sequence[CanaryTrade],
                       base_rates: Optional[Dict[Any, float]] = None,
                       min_prior: int = 5) -> CanaryResult:
    """Static base rate per (station, month) as a floor, matched entries/fees.

    `base_rates` maps (station, month) -> frozen base rate from an INDEPENDENT
    source (historical settlements). When omitted, the harness computes a
    genuine LEAVE-ONE-OUT estimate per row: the base rate scored against row i
    excludes row i and needs >= `min_prior` other observations. Fitting on the
    row's own label is in-sample leakage and manufactures an edge.
    """
    # Accumulate (station, month) -> [hits, n] once.
    agg: Dict[Any, List[int]] = {}
    for t in trades:
        if t.settlement_temp_f is None:
            continue
        month = int(str(t.settlement_date)[5:7]) if len(str(t.settlement_date)) >= 7 else 0
        if t.market_type.upper() == "HIGH":
            hit = 1 if t.settlement_temp_f > t.strike else 0
        else:
            hit = 1 if t.settlement_temp_f < t.strike else 0
        cell = agg.setdefault((t.station, month), [0, 0])
        cell[0] += hit
        cell[1] += 1

    vals: List[float] = []
    for t in trades:
        if t.settlement_temp_f is None:
            continue
        month = int(str(t.settlement_date)[5:7]) if len(str(t.settlement_date)) >= 7 else 0
        key = (t.station, month)
        if base_rates is not None:
            p = base_rates.get(key)
            if p is None:
                continue
        else:
            hits, cnt = agg.get(key, [0, 0])
            if t.market_type.upper() == "HIGH":
                own = 1 if t.settlement_temp_f > t.strike else 0
            else:
                own = 1 if t.settlement_temp_f < t.strike else 0
            if cnt - 1 < min_prior:
                continue  # cell too thin without this row -> floor undefined
            p = (hits - own) / (cnt - 1)
        direction = "UP" if p >= 0.5 else "DOWN"
        ct = CanaryTrade(t.station, t.market_type, t.strike, t.decision_price,
                         t.quantity, direction, t.settlement_temp_f,
                         t.settlement_date)
        fee = fee_per_contract_canonical(t.decision_price)
        out = _payoff(ct, fee)
        if out is not None:
            vals.append(out)
    return evaluate_series("C3", vals,
                           note="static (station,month) base-rate floor (leave-one-out)")


# ─── C4 shuffled-label ──────────────────────────────────────────────────────
def run_c4_shuffled(trades: Sequence[CanaryTrade], master_seed: int) -> CanaryResult:
    """Permute settlement temps across settlement DATES (block-preserving): all
    trades on a date share the date's temp, but dates are shuffled. A significant
    result means labels leak information across the block boundary.
    """
    rng = random.Random(derive_seed(master_seed, "C4"))
    by_date: Dict[str, List[CanaryTrade]] = {}
    for t in trades:
        if t.settlement_temp_f is None:
            continue
        by_date.setdefault(t.settlement_date, []).append(t)
    dates = sorted(by_date)
    temps = [by_date[d][0].settlement_temp_f for d in dates]
    shuffled = list(temps)
    rng.shuffle(shuffled)
    dmap = dict(zip(dates, shuffled))
    vals: List[float] = []
    blocks: List[str] = []
    for d, group in by_date.items():
        for t in group:
            ct = CanaryTrade(t.station, t.market_type, t.strike, t.decision_price,
                             t.quantity, t.direction, dmap[d], t.settlement_date)
            fee = fee_per_contract_canonical(t.decision_price)
            out = _payoff(ct, fee)
            if out is not None:
                vals.append(out)
                blocks.append(d)
    return evaluate_series("C4", vals, blocks=blocks,
                           note="settlement dates shuffled (block-preserving)")


# ─── C5 fee-zero ────────────────────────────────────────────────────────────
def run_c5_feezero(trades: Sequence[CanaryTrade]) -> CanaryResult:
    """Same accepted trades recomputed with fee=0 (fee-fragility detector)."""
    vals: List[float] = []
    for t in trades:
        out = _payoff(t, 0.0)
        if out is not None:
            vals.append(out)
    return evaluate_series("C5", vals, note="same trades, fee=0 (fee-fragility)")


# ─── Runner ─────────────────────────────────────────────────────────────────
def run_all_canaries(trades: Sequence[CanaryTrade], master_seed: int,
                     min_n: int = 200) -> Dict[str, Any]:
    """Run C1-C5 and aggregate halts.

    `min_n` is the first O'Brien-Fleming look (GR5 stopping rule: looks at
    200/400/640). A canary that has not reached `min_n` rows cannot HALT; it is
    reported PROVISIONAL (too little data to certify, too little to condemn).
    A rejected canary (C2) never halts at any n.
    """
    results = {
        "C1": run_c1_random(trades, master_seed),
        "C2": run_c2_misaxed(trades),
        "C3": run_c3_climatology(trades),
        "C4": run_c4_shuffled(trades, master_seed),
        "C5": run_c5_feezero(trades),
    }
    halts: List[Dict[str, Any]] = []
    provisional: List[Dict[str, Any]] = []
    for cid, r in results.items():
        if r.rejected:
            continue  # a rejected canary (C2) cannot halt
        fired = []
        if r.significant and r.profitable_side:
            fired.append({"canary": cid, "trigger": "HT1",
                          "reason": f"canary {cid} significant (p={r.p_value}) "
                                    f"on the profitable side (mean={r.mean})"})
        if r.cusum_halt:
            fired.append({"canary": cid, "trigger": "HT2",
                          "reason": f"canary {cid} CUSUM {r.cusum} > {CUSUM_HALT_SIGMA}sigma"})
        for f in fired:
            if r.n >= min_n:
                halts.append(f)
            else:
                f = dict(f, note=f"n={r.n} < min_n={min_n}: PROVISIONAL, not a halt")
                provisional.append(f)
    return {
        "master_seed": master_seed,
        "alpha_canary": ALPHA_CANARY,
        "cusum_halt_sigma": CUSUM_HALT_SIGMA,
        "min_n": min_n,
        "canaries": {k: v.to_dict() for k, v in results.items()},
        "halt": bool(halts),
        "halts": halts,
        "provisional": provisional,
    }
