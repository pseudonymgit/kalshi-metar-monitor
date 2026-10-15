#!/usr/bin/env python3
"""market_making_entry_bridge.py — "trade anyway" entry model (DESIGN ONLY).

Dan (2026-10-15):
  "Suppose I want to trade anyway on no edge. Just do the absolute best we can.
   Buy early-ish. Sell at a profit as much as we can."

This is a MARKET-MAKING posture, not edge-prediction. When we have no positive
expected edge, the trade is still worth making *if* we can buy the two-sided
spread and sell it back — the profit comes from the spread and the intraday
revaluation, not from a directional forecast.

WHAT CHANGES vs THE SBOX EV BRIDGE (scripts/sbox_intraday_ev_bridge.py)
-----------------------------------------------------------------------
  ENTRY GATE   EV bridge :  (p_calibrated - mid - cost) > min_edge   [EV-based]
                here     :  spread <= spread_cap  AND  fill_p >= fill_gate
                                                                    [liquidity]
  om_gfs        EV bridge : the entry gate (a forecast)
                here     : a CURATION signal only — hold the strikes it favours,
                           exit the ones it rejects as the day evolves
  SIZING        EV bridge : Kelly on the edge  f* = (p - m - fee)/m
                here     : Kelly on the SPREAD, and never larger than any
                           current leg (a spread play must not out-size the book)
  EXIT          both     : the SAME core/intraday/exit_logic rules
                           (take_profit, stop_loss, signal_flip, auto_close_h1)

NOT WIRED. No cron, no live path, no book writes. This file defines the model and
exposes `dry_run(...)` so the entry gate can be evaluated against snapshot data
without booking anything. Wiring requires an explicit Dan/approval step.

CLI (evaluation only):
  python3 scripts/market_making_entry_bridge.py --dry-run --station KNYC
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

# Exit machinery is SHARED, not re-derived — the same rules the SBOX bridge runs.
from core.intraday.exit_logic import (  # noqa: E402
    evaluate_exits, TAKE_PROFIT_YES_PRICE, TAKE_PROFIT_NO_PRICE, STOP_LOSS_PP,
)

DATA = REPO / "data"
SNAPS = DATA / "kalshi_market_snapshots.db"

# ── Arming flag (defaults OFF). The module is design-only until this is set. ──
MARKET_MAKING_BRIDGE_ENABLED = os.environ.get("MARKET_MAKING_BRIDGE_ENABLED", "0") == "1"

# ── Entry gate parameters (liquidity, not edge) ─────────────────────────────
SPREAD_CAP = 0.01          # enter only when (ask - bid) <= 1¢  (Dan: "spread <= 1¢")
FILL_GATE = 0.60           # minimum modelled probability of a fill at the quote
LIQUID_WINDOW_OPEN = 13 * 60 + 30   # 13:30 station-local ("early-ish")
LIQUID_WINDOW_CLOSE = 20 * 60       # 20:00 station-local
N_LEGS = 2                 # buy the 2 cheapest strikes; allow up to 3

# ── Sizing: Kelly on the SPREAD, capped at the smallest current leg ─────────
BANKROLL_USD = 200.0
MAX_KELLY_FRACTION = 0.05
MAX_POSITION_USD = 10.0
# A market-making leg must never be larger than the smallest leg already held,
# so the spread book stays balanced and no single leg dominates the exposure.
MAX_LEG_VS_BOOK = 1.0


@dataclass
class LegQuote:
    ticker: str
    strike: float
    bid: float
    ask: float
    mid: float
    spread: float
    open_interest: float = 0.0

    @property
    def half_spread(self) -> float:
        return self.spread / 2.0


@dataclass
class EntryDecision:
    station: str
    target_date: str
    leg: LegQuote
    fill_p: float
    entry: bool
    reason: str
    size_usd: float = 0.0
    quantity: int = 0


def _ro(p) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


# =============================================================================
# 1. ENTRY GATE — spread-based, fill-based. NOT edge-based.
# =============================================================================
def fill_probability(quote: LegQuote, *, depth_floor: float = 100.0) -> float:
    """Modelled probability that a passive order fills at this quote.

    A market-making entry does not need an edge; it needs to *get filled* at a
    price it can later sell. The spread is enforced as a **hard gate** by the
    caller (<= SPREAD_CAP, which is the 1-tick minimum), so among admissible
    quotes the binding constraint is **depth**: a tight quote on a thin book may
    be stale and never fill.

        p_fill = min(1, open_interest / depth_floor)

    Deterministic, no AI/ML.
    """
    if quote.spread <= 0 or quote.spread > SPREAD_CAP:
        return 0.0
    if depth_floor <= 0:
        return 1.0
    return max(0.0, min(1.0, quote.open_interest / depth_floor))


def in_liquid_window(local_minute: int) -> bool:
    return LIQUID_WINDOW_OPEN <= local_minute <= LIQUID_WINDOW_CLOSE


def select_legs(quotes: List[LegQuote], n: int = N_LEGS) -> List[LegQuote]:
    """The cheapest strikes we can buy into — the market-making inventory.

    Prefer tight spreads, then the cheapest ask (cheapest inventory to hold and
    the most room to sell back at a profit). Deterministic sort.
    """
    tradable = [q for q in quotes
                if q.spread > 0 and q.spread <= SPREAD_CAP and 0.0 < q.mid < 1.0]
    tradable.sort(key=lambda q: (q.spread, q.ask))
    return tradable[:n]


def evaluate_entry(quote: LegQuote, local_minute: int) -> EntryDecision:
    """The entry gate: spread <= cap AND fill_p >= gate AND inside the window."""
    if not in_liquid_window(local_minute):
        return EntryDecision("", "", quote, 0.0, False, "outside_liquid_window")
    if quote.spread > SPREAD_CAP:
        return EntryDecision("", "", quote, 0.0, False, "spread_too_wide")
    p = fill_probability(quote)
    if p < FILL_GATE:
        return EntryDecision("", "", quote, p, False, "fill_probability_below_gate")
    return EntryDecision("", "", quote, p, True, "enter")


# =============================================================================
# 2. CURATION — om_gfs is a signal, not a gate.
# =============================================================================
def curation_score(om_gfs_mu: float, sigma: float, strike: float) -> float:
    """P(the strike is *in play*) — how strongly om_gfs favours holding this leg.

    For a HIGH leg, the strike is in play when the forecast high lands near it;
    we use the two-sided density at the strike rather than a tail probability,
    because curation asks "is this the strike the day is going to settle around",
    not "does it cross". A leg the forecast pushes away from is a leg to exit.
    """
    if sigma <= 0:
        return 0.0
    z = (strike - om_gfs_mu) / sigma
    return math.exp(-0.5 * z * z)  # unnormalised Gaussian density at the strike


def curation_decide(om_gfs_mu: float, sigma: float, held: List[LegQuote],
                    keep: int = 1) -> List[Tuple[LegQuote, bool]]:
    """Rank held legs by curation; keep the top `keep`, mark the rest for exit."""
    scored = [(curation_score(om_gfs_mu, sigma, q.strike), q) for q in held]
    scored.sort(key=lambda t: t[0], reverse=True)
    return [(q, i < keep) for i, (_, q) in enumerate(scored)]


# =============================================================================
# 3. SIZING — Kelly on the spread, never larger than any current leg.
# =============================================================================
def spread_kelly_fraction(entry_price: float, exit_price: float) -> float:
    """Kelly fraction for a spread play: win = sell at exit_price, lose = 0.

    The "edge" here is the modelled spread capture (exit - entry), and the bet
    pays $1 or $0 exactly like a binary. f* = (win_prob*b - (1-win_prob)) / b
    with b = (1 - entry_price)/entry_price is the standard binary Kelly; for a
    pure spread play win_prob is the fill/exit probability, not a forecast.
    """
    if not (0.0 < entry_price < 1.0):
        return 0.0
    b = (1.0 - exit_price) / exit_price if 0.0 < exit_price < 1.0 else 0.0
    if b <= 0:
        return 0.0
    # conservative: treat the spread capture as the payout, entry as the stake
    capture = max(0.0, exit_price - entry_price)
    return capture / entry_price


def spread_position_size(entry_price: float, exit_price: float, *,
                         bankroll: float = BANKROLL_USD,
                         max_fraction: float = MAX_KELLY_FRACTION,
                         max_position: float = MAX_POSITION_USD,
                         smallest_current_leg_usd: Optional[float] = None) -> float:
    """USD size for a market-making leg.

    Kelly on the spread, then capped by BOTH the bankroll fraction / hard cap AND
    the smallest current leg (a spread book must stay balanced). Returns 0.0 when
    the spread capture is non-positive.
    """
    f = spread_kelly_fraction(entry_price, exit_price)
    if f <= 0.0:
        return 0.0
    f = min(f, max_fraction)
    size = min(bankroll * f, max_position)
    if smallest_current_leg_usd is not None:
        size = min(size, smallest_current_leg_usd * MAX_LEG_VS_BOOK)
    return round(max(0.0, size), 2)


# =============================================================================
# 4. EXIT — the SAME rules the SBOX bridge runs. Not re-derived.
# =============================================================================
def build_exit_positions(held: List[LegQuote], entry_prices: Dict[str, float]) -> List[Dict]:
    """Held legs -> exit_logic position dicts (direction always UP: we bought YES)."""
    out = []
    for q in held:
        out.append({
            "station": q.ticker,
            "direction": "UP",
            "entry_price": entry_prices.get(q.ticker, q.mid),
            "position_size": 1.0,
            "entry_hour": LIQUID_WINDOW_OPEN // 60,
        })
    return out


def evaluate_mm_exits(held: List[LegQuote], entry_prices: Dict[str, float],
                      live_prices: Dict[str, float], hours_to_settlement: int,
                      enabled_rules=None) -> List:
    """Delegates to the shared exit engine with take-profit + signal_flip on."""
    rules = enabled_rules or {"take_profit", "stop_loss", "signal_flip", "auto_close_h1"}
    return evaluate_exits(build_exit_positions(held, entry_prices),
                          {}, live_prices, hours_to_settlement,
                          enabled_rules=rules)


# =============================================================================
# 5. DRY RUN — evaluate the gate against snapshots. NO booking.
# =============================================================================
def _ticker_yyyymmdd(ticker: str) -> Optional[str]:
    """'KXHIGHNY-26SEP08-T80' -> '2026-09-08' (the contract's own target day)."""
    import re
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})-", ticker)
    if not m:
        return None
    mon = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
           "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
    yy, mo, dd = m.groups()
    try:
        return f"20{int(yy):02d}-{mon[mo]:02d}-{int(dd):02d}"
    except KeyError:
        return None


def load_quotes(station: str, target_date: str) -> List[LegQuote]:
    """Two-sided snapshot quotes for one station-day (read-only), date-aligned
    to the ticker's OWN target date, and reduced to ONE quote per ticker — the
    snapshot nearest the 13:30 station-local pin. Without that reduction the
    same ticker appears dozens of times and the selection is dominated by
    whichever hour happened to be sampled most."""
    from zoneinfo import ZoneInfo
    from core.station_time import station_timezone_name

    c = _ro(SNAPS)
    try:
        rows = c.execute(
            "SELECT market_ticker, yes_bid, yes_ask, mid_price, open_interest, snapshot_time "
            "FROM market_snapshots WHERE station=? AND market_ticker LIKE 'KXHIGH%-T%' "
            "AND market_type='binary' AND yes_bid > 0 AND yes_ask < 1 AND yes_ask > yes_bid",
            (station,)).fetchall()
    finally:
        c.close()

    try:
        tz = ZoneInfo(station_timezone_name(station))
        pin = datetime.fromisoformat(f"{target_date}T13:30:00").replace(tzinfo=tz).timestamp()
    except Exception:
        pin = None

    best: Dict[str, tuple] = {}
    for tk, bid, ask, mid, oi, snap in rows:
        if _ticker_yyyymmdd(tk) != target_date:
            continue
        try:
            ep = datetime.fromisoformat(snap).timestamp()
        except (TypeError, ValueError):
            ep = 0.0
        dist = abs(ep - pin) if pin is not None else 0.0
        if tk not in best or dist < best[tk][0]:
            best[tk] = (dist, bid, ask, mid, oi)

    out = []
    for tk, (_, bid, ask, mid, oi) in best.items():
        try:
            strike = float(tk.rsplit("-T", 1)[-1])
        except ValueError:
            continue
        out.append(LegQuote(tk, strike, float(bid), float(ask),
                            float(mid) if mid is not None else (bid + ask) / 2.0,
                            float(ask) - float(bid), float(oi or 0.0)))
    return out


def dry_run(station: str, target_date: str) -> Dict:
    """Evaluate the entry gate only. Books nothing. Never touches the book."""
    quotes = load_quotes(station, target_date)
    picks = select_legs(quotes)
    decisions = [evaluate_entry(q, LIQUID_WINDOW_OPEN) for q in picks]
    return {
        "station": station, "target_date": target_date,
        "n_quotes": len(quotes), "n_picks": len(picks),
        "entry_gate": {"spread_cap": SPREAD_CAP, "fill_gate": FILL_GATE,
                       "window": [LIQUID_WINDOW_OPEN, LIQUID_WINDOW_CLOSE]},
        "picks": [{"ticker": d.leg.ticker, "strike": d.leg.strike,
                   "bid": d.leg.bid, "ask": d.leg.ask, "spread": d.leg.spread,
                   "fill_p": round(d.fill_p, 3), "enter": d.entry,
                   "reason": d.reason} for d in decisions],
        "booked": False,
        "note": "design-only; dry run never books and never writes the book",
    }


def main():
    ap = argparse.ArgumentParser(description="Market-making entry model (design only)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--station", default="KNYC")
    ap.add_argument("--date", default="2026-09-08")
    args = ap.parse_args()
    if not MARKET_MAKING_BRIDGE_ENABLED:
        print("[mm] MARKET_MAKING_BRIDGE_ENABLED=0 — design-only mode (no booking).")
    out = dry_run(args.station, args.date)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
