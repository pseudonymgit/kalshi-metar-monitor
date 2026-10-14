#!/usr/bin/env python3
"""LOW-lane entry bridge (Workstream AF, 2026-10-14) — PREPARE ONLY.

The LOW ("or below") lane's side-by-side vs HIGH found:
  * SAME 13:30 station-local decision pin (Tmin skill is a 06:00-21:00 plateau,
    so the HIGH pin carries over);
  * SAME gate stack / grade ladder / entry window;
  * the cooling-rate nowcast (rlow_1h) does NOT work (Brier worse than the base
    rate) -> NOT used;
  * the primary signal is om_gfs Tmin (Brier 0.209 vs Tmax 0.107 — harder but
    skilful; per-station bias-corrected Brier 0.147).

This bridge is a minimal HIGH-bridge mirror that forecasts on the single om_gfs
Tmin value and sizes with the SAME Kelly rule (scripts/sbox_intraday_ev_bridge
.kelly_position_size). It shares the book (data/paper_trading_sbox.db) via the
additive `lane` column ('LOW').

NOT WIRED to a cron / live path — this file only produces the script (Dan
2026-10-14: "Do NOT wire the LOW bridge to a cron or live path — prepare only").
LOW_BRIDGE_ENABLED defaults OFF and is the intended arming flag for a later
sprint.

Axis (mirror of HIGH `T = floor(hsf@13:30)+1, y = settled_max > T`):
  T(p) = floor(running_low@pin) - 0.5 ; y = 1 if settled_min <= T(p) else 0
  p    = Phi((T - mu_low)/sigma)   ==  P(daily min <= T)
i.e. the LOW proposition is P(daily_min <= X) <=> a NO on the HIGH-shaped ladder.

CLI:
  python3 scripts/low_entry_bridge.py --dry-run [--stations KATL KDEN]
  python3 scripts/low_entry_bridge.py --report
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from sbox_intraday_ev_bridge import (  # noqa: E402
    STATIONS, kelly_position_size, grade_position_cap, compute_trade_grade,
    record_trade, SOLE_FORECAST_RULE_ID, _hours_before_settlement,
    _station_spread_model,
)
from core.strike_selection_ev import select_strike, make_truncated_p_fn  # noqa: E402
from core.station_params import get_station_params  # noqa: E402
from core.station_time import is_within_entry_window, station_timezone_name  # noqa: E402

DATA = REPO / "data"
SBOX_DB = DATA / "paper_trading_sbox.db"
METAR = DATA / "metar_backfill.db"
LOW_CSV = (REPO / "artifacts" / "low-market-backfill"
           / "low_entry_price_series_2026-10-13.csv")

INSTANCE = "SBOX"
LANE = "LOW"
# Arming flag (default OFF). Not read by anything yet — the bridge is unwired.
LOW_BRIDGE_ENABLED = os.environ.get("LOW_BRIDGE_ENABLED", "0") == "1"
# LOW gate mirrors HIGH: same 13:30 pin, same window/grade stack.
DECISION_PIN_LOCAL_HHMM = "13:30:00"
FUSION_RULE_ID = SOLE_FORECAST_RULE_ID


def _ro(p):
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


# ── forecast: om_gfs Tmin (sole) ─────────────────────────────────────────────
def om_gfs_tmin_state(station: str, day_str: str, *, live: bool = True,
                      low_so_far: Optional[float] = None):
    """P(daily_min <= X) source: om_gfs(bc) Tmin DistributionState, or None.

    Uses the market_type='LOW' path added to core/intraday/om_gfs_nwp.py, which
    fetches `temperature_2m_min` and bias-corrects against LOW settlement truth.
    ``low_so_far`` (F2) arms the running-low truncation on the returned state.
    Returns None (never raises) when om_gfs has no LOW value for the day.
    """
    try:
        from core.intraday.om_gfs_nwp import om_gfs_state_for_day
    except Exception:
        return None
    try:
        return om_gfs_state_for_day(station, day_str, live=live, market_type="LOW",
                                    low_so_far=low_so_far)
    except Exception:
        return None


def sole_tmin_p_fn(state, station: Optional[str] = None,
                   low_so_far: Optional[float] = None):
    """Return p_fn: X -> calibrated P(daily_min <= X) for the sole om_gfs Tmin.

    The om_gfs LOW state's ``p_ge(X)`` is P(min >= X) (a lower-bound Gaussian);
    P(min <= X) = 1 - P(min >= X). The raw value is then passed through the SAME
    honest decision-time recalibration the HIGH lane uses, clamped at the
    station ceiling, so both lanes book a comparable calibrated probability.

    F2 (2026-10-14): ``low_so_far`` is the running-low truncation, the LOW mirror
    of the HIGH ``high_so_far``. The state is built with that bound armed on
    ``DistributionState.p_ge`` (P(min>=X)=0, hence P(min<=X)=1, for X at/below the
    observed low — a locked win), exactly as HIGH imposes ``hsf`` on its state
    before the sole p_fn reads it. ``low_so_far`` here is only for the meta flag.

    ``p_fn`` returns the CALIBRATED value; ``meta_fn(X)`` carries the raw +
    provenance row (schema-compatible with the HIGH companion row).
    """
    from core.intraday.honest_calibration import honest_calibrated_p, station_ceiling
    cache: Dict[float, Dict] = {}
    ceiling = station_ceiling(station) if station else None
    rlow = low_so_far if low_so_far is not None else getattr(state, "low_so_far", None)

    def _calc(x: float) -> Dict:
        if x in cache:
            return cache[x]
        # state.p_ge already carries the low_so_far bound -> a strike at/below the
        # observed low returns 0.0 here, i.e. P(min<=X)=1 (locked win).
        p_min_le = 1.0 - float(state.p_ge(x))
        # A strike at/ABOVE the observed running low is already breached (the min
        # so far is <= it) -> P(min<=X)=1 (locked win). state.p_ge enforces this;
        # the flag is diagnostic only.
        locked = rlow is not None and x >= rlow
        ce = float(ceiling) if ceiling is not None else 0.84
        try:
            cal = honest_calibrated_p(p_min_le, ce)
        except ValueError:
            cal = float("nan")
        conf = abs(cal - 0.5) * 2.0 if cal == cal else None
        cache[x] = {
            "p_gefs": round(p_min_le, 4), "p_analog": None, "p_trajectory": None,
            "p_fused": round(p_min_le, 4), "agreement": "sole", "edge_mult": 1.0,
            "traj_verdict": "unavailable", "traj_mult": 1.0, "p_market": None,
            "p_om_gfs": round(p_min_le, 4), "ceiling": round(ce, 4),
            "calibrated_p": cal,
            "composite_conf": (round(conf, 4) if conf is not None else None),
            "grade": None, "use_weights": False,
            "agreement_boost": {"enabled": False},
            "weights_applied": {"om_gfs_tmin": 1.0},
            "sole_forecast": True, "lane": LANE, "fusion_rule": FUSION_RULE_ID,
            "low_so_far": rlow, "running_low_locked": locked,
        }
        return cache[x]

    def p_fn(x: float) -> float:
        return _calc(x)["calibrated_p"]

    return p_fn, _calc


# ── LOW strike ladder (from the backfilled LOW entry-price series) ───────────
def low_ladder_from_csv(station: str, day_str: str) -> List[Dict]:
    """LOW strikes + mids for a station-day from the backfilled price series.

    Workstream AF mirrors the HIGH ladder off the SAME market snapshot source;
    the LOW equivalent is the backfill CSV (the real KXLOW books are ingested as
    market_type='binary' under the KXLOWT* prefix — see
    scripts/low_market_price_backfill_2026-10-13.py). 'B' (both-side) rows carry
    a mid; 'T' (threshold) rows are the strike axis. One mid per strike (nearest
    the 13:30 pin wins).
    """
    ladder: Dict[float, Dict] = {}
    try:
        with open(LOW_CSV) as fh:
            for r in csv.DictReader(fh):
                if r["station"] != station or r["target_date"] != day_str:
                    continue
                try:
                    strike = float(r["strike"])
                    mid = float(r["mid"])
                except (TypeError, ValueError):
                    continue
                if not (0.0 < mid < 1.0):
                    continue
                prev = ladder.get(strike)
                d = float(r.get("dist_pin_minutes") or 9e9)
                if prev is None or d < prev["_d"]:
                    ladder[strike] = {
                        "strike": strike, "yes_price": mid,
                        "no_price": round(1 - mid, 4), "ticker": r["ticker"],
                        "volume_24h": None, "snapshot_time": None,
                        "dist_pin_minutes": d if d < 9e9 else None,
                        "running_low_at_pin": _parse_running_low(r.get("running_low_at_pin")),
                        "_d": d,
                    }
    except OSError:
        return []
    for v in ladder.values():
        v.pop("_d", None)
    return sorted(ladder.values(), key=lambda d: d["strike"])


def _parse_running_low(raw) -> Optional[float]:
    """F2: the observed running minimum at the pin from a backfill row, or None.

    The backfill series carries ``running_low_at_pin`` on every row (constant per
    station-day); a missing/degenerate value yields None so the truncation is
    simply not armed rather than manufacturing a bound.
    """
    if raw in (None, "", "nan", "None"):
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def running_low_at_pin(ladder: List[Dict]) -> Optional[float]:
    """The observed running low (station-day) from any ladder row, or None."""
    for r in ladder:
        v = r.get("running_low_at_pin")
        if v is not None:
            return float(v)
    return None


def running_low(station: str, day_str: str, *,
                pin_local_hhmm: str = DECISION_PIN_LOCAL_HHMM
                ) -> Optional[float]:
    """Min observed temp (F) over [station-local day start, pin). None if none.

    F2 (2026-10-14): the LOW axis mirror of the HIGH bridge's ``high_so_far``.
    The daily minimum cannot go ABOVE anything already observed, so the running
    low at the decision pin bounds the LOW proposition P(min <= X)=1 for every
    X at/below it. Read-only on metar_backfill.db; returns None when the station
    timezone or any observation is unavailable (the caller then falls back to the
    backfill series' own ``running_low_at_pin``).
    """
    tz = station_timezone_name(station)
    if tz is None:
        return None
    from zoneinfo import ZoneInfo
    try:
        tzinfo = ZoneInfo(tz)
    except Exception:
        return None
    try:
        d = date.fromisoformat(day_str)
    except ValueError:
        return None
    local_start = datetime(d.year, d.month, d.day, tzinfo=tzinfo)
    pin_h, pin_m, pin_s = (int(x) for x in pin_local_hhmm.split(":"))
    pin_local = datetime(d.year, d.month, d.day, pin_h, pin_m, pin_s, tzinfo=tzinfo)
    start_utc = local_start.astimezone(timezone.utc)
    pin_utc = pin_local.astimezone(timezone.utc)
    c = _ro(METAR)
    try:
        row = c.execute(
            "SELECT MIN(temp_f) FROM metar_observations WHERE station=? "
            "AND timestamp_utc >= ? AND timestamp_utc < ?",
            (station, start_utc.isoformat(), pin_utc.isoformat())).fetchone()
    except sqlite3.Error:
        return None
    finally:
        c.close()
    return float(row[0]) if row and row[0] is not None else None


def _market_cap(station: str) -> float:
    return float(get_station_params(station)["position_cap_usd"])


# ── entry pass ───────────────────────────────────────────────────────────────
def run(dry_run=False, stations=None, min_edge=0.03, ignore_window=True):
    """Evaluate the LOW lane for each station-day in the LOW backfill panel.

    PREPARE-ONLY pass: it computes what the LOW bridge WOULD book and (unless
    dry_run) records it with lane='LOW'. It never touches the HIGH rows, and it
    is never invoked by a cron.
    """
    stations = stations or STATIONS
    now_utc = datetime.now(timezone.utc)
    conn = None if dry_run else sqlite3.connect(SBOX_DB)
    if conn:
        conn.execute("PRAGMA journal_mode=WAL;")

    results, skipped, diag = [], [], []
    panel = _low_panel_dates()
    for st in stations:
        for day_str in sorted(panel.get(st, [])):
            sp = get_station_params(st)
            if not ignore_window:
                ok, reason = is_within_entry_window(st, day_str, now_utc=now_utc)
                if not ok:
                    skipped.append((st, day_str, reason))
                    continue
            state = om_gfs_tmin_state(st, day_str, live=False)
            if state is None:
                skipped.append((st, day_str, "no_om_gfs_tmin"))
                continue
            ladder = low_ladder_from_csv(st, day_str)
            if not ladder:
                skipped.append((st, day_str, "no_low_ladder"))
                continue
            # F2 (2026-10-14): arm the running-low truncation, the LOW mirror of
            # HIGH's high_so_far. The observed low at the pin is FACT: P(min<=X)=1
            # for every X at/below it (a locked win). Mirrors HIGH, which imposes
            # hsf on its DistributionState before the sole p_fn reads it.
            rlow = running_low(st, day_str, pin_local_hhmm=DECISION_PIN_LOCAL_HHMM)
            if rlow is None:
                # Fall back to the backfill series' own pin reading (auditable).
                rlow = running_low_at_pin(ladder)
            state.low_so_far = rlow
            p_fn, meta_fn = sole_tmin_p_fn(state, station=st, low_so_far=rlow)
            # NO mirror: the KXLOWT-X YES mid already IS the market's P(min<=X),
            # exactly the surface `p_fn` returns. The EV picker therefore prices
            # the YES leg (min<=X) and the NO leg (min>X) directly against the
            # real mids — no axis flip anywhere in the LOW path.
            #
            # F3 (2026-10-14): price EV net of the modeled execution cost
            # (station half-spread + slippage), exactly as HIGH does, and fail
            # CLOSED — a missing/unresolvable calibrated p or spread must reject
            # the trade rather than silently pricing off the bare mid (the LOW
            # lane previously priced off the bare mid with no fail-closed guard,
            # a systematic over-entry bias vs HIGH).
            cost_model = _station_spread_model(ladder)
            picked = select_strike(state.mu, state.sigma, ladder, min_edge=min_edge,
                                   p_fn=p_fn, cost_model=cost_model, fail_closed=True)
            if not picked or picked.get("ev", 0) <= 0:
                skipped.append((st, day_str, "no_positive_ev_strike"))
                continue
            strike = picked["strike"]
            meta = meta_fn(strike)
            calibrated_p = meta.get("calibrated_p")
            if not calibrated_p or calibrated_p != calibrated_p or float(calibrated_p) <= 0.0:
                skipped.append((st, day_str, "no_calibrated_p_fail_closed"))
                continue
            # Booked side for LOW: proposition is P(min <= X) = p_fn(strike) on
            # the YES leg, P(min > X) = 1 - calibrated_p on the NO leg.
            our_prob = (float(calibrated_p) if picked["side"] == "yes"
                        else 1.0 - float(calibrated_p))
            row = next((s for s in ladder if abs(s["strike"] - strike) < 1e-9), None)
            if row is None:
                skipped.append((st, day_str, "no_real_price_for_picked_strike"))
                continue
            mkt = row["yes_price"] if picked["side"] == "yes" else row["no_price"]
            confidence = abs(our_prob - 0.5) * 2.0
            size_usd = kelly_position_size(our_prob, mkt)
            if size_usd <= 0:
                skipped.append((st, day_str, "kelly_zero_edge"))
                continue
            if confidence < sp["confidence_floor"]:
                skipped.append((st, day_str, "below_station_confidence_floor"))
                continue
            grade, grade_edge = compute_trade_grade(our_prob, mkt)
            if grade == "F":
                skipped.append((st, day_str, f"grade_F_edge:{round(grade_edge, 4)}"))
                continue
            size_usd = min(size_usd, grade_position_cap(grade), sp["position_cap_usd"])
            if size_usd <= 0:
                skipped.append((st, day_str, f"grade_{grade}_zero_cap"))
                continue

            probs = {"p_gefs": meta["p_gefs"], "p_analog": None, "p_trajectory": None,
                     "p_fused": meta["p_fused"], "n_analog": None, "n_traj": None,
                     "agreement": "sole", "edge_mult": 1.0,
                     "traj_verdict": "unavailable", "traj_mult": 1.0,
                     "grade_entered": grade, "grade_cap_usd": grade_position_cap(grade),
                     "calibrated_p": meta.get("calibrated_p"), "p_market": None,
                     "p_om_gfs": meta.get("p_om_gfs"),
                     "composite_conf": meta.get("composite_conf"),
                     "grade": meta.get("grade"), "ceiling": meta.get("ceiling"),
                     "fusion_rule": FUSION_RULE_ID,
                     "weights_applied": {"om_gfs_tmin": 1.0}}
            diag.append({"station": st, "day": day_str, "lane": LANE,
                         "strike": strike, "side": picked["side"],
                         "our_prob": round(our_prob, 4), "mkt": mkt,
                         "calibrated_p": meta.get("calibrated_p"),
                         "size": size_usd, "grade": grade, "ev": picked["ev"]})
            if dry_run:
                results.append({"status": "would_execute", "station": st,
                                "day": day_str, "strike": strike,
                                "side": picked["side"], "size": size_usd,
                                "our_prob": round(our_prob, 4), "grade": grade})
            else:
                rec = record_trade(
                    conn, st, "LOW", picked["side"], our_prob, mkt, confidence,
                    strike, row["ticker"], size_usd, "low_entry_bridge", day_str,
                    probs=probs, selected_side=picked["side"], lane=LANE)
                results.append(rec)
    if conn:
        conn.close()
    return {"date_utc": now_utc.strftime("%Y-%m-%d"), "lane": LANE,
            "n_executed": len(results), "n_skipped": len(skipped),
            "executed": results, "skipped": skipped[:25], "diagnostics": diag}


def _low_panel_dates() -> Dict[str, List[str]]:
    """station -> sorted distinct dates in the LOW backfill panel (with truth)."""
    seen: Dict[str, set] = defaultdict(set)
    try:
        with open(LOW_CSV) as fh:
            for r in csv.DictReader(fh):
                if r.get("has_low_truth") == "1":
                    seen[r["station"]].add(r["target_date"])
    except OSError:
        pass
    return {st: sorted(ds) for st, ds in seen.items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--stations", nargs="*", default=None)
    ap.add_argument("--min-edge", type=float, default=0.03)
    a = ap.parse_args()
    out = run(dry_run=a.dry_run, stations=a.stations, min_edge=a.min_edge)
    if a.report:
        print(json.dumps(out, indent=2, default=str))
    else:
        brief = {k: v for k, v in out.items() if k != "executed"}
        print(json.dumps(brief, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
