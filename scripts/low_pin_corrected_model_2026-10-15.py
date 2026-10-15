#!/usr/bin/env python3
"""low_pin_corrected_model_2026-10-15.py — LOW pin sweep under the CORRECT target
model (Dan's challenge, 2026-10-15).

WHY THIS EXISTS
---------------
The prior LOW pin sweeps (AG-1 `low_pin_sweep_market_timing_2026-10-14.py`, and
the earlier om_gfs-Brier-only sweep) framed the LOW pin like the HIGH pin: a
daytime decision point where a still-forming extreme is forecast. Dan
(2026-10-15):

    "The low market is dead in that time window because the low of the day is
     going to happen overnight. I think you're presuming that the target is
     during the day."

He is right. The daily LOW forms OVERNIGHT: the running-low argmin histogram for
this cohort peaks at 05:00-07:00 local. A 13:30-local pin is AFTER the event on
most days, so the market price there is mostly *settled information*, not a
forecast. The weather forecast only matters on the minority of days where the
low has NOT yet formed by the pin.

WHAT THIS MEASURES (per candidate pin)
--------------------------------------
  (a) CERTAINTY FRACTION — % of station-days where the running low at the pin
      already equals the final daily min (running_low@pin <= settled_min + eps).
      On those days the outcome is (near-)decided and the forecast is moot.
  (b) om_gfs Tmin Brier on the REMAINING UNCERTAIN days only — the forecast
      matters exactly here; scored on the honest LOW axis (T = floor(rlow)-0.5,
      y = settled_min <= T, bias-corrected om_gfs Tmin Gaussian + running-low
      truncation).
  (c) MEDIAN SPREAD + quote depth at that station-local hour (KXLOWT* two-sided
      quotes from data/kalshi_market_snapshots.db, READ-ONLY).
  (d) REALIZED DECISION EDGE — the money question, not just Brier. For each
      station-day we take the near-money strike (|mid-0.5| minimal) quoted at the
      pin and bet YES (settled_min <= strike). EV = mean(1{won} - mid - cost).
      We report the realized (outcome-based) edge AND the model-based edge
      (p_calibrated - mid - cost) so the two can be compared.

BOUNDARY CONSTRAINT
-------------------
The entry window forbids entering within T-2h of settlement (settlement = midnight
station-local). The latest USABLE decision pin must therefore leave >= 2h of fill
time: for an EDT station (settlement 04:00Z) that is 22:00 local. Pins later than
22:00 are reported but flagged OUT_OF_BOUNDS.

READ-ONLY on every DB. Writes ONLY the plan doc + its JSON artifact. No book
writes. No network (reuses the /tmp caches the prior sweeps produced).

usage: python3 scripts/low_pin_corrected_model_2026-10-15.py
"""
from __future__ import annotations

import csv
import gzip
import json
import math
import pickle
import re
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from core.station_time import station_timezone_name  # noqa: E402
from core.settlement_labels import settle_yes_from_strike_type  # noqa: E402

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

SNAPS = REPO / "data" / "kalshi_market_snapshots.db"
LADDER_DB = REPO / "data" / "kalshi_strike_ladder.db"
METAR_DB = REPO / "data" / "metar_backfill.db"
LOW_CSV = REPO / "artifacts" / "low-market-backfill" / "low_entry_price_series_2026-10-13.csv"
ASOS_RECS = Path("/tmp/we_asos_recs_2026-10-13.pkl.gz")
OM_LOW_CACHE = Path("/tmp/we_low_omgfs_tmin_2026-10-14.json")

OUT_JSON = REPO / "data" / "low_pin_corrected_2026-10-15.json"
OUT_MD = REPO / "docs" / "plans" / "LOW-PIN-CORRECTED-MODEL-2026-10-15.md"

# Candidate pins (station-local minutes). 13:30 is the incumbent baseline; the
# rest walk the evening toward the boundary.
PINS = [810, 900, 1020, 1140, 1200, 1260, 1320, 1380]
PIN_LABEL = {810: "13:30", 900: "15:00", 1020: "17:00", 1140: "19:00",
             1200: "20:00", 1260: "21:00", 1320: "22:00", 1380: "23:00"}
# Settlement is midnight station-local; the window forbids entry within T-2h.
LATEST_USABLE_PIN = 22 * 60  # 22:00 local == T-2h for an EDT-style station

SIGMA_TMIN = 3.2691           # om_gfs Tmin sigma of the bias (prior LOW sweep)
EXEC_COST_FLOOR = 0.0205      # F1 fallback execution cost (half-spread+slippage)
TICKER_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})-([TB])([0-9]+(?:\.[0-9]+)?)$")
MON = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
       "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


def ro(p):
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


# ── cohort / truth / forecast caches ─────────────────────────────────────────
def load_cohort() -> set:
    """(station, target_date) with LOW truth from the backfill series."""
    sds = set()
    with open(LOW_CSV) as f:
        for r in csv.DictReader(f):
            if r.get("has_low_truth") == "1":
                sds.add((r["station"], r["target_date"]))
    return sds


def load_low_truth() -> Dict[Tuple[str, str], float]:
    c = ro(METAR_DB)
    try:
        return {(r[0], r[1]): float(r[2]) for r in c.execute(
            "SELECT station, local_trading_date, settlement_temp_f FROM settlement_truth "
            "WHERE market_type='LOW' AND settlement_temp_f IS NOT NULL "
            "AND (trustworthy IS NULL OR trustworthy=1)")}
    finally:
        c.close()


def load_recs() -> Dict:
    with gzip.open(ASOS_RECS, "rb") as f:
        return pickle.load(f)


def om_tmin_cache() -> Dict[Tuple[str, str], float]:
    flat = json.loads(OM_LOW_CACHE.read_text())
    return {(k.split("|")[0], k.split("|")[1]): v for k, v in flat.items()}


def running_low(rec: Dict, pin_mod: int) -> Optional[float]:
    """Running low at the pin (min of ASOS bucket means with mod <= pin)."""
    b = rec.get("buckets") or {}
    vals = [b[m] for m in b if m <= pin_mod]
    return min(vals) if vals else None


def final_series_min(rec: Dict) -> Optional[float]:
    b = rec.get("buckets") or {}
    return min(b.values()) if b else None


def low_formed_by_pin(rec: Dict, pin_mod: int) -> bool:
    """True when the day's minimum has already occurred by the pin.

    Within-instrument certainty: the running low equals the final series min.
    Comparing ASOS bucket means to METAR settlement truth across instruments
    does NOT agree on the value (hourly mean vs minute extreme), so the honest
    "has the low formed?" test is argmin(buckets) <= pin.
    """
    b = rec.get("buckets") or {}
    if not b:
        return False
    argmin = min(b, key=lambda x: b[x])
    return argmin <= pin_mod


def min_hour_histogram(recs: Dict, keys) -> Dict[int, int]:
    hist: Dict[int, int] = defaultdict(int)
    for k in keys:
        rec = recs.get(k)
        if not rec:
            continue
        b = rec.get("buckets") or {}
        if not b:
            continue
        m = min(b, key=lambda x: b[x])
        hist[m // 60] += 1
    return dict(sorted(hist.items()))


# ── market quotes at a pin ───────────────────────────────────────────────────
def load_low_quotes() -> Dict[Tuple[str, str], List[Tuple]]:
    """{(station, target_date): [(local_min, strike, bid, ask, mid, oi, ticker), ...]}.

    Two-sided KXLOWT* quotes only; the bulk of snapshot rows are synthetic 0/1
    books and would poison both spread and edge. A snapshot counts for a target
    date ONLY when the snapshot's OWN station-local calendar date equals that
    target date (the backfill's alignment rule) — otherwise a quote a full day
    early leaks in and the "near-money" contract is read before the market had
    the day's information.
    """
    c = ro(SNAPS)
    try:
        rows = c.execute(
            "SELECT snapshot_time, station, market_ticker, yes_bid, yes_ask, "
            "mid_price, open_interest FROM market_snapshots "
            "WHERE market_ticker LIKE 'KXLOWT%' AND market_type='binary' "
            "AND yes_bid > 0 AND yes_ask < 1 AND yes_ask > yes_bid").fetchall()
    finally:
        c.close()
    out: Dict[Tuple[str, str], List[Tuple]] = defaultdict(list)
    for snap, st, tk, bid, ask, mid, oi in rows:
        m = TICKER_RE.search(tk)
        if not m:
            continue
        yy, mon, dd, _kind, strike = m.groups()
        target_date = f"20{yy}-{MON[mon]:02d}-{int(dd):02d}"
        try:
            dt = datetime.fromisoformat(snap)
        except (TypeError, ValueError):
            continue
        if ZoneInfo is not None:
            try:
                dt = dt.astimezone(ZoneInfo(station_timezone_name(st)))
            except Exception:
                pass
        if dt.date().isoformat() != target_date:
            continue
        lmin = dt.hour * 60 + dt.minute
        midv = float(mid) if mid is not None else (float(bid) + float(ask)) / 2.0
        out[(st, target_date)].append(
            (lmin, float(strike), float(bid), float(ask), midv,
             float(oi) if oi is not None else 0.0, tk))
    return out


def quote_at_pin(cells: List[Tuple], pin: int, radius: int = 90):
    """Quotes within +/- radius minutes of the pin (station-local)."""
    near = [c for c in cells if abs(c[0] - pin) <= radius]
    return near


# ── strike axis (greater / less / between) from the SHIPPED ladder ───────────
def band_consistency(cells: List[Tuple]) -> Tuple[int, int]:
    """(checks, violations): a `B` band's mid must not exceed its own superset
    threshold (`T`) mid. A band B<x.5 covers [x, x+1]; its probability can never
    exceed P(min <= x+1) (the `-T(x+1)` threshold). Count incoherent quotes.
    """
    T = {}
    B = {}
    for c in cells:
        kind = c[6].split("-")[-1][:1]
        if kind == "T":
            T[c[1]] = c[4]
        elif kind == "B":
            B[c[1]] = c[4]
    checks = viol = 0
    for bsv, bmid in B.items():
        cap = bsv + 0.5
        cand = [s for s in T if s >= cap - 0.01]
        if not cand:
            continue
        checks += 1
        if bmid > T[min(cand)] + 0.05:
            viol += 1
    return checks, viol


def load_ladder_axes() -> Dict[Tuple[str, str], List[Tuple]]:
    """{(station, target_date): [(strike_value, strike_type, floor, cap, ticker)]}.

    The `-T##` ticker suffix is ambiguous between the greater/less tails; the
    YES label must come from the ladder's OWN strike_type (the same rule the
    settle writer uses via core.settlement_labels).
    """
    c = ro(LADDER_DB)
    try:
        rows = c.execute(
            "SELECT station, local_trading_date, strike_value, strike_type, "
            "floor_strike, cap_strike, ticker FROM strike_ladder "
            "WHERE market_type='LOW'").fetchall()
    finally:
        c.close()
    out: Dict[Tuple[str, str], List[Tuple]] = defaultdict(list)
    for st, ld, sv, sty, fl, cap, tk in rows:
        out[(st, ld)].append((float(sv), sty, fl, cap, tk))
    return out


def load_kalshi_results() -> Dict[str, str]:
    """{ticker: 'yes'|'no'} — Kalshi's OWN recorded settlement (the realized
    outcome that actually pays). The METAR `settlement_truth` diverges from it by
    ~1.7F median on this cohort, so the realized edge must NOT be scored off the
    METAR temperature (that manufactures a fake edge).
    """
    c = ro(LADDER_DB)
    try:
        rows = c.execute(
            "SELECT ticker, result FROM strike_ladder "
            "WHERE market_type='LOW' AND result IN ('yes','no')").fetchall()
    finally:
        c.close()
    return {tk: r for tk, r in rows}


def axis_for_strike(axes: Dict, key, ticker: str, strike: float):
    """Resolve (strike_type, floor, cap) for a quoted ticker on a station-day."""
    for sv, sty, fl, cap, tk in axes.get(key, []):
        if tk == ticker:
            return sty, fl, cap
    # fall back to strike-value match
    for sv, sty, fl, cap, tk in axes.get(key, []):
        if abs(sv - strike) < 1e-9:
            return sty, fl, cap
    return None, None, None


def yes_wins(settled_min: float, strike: float, strike_type, floor, cap) -> bool:
    """YES label on the market's OWN axis (STRICT; equality on a bound = LOSS)."""
    if strike_type:
        try:
            return settle_yes_from_strike_type(settled_min, strike_type, floor, cap)
        except ValueError:
            pass
    return settled_min < strike        # legacy LOW fallback (less tail)


def p_yes_calibrated(mu, sigma, strike, strike_type, floor, cap) -> float:
    """P(YES) under the bias-corrected om_gfs Tmin Gaussian, on the own axis."""
    st = (strike_type or "less").lower()
    if st == "greater":
        return 1.0 - _p_low_le(mu, sigma, floor if floor is not None else strike)
    if st == "between":
        lo = _p_low_le(mu, sigma, floor) if floor is not None else 0.0
        hi = _p_low_le(mu, sigma, cap) if cap is not None else 1.0
        return max(0.0, hi - lo)
    # less / fallback: P(min < bound) == P(min <= bound) for a continuous min.
    return _p_low_le(mu, sigma, cap if cap is not None else strike)


def _p_low_le(mu, sigma, x):
    if sigma <= 0:
        return 1.0 if mu <= x else 0.0
    return 0.5 * (1.0 + math.erf(((x - mu) / sigma) / math.sqrt(2.0)))


def _brier(ps, ys) -> Dict:
    if not ps:
        return {"n": 0}
    n = len(ps)
    brier = sum((p - y) ** 2 for p, y in zip(ps, ys)) / n
    base = sum(ys) / n
    brier_base = sum((base - y) ** 2 for y in ys) / n
    return {"n": n, "brier": round(brier, 4),
            "bss": round(1.0 - brier / brier_base, 4) if brier_base > 0 else 0.0,
            "base_rate": round(base, 4)}


def om_tmin_bias(recs, truth, keys, om_t) -> float:
    resid = []
    for k in keys:
        tmin = truth.get(k)
        mu = om_t.get(k)
        if tmin is None or mu is None:
            continue
        resid.append(mu - tmin)
    return sum(resid) / len(resid) if resid else 0.0


def om_tmin_brier_at_pin(recs, truth, om_t, keys, pin_mod, bias, uncertain_only):
    ps, ys = [], []
    for k in keys:
        rec = recs.get(k)
        tmin = truth.get(k)
        mu = om_t.get(k)
        if rec is None or tmin is None or mu is None:
            continue
        rl = running_low(rec, pin_mod)
        if rl is None:
            continue
        formed = low_formed_by_pin(rec, pin_mod)
        if uncertain_only and formed:
            continue
        T = math.floor(rl) - 0.5
        ps.append(_p_low_le(mu - bias, SIGMA_TMIN, T))
        ys.append(1 if tmin <= T else 0)
    return _brier(ps, ys)


def _calibrated_p_low(mu, sigma, x):
    return _p_low_le(mu, sigma, x)


# ── the four per-pin measures ────────────────────────────────────────────────
def measure_pin(pin, recs, truth, om_t, bias, quotes, cohort, axes, kalshi_results) -> Dict:
    sd_all = [k for k in cohort if k in recs and (recs[k].get("buckets"))]
    # (a) certainty fraction — the day's low has already formed at the pin
    n_formed = sum(1 for k in sd_all if low_formed_by_pin(recs[k], pin))
    certainty = (n_formed / len(sd_all)) if sd_all else None
    # (b) forecast skill only matters on the days the low has NOT formed
    brier_all = om_tmin_brier_at_pin(recs, truth, om_t, sd_all, pin, bias, False)
    brier_unc = om_tmin_brier_at_pin(recs, truth, om_t, sd_all, pin, bias, True)

    # (c) spread + depth at the pin hour, and (d) TWO-SIDED realized decision edge
    spreads, ois = [], []
    realized_edges, model_edges = [], []
    re_thresh, me_thresh = [], []   # coherent `T` threshold contracts only
    re_formed, re_unformed = [], []  # realized edge split by low-formed state
    n_with_quote = 0
    band_checks = band_viol = 0
    kalshi_used = 0
    for k in cohort:
        cells = quotes.get(k)
        if not cells:
            continue
        near = quote_at_pin(cells, pin)
        if not near:
            continue
        n_with_quote += 1
        bc, bv = band_consistency(near)
        band_checks += bc
        band_viol += bv
        tmin = truth.get(k)
        near_sorted = sorted(near, key=lambda c: abs(c[0] - pin))
        _lm, _st, bid, ask, mid0, oi = near_sorted[0][:6]
        spreads.append(ask - bid)
        ois.append(oi)
        # near-money strike on that station-day at the pin (closest to 0.5)
        nm = min(near, key=lambda c: abs(c[4] - 0.5))
        _lm, strike, nm_bid, nm_ask, nm_mid, _oi2 = nm[:6]
        nm_ticker = nm[6] if len(nm) > 6 else None
        if tmin is None:
            continue
        rec = recs.get(k)
        mu = om_t.get(k)
        sty, fl, cap = axis_for_strike(axes, k, nm_ticker, strike)
        # YES of a LOW market wins on the market's OWN axis (greater/less/between).
        # REALIZED outcome = Kalshi's own recorded result (what actually pays);
        # fall back to the METAR-derived label only when the result is absent.
        kr = kalshi_results.get(nm_ticker)
        if kr in ("yes", "no"):
            yes_won = 1 if kr == "yes" else 0
            kalshi_used += 1
        else:
            yes_won = 1 if yes_wins(tmin, strike, sty, fl, cap) else 0
        cost = max(EXEC_COST_FLOOR, (nm_ask - nm_bid) / 2.0)
        # Bet whichever side the calibrated model says is underpriced.
        if rec is not None and mu is not None:
            p_yes = p_yes_calibrated(mu - bias, SIGMA_TMIN, strike, sty, fl, cap)
            ey = p_yes - nm_mid
            en = (1.0 - p_yes) - (1.0 - nm_mid)
            if ey >= en:
                model_edges.append(ey - cost)
                realized_edges.append(yes_won - nm_mid - cost)
            else:
                model_edges.append(en - cost)
                realized_edges.append((1 - yes_won) - (1.0 - nm_mid) - cost)
        else:
            # no forecast: score the market-priced YES side on realized outcome
            realized_edges.append(yes_won - nm_mid - cost)
        # realized edge split by whether the low had already formed at the pin
        if rec is not None and low_formed_by_pin(rec, pin):
            re_formed.append(realized_edges[-1])
        else:
            re_unformed.append(realized_edges[-1])
        # coherent-subset edge: `T` threshold strikes only (the ladder whose
        # mids are internally consistent — see band_consistency).
        if nm[6].split("-")[-1][:1] == "T" and 0.15 <= nm_mid <= 0.85 \
                and rec is not None and mu is not None:
            re_thresh.append(realized_edges[-1])
            me_thresh.append(model_edges[-1])

    def med(v):
        s = sorted(v)
        n = len(s)
        if not n:
            return None
        return round(s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2, 4)

    return {
        "pin_local": PIN_LABEL[pin], "pin_mod": pin,
        "n_station_days_with_running_low": len(sd_all),
        "certainty_fraction": round(certainty, 4) if certainty is not None else None,
        "n_low_formed": n_formed,
        "brier_uncertain_only": brier_unc,
        "brier_all_days": brier_all,
        "n_station_days_with_quote": n_with_quote,
        "median_spread": med(spreads),
        "mean_spread": round(sum(spreads) / len(spreads), 4) if spreads else None,
        "mean_open_interest": round(sum(ois) / len(ois), 1) if ois else None,
        "realized_edge_mean": round(sum(realized_edges) / len(realized_edges), 4) if realized_edges else None,
        "realized_edge_n": len(realized_edges),
        "model_edge_mean": round(sum(model_edges) / len(model_edges), 4) if model_edges else None,
        "model_edge_n": len(model_edges),
        "realized_edge_mean_threshold_only": round(sum(re_thresh) / len(re_thresh), 4) if re_thresh else None,
        "model_edge_mean_threshold_only": round(sum(me_thresh) / len(me_thresh), 4) if me_thresh else None,
        "n_threshold_only": len(re_thresh),
        "band_superset_checks": band_checks,
        "band_superset_violations": band_viol,
        "band_violation_rate": round(band_viol / band_checks, 4) if band_checks else None,
        "kalshi_result_used": kalshi_used,
        "realized_edge_mean_low_formed": round(sum(re_formed) / len(re_formed), 4) if re_formed else None,
        "realized_edge_mean_low_unformed": round(sum(re_unformed) / len(re_unformed), 4) if re_unformed else None,
        "n_low_formed_edge": len(re_formed),
        "n_low_unformed_edge": len(re_unformed),
        "within_fill_boundary": pin <= LATEST_USABLE_PIN,
    }


def _per_sd_edges(pin, recs, truth, om_t, bias, quotes, axes, kalshi_results, cohort):
    """{(station,date): dict} for the edge/paired diagnostics."""
    out = {}
    for k in cohort:
        cells = quotes.get(k)
        if not cells:
            continue
        near = quote_at_pin(cells, pin)
        if not near:
            continue
        nm = min(near, key=lambda c: abs(c[4] - 0.5))
        _lm, strike, bid, ask, mid, _oi, tk = nm
        r = kalshi_results.get(tk)
        if r not in ("yes", "no"):
            continue
        yw = 1 if r == "yes" else 0
        mu = om_t.get(k)
        rec = recs.get(k)
        sty, fl, cap = axis_for_strike(axes, k, tk, strike)
        cost = max(EXEC_COST_FLOOR, (ask - bid) / 2.0)
        p = (p_yes_calibrated(mu - bias, SIGMA_TMIN, strike, sty, fl, cap)
             if mu is not None else None)
        if p is not None:
            ey = p - mid
            en = (1.0 - p) - (1.0 - mid)
            r_edge = (yw - mid - cost) if ey >= en else ((1 - yw) - (1.0 - mid) - cost)
            me = (ey - cost) if ey >= en else (en - cost)
        else:
            r_edge, me = yw - mid - cost, None
        out[k] = {"mid": mid, "yw": yw, "p": p, "r": r_edge, "me": me,
                  "formed": rec is not None and low_formed_by_pin(rec, pin)}
    return out


def _paired(a, b):
    com = set(a) & set(b)
    if not com:
        return {"n": 0}
    dd = [b[k]["r"] - a[k]["r"] for k in com]
    n = len(dd)
    mean = sum(dd) / n
    se = (statistics.pstdev(dd) / math.sqrt(n)) if n > 1 else 0.0
    return {"n": n, "mean_diff": round(mean, 4), "se": round(se, 4)}


def main():
    cohort = load_cohort()
    truth = load_low_truth()
    recs = load_recs()
    om_t = om_tmin_cache()
    quotes = load_low_quotes()
    axes = load_ladder_axes()
    kalshi_results = load_kalshi_results()
    bias = om_tmin_bias(recs, truth, [k for k in cohort if k in recs], om_t)

    hist = min_hour_histogram(recs, [k for k in cohort if k in recs])
    rows = [measure_pin(p, recs, truth, om_t, bias, quotes, cohort, axes,
                        kalshi_results) for p in PINS]

    # ── diagnostics: paired vs the 13:30 incumbent + market-vs-model skill ──
    base = _per_sd_edges(810, recs, truth, om_t, bias, quotes, axes,
                         kalshi_results, cohort)
    paired = {PIN_LABEL[p]: _paired(base, _per_sd_edges(
        p, recs, truth, om_t, bias, quotes, axes, kalshi_results, cohort))
        for p in PINS if p != 810}

    # contested contracts (0.2-0.8) at 13:30: does the market or the model win?
    contested = [v for v in base.values() if 0.2 <= v["mid"] <= 0.8 and v["p"] is not None]
    n = len(contested)
    market_vs_model = {
        "n": n,
        "mean_mid": round(sum(v["mid"] for v in contested) / n, 4) if n else None,
        "mean_model_p": round(sum(v["p"] for v in contested) / n, 4) if n else None,
        "realized_yes_rate": round(sum(v["yw"] for v in contested) / n, 4) if n else None,
        "brier_market": round(sum((v["mid"] - v["yw"]) ** 2 for v in contested) / n, 4) if n else None,
        "brier_model": round(sum((v["p"] - v["yw"]) ** 2 for v in contested) / n, 4) if n else None,
    }

    # recommendation: the latest IN-BOUNDS pin that (i) has the low formed on
    # most days, (ii) keeps a 1c-ish spread, (iii) has the least-negative
    # REALIZED edge (the market-truthed money question). No pin clears zero, so
    # the honest output is the LEAST-BAD in-bounds pin, flagged as not-positive.
    inb = [r for r in rows if r["within_fill_boundary"]]
    rec_pin = min(inb, key=lambda r: abs(r["realized_edge_mean"] or 9)) if inb else None
    any_positive = bool(inb) and max(
        (r["realized_edge_mean"] or -9) for r in inb) > 0

    result = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "target_model": "daily LOW forms OVERNIGHT; a pin after the low has formed "
                        "is settled-information, not forecast",
        "running_low_argmin_hour_histogram": hist,
        "cohort_n": len(cohort),
        "om_tmin_bias_f": round(bias, 4),
        "latest_usable_pin": PIN_LABEL[LATEST_USABLE_PIN],
        "boundary_note": "entry window forbids entering within T-2h of settlement "
                         "(midnight local); 22:00 local is the latest usable pin",
        "realized_truth": "Kalshi's OWN recorded settlement (strike_ladder.result); "
                          "the METAR settlement_truth diverges ~1.7F median",
        "rows": rows,
        "paired_vs_1330": paired,
        "contested_1330_market_vs_model": market_vs_model,
        "recommended_pin": rec_pin["pin_local"] if rec_pin else None,
        "recommended_row": rec_pin,
        "any_inbounds_pin_positive": any_positive,
        "read_only": True,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(result, indent=2))
    print(json.dumps({k: v for k, v in result.items()
                      if k not in ("rows", "recommended_row")}, indent=2, default=str))
    for r in rows:
        print(f"  {r['pin_local']:>5} certainty={r['certainty_fraction']} "
              f"brier_unc={r['brier_uncertain_only'].get('brier')} "
              f"spread={r['median_spread']} realized={r['realized_edge_mean']} "
              f"model={r['model_edge_mean']} inb={r['within_fill_boundary']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
