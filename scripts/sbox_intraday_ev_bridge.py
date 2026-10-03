#!/usr/bin/env python3
"""
SBOX Multi-Forecaster Intraday EV Bridge (2026-09-30)
=====================================================
Extends the Item-1 bridge (scripts/sbox_intraday_ev_bridge.py) to run THREE
independent forecasters and score trading AND analytics together.

Pipeline:

    WS-P calibrated GEFS/emOS distribution        (p_gefs,  intraday-truncated)
  + GHCN analog empirical CDF                     (p_analog, independent)
  + trajectory historical-analog outcome CDF      (p_trajectory, independent)
        -> fusion (core/intraday/multi_forecaster_fusion.py)
        -> strike selection (core/strike_selection_ev.py, EV net of fee)
        -> EntryTimingEngine entry decision
        -> paper trade recorded in data/paper_trading_sbox.db
           (p_gefs, p_analog, p_trajectory, p_fused stored per trade)
  AND a pure weather-analytics evaluation (CRPS vs settlement, resolution)
      reported TOGETHER with the trade results.

Deterministic, no AI/ML. SBOX only — refuses to touch DEV/PROD.

Fusion rule (see docs/plans/SBOX-MULTI-FORECASTER-BRIDGE-2026-09-30.md):
  core  = 0.55*p_gefs + 0.45*p_analog          when |gap| <= 0.10  (agree, x1.0)
        = 0.50/0.50                             when 0.10 < gap <= 0.20 (split, x1.25)
        = shrink(0.50/0.50, x0.5)               when gap > 0.20        (disagree, x1.50)
  p_fused = 0.5 + traj_mult*(core - 0.5), traj_mult in {0.75 discount, 0.95 soft, 1.05 confirm}
  min-edge is scaled by the agreement edge multiplier for the picked strike.

CLI:
  python3 scripts/sbox_intraday_ev_bridge.py --dry-run --ignore-window   # evaluate
  python3 scripts/sbox_intraday_ev_bridge.py                             # record (window)
  python3 scripts/sbox_intraday_ev_bridge.py --combined-report           # trade+analytics
  python3 scripts/sbox_intraday_ev_bridge.py --report                    # per-strike dump
"""
import argparse
import json
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "core"))

# Load repo-root .env before anything reads webhook/API keys. Cron runs via
# `sh -lc` with no shell init that sources .env, so without this the SBOX
# trade-failure notify path resolves no webhook and silently no-ops.
try:  # never let env loading break the bridge
    from core.env_file import load_env_file as _load_env_file
    _load_env_file()
except Exception:
    pass

from core.intraday.distribution_adapter import from_ws_p, update_from_intraday  # noqa: E402
from core.intraday.entry_engine import EntryTimingEngine  # noqa: E402
from core.intraday.timing_overlay_gate import (  # noqa: E402
    evaluate_with_overlay, live_overlay_signal, overlay_enabled,
)
from core.intraday.analog_cdf import _load_series  # noqa: E402
from core.strike_selection_ev import select_strike, p_high_ge as gauss_p_high_ge  # noqa: E402
from core.intraday.multi_forecaster_fusion import (  # noqa: E402
    TrajectoryForecaster, build_live_epoch, build_analog_cdf_today,
    fuse_strike, ensure_schema, FUSION_RULE_ID,
)
from core.station_time import (  # noqa: E402
    is_within_entry_window, station_timezone_name,
)
from core.market_cost_model import MARKET_COST_MODEL  # noqa: E402

DATA = REPO / "data"
SBOX_DB = DATA / "paper_trading_sbox.db"
WS_P = DATA / "ws_p_distribution.json"
NWP = DATA / "nwp_forecasts.db"   # CLEAN GEFS source
METAR = DATA / "metar_backfill.db"
SNAPS = DATA / "kalshi_market_snapshots.db"
SETTLEMENTS = DATA / "kalshi_settlements.db"
INSTANCE = "SBOX"

STATIONS = ["KATL", "KAUS", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU", "KLAS",
            "KLAX", "KMDW", "KMIA", "KMSP", "KMSY", "KNYC", "KOKC", "KPHL",
            "KPHX", "KSAT", "KSEA", "KSFO"]

MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
          "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


def _ro(p):
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def _c2f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def local_day(station: str, now_utc: datetime):
    """(date_str, day_start_utc, day_end_utc) for the station's LOCAL weather day."""
    tz = ZoneInfo(station_timezone_name(station))
    local = now_utc.astimezone(tz)
    d = local.date()
    start = datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    return start.astimezone(tz).strftime("%Y-%m-%d"), start, end


def _dt_yy(d: str) -> str:
    y, m, dd = d.split("-")
    inv = {v: k for k, v in MONTHS.items()}
    return f"{y[2:]}{inv[int(m)]}{dd}"


def gefs_ens(station: str, target_date: str):
    """(ens_mean_f, ens_std_f) for the daily-max GEFS ensemble, or None."""
    c = _ro(NWP)
    try:
        rows = c.execute(
            "SELECT variable, value, fetch_date FROM nwp_forecasts "
            "WHERE model='gefs_ens' AND station=? AND target_date=? "
            "AND variable LIKE 'temperature_2m_max_member%'",
            (station, target_date)).fetchall()
    finally:
        c.close()
    latest = {}
    for var, val, fd in rows:
        if val is None:
            continue
        if var not in latest or fd > latest[var][0]:
            latest[var] = (fd, val)
    vals = [_c2f(v) for _, v in latest.values()]
    if len(vals) < 3:
        return None
    m = sum(vals) / len(vals)
    std = (sum((v - m) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5
    return (m, std)


def high_so_far(station: str, day_start: datetime, day_end: datetime,
                now_utc: datetime) -> Optional[float]:
    """Max observed temp over [day_start, min(now, day_end)). None if no obs yet."""
    hi_iso = min(now_utc, day_end).isoformat()
    c = _ro(METAR)
    try:
        row = c.execute(
            "SELECT MAX(temp_f) FROM metar_observations WHERE station=? "
            "AND timestamp_utc >= ? AND timestamp_utc < ?",
            (station, day_start.isoformat(), hi_iso)).fetchone()
    finally:
        c.close()
    return float(row[0]) if row and row[0] is not None else None


def strike_ladder(station: str, date_yy: str, before_iso: Optional[str] = None,
                  min_volume: float = 50.0) -> List[Dict]:
    """Threshold (-T) strikes with LIQUID mid prices for one station-day."""
    c = _ro(SNAPS)
    try:
        sql = ("SELECT market_ticker, mid_price, yes_ask, volume_24h FROM market_snapshots "
               "WHERE station=? AND market_ticker LIKE ? AND market_ticker LIKE 'KXHIGH%' "
               "AND mid_price IS NOT NULL")
        params = [station, f"%-{date_yy}-%"]
        if before_iso:
            sql += " AND snapshot_time < ?"
            params.append(before_iso)
        sql += " ORDER BY snapshot_time DESC"
        rows = c.execute(sql, params).fetchall()
    finally:
        c.close()
    seen = {}
    for tk, mid, ask, vol in rows:
        tail = tk.rsplit("-", 1)[-1]
        if not tail.startswith("T"):
            continue
        try:
            strike = float(tail[1:])
        except ValueError:
            continue
        if strike in seen:
            continue
        px = mid if mid is not None else ask
        if px is None:
            continue
        px = float(px)
        if px <= 0.02 or px >= 0.98:
            continue
        if (vol or 0) < min_volume:
            continue
        seen[strike] = {"strike": strike, "yes_price": px,
                        "no_price": round(1 - px, 4), "ticker": tk,
                        "volume_24h": vol}
    return sorted(seen.values(), key=lambda d: d["strike"])


def _upsert_forecast(conn, tuuid, station, day_str, strike, side, ticker, probs):
    """Insert-or-replace the companion per-trade forecaster row (idempotent)."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO trade_forecaster_probs
           (trade_uuid, station, trade_date_utc, strike, side, ticker,
            p_gefs, p_analog, p_trajectory, p_fused, n_analog, n_traj,
            agreement, edge_mult, traj_verdict, traj_mult, fusion_rule, created_at_utc)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(trade_uuid) DO UPDATE SET
             p_gefs=excluded.p_gefs, p_analog=excluded.p_analog,
             p_trajectory=excluded.p_trajectory, p_fused=excluded.p_fused,
             n_analog=excluded.n_analog, n_traj=excluded.n_traj,
             agreement=excluded.agreement, edge_mult=excluded.edge_mult,
             traj_verdict=excluded.traj_verdict, traj_mult=excluded.traj_mult,
             fusion_rule=excluded.fusion_rule, created_at_utc=excluded.created_at_utc""",
        (tuuid, station, day_str, strike, side, ticker,
         probs.get("p_gefs"), probs.get("p_analog"),
         probs.get("p_trajectory"), probs.get("p_fused"),
         probs.get("n_analog"), probs.get("n_traj"),
         probs.get("agreement"), probs.get("edge_mult"),
         probs.get("traj_verdict"), probs.get("traj_mult"),
         FUSION_RULE_ID, now))


def _position_size_usd(confidence: float) -> float:
    """Dan's sizing ladder (2026-10-01):
    - very high confidence (>0.85): $50 max
    - high confidence (>0.7): $25 max
    - medium confidence (>0.5): $10 max
    - below 0.5: $0 (no trade)
    """
    if confidence > 0.85:
        return 50.0
    elif confidence > 0.7:
        return 25.0
    elif confidence > 0.5:
        return 10.0
    return 0.0


def record_trade(conn, station, market_type, side, our_prob, market_price,
                 confidence, strike, ticker, size_usd, functionality, day_str,
                 probs: Optional[Dict] = None):
    """Record ONE SBOX trade per (station, day, functionality), idempotently.

    Extension (2026-09-30): the row also carries p_gefs/p_analog/p_trajectory/
    p_fused (additive columns) plus a companion row in trade_forecaster_probs.

    Idempotency / no-double-count rule: if a row already exists for
    (station, trade_date_utc, functionality) — e.g. an earlier single-forecaster
    bridge run already booked today's position — this pass does NOT insert a
    second trade. It ENRICHES the existing row in place with the multi-forecaster
    probabilities and refreshes the companion row (status="enriched"). The
    canonical `trades` columns and existing semantics are untouched.
    """
    probs = probs or {}
    existing = conn.execute(
        "SELECT trade_uuid FROM trades WHERE station=? AND trade_date_utc=? "
        "AND functionality=?",
        (station, day_str, functionality)).fetchone()
    if existing:
        tuuid = existing[0]
        conn.execute(
            "UPDATE trades SET p_gefs=?, p_analog=?, p_trajectory=?, p_fused=? "
            "WHERE trade_uuid=?",
            (probs.get("p_gefs"), probs.get("p_analog"),
             probs.get("p_trajectory"), probs.get("p_fused"), tuuid))
        _upsert_forecast(conn, tuuid, station, day_str, strike, side, ticker, probs)
        conn.commit()
        return {"status": "enriched", "station": station,
                "picked_strike": strike,
                "p_gefs": probs.get("p_gefs"), "p_analog": probs.get("p_analog"),
                "p_trajectory": probs.get("p_trajectory"), "p_fused": probs.get("p_fused"),
                "agreement": probs.get("agreement"), "traj_verdict": probs.get("traj_verdict")}
    now = datetime.now(timezone.utc).isoformat()
    price = float(market_price)
    qty = max(1, int(round(size_usd / max(price, 0.01))))
    cost = abs(qty * price)
    direction = "UP" if side == "BUY" else "DOWN"
    tuuid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO trades (trade_uuid, trade_date_utc, station, market_type,
           signal_direction, forecast_prob, market_price, trade_type, quantity,
           position_size_usd, trade_price, trade_cost, trade_version, functionality,
           status, notes, implied_prob, analytical_prob, calibration_error,
           confidence_indicator, created_at_utc,
           p_gefs, p_analog, p_trajectory, p_fused)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (tuuid, day_str, station, market_type, direction, our_prob, price,
         "buy_yes" if side == "BUY" else "sell_yes", qty, size_usd, price, cost,
         f"v3.1_{INSTANCE.lower()}", functionality, "open",
         f"multi-forecaster EV-strike {ticker} @ {strike}F; inst={INSTANCE}",
         price, our_prob, abs(our_prob - price), confidence, now,
         probs.get("p_gefs"), probs.get("p_analog"),
         probs.get("p_trajectory"), probs.get("p_fused")))
    _upsert_forecast(conn, tuuid, station, day_str, strike, side, ticker, probs)
    conn.commit()
    return {"status": "executed", "station": station, "signal_direction": direction,
            "strike": strike, "ticker": ticker, "our_prob": round(our_prob, 4),
            "market_price": price, "position_size_usd": round(size_usd, 2),
            "edge": round(our_prob - price, 4),
            "p_gefs": probs.get("p_gefs"), "p_analog": probs.get("p_analog"),
            "p_trajectory": probs.get("p_trajectory"), "p_fused": probs.get("p_fused"),
            "agreement": probs.get("agreement"), "traj_verdict": probs.get("traj_verdict")}


def build_fused_p_fn(state, cdf, traj: TrajectoryForecaster, hsf: Optional[float]):
    """Return (p_fn, meta_fn): per-strike x -> p_fused and the fusion meta dict."""
    cache: Dict[float, Dict] = {}

    def _calc(x: float) -> Dict:
        if x in cache:
            return cache[x]
        row = fuse_strike(state.p_ge, cdf.p_high_ge,
                          lambda xx: traj.p_high_ge(xx, hsf), x)
        cache[x] = row
        return row

    def p_fn(x: float) -> float:
        return _calc(x)["p_fused"]

    return p_fn, _calc


def run(dry_run=False, ignore_window=False, stations=None, min_edge=0.03,
        report=False, max_stations=None):
    stations = stations or STATIONS
    if max_stations:
        stations = stations[:max_stations]
    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()

    try:
        dist = json.load(open(WS_P))
    except Exception as e:
        return {"error": f"ws_p_distribution.json unreadable: {e}"}

    engine = EntryTimingEngine()
    conn = None if dry_run else sqlite3.connect(SBOX_DB)
    if conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        ensure_schema(conn)  # additive: p_* columns + companion table

    # ONE matcher/climatology for the whole pass (the climatology build is the
    # expensive part; constructing per-station rebuilt it every time).
    shared_traj = TrajectoryForecaster()

    results, skipped, diag = [], [], []
    for st in stations:
        day_str, day_start, day_end = local_day(st, now_utc)
        date_yy = _dt_yy(day_str)
        month = int(day_str[5:7])

        if not ignore_window:
            ok, reason = is_within_entry_window(st, day_str, now_utc=now_utc)
            if not ok:
                skipped.append((st, reason))
                continue

        ens = gefs_ens(st, day_str)
        if not ens:
            skipped.append((st, "no_gefs_step24"))
            continue
        hsf = high_so_far(st, day_start, day_end, now_utc)
        if hsf is None:
            skipped.append((st, "no_intraday_obs_yet"))
            continue

        frac = max(0.0, min(1.0, (now_utc - day_start).total_seconds()
                             / (day_end - day_start).total_seconds()))
        state = from_ws_p(dist, st, ens_mean=ens[0], ens_std=ens[1], month=month, date=day_str)
        state = update_from_intraday(state, hsf, frac)

        ladder = strike_ladder(st, date_yy, before_iso=now_iso)
        if not ladder:
            skipped.append((st, "no_strike_ladder"))
            continue

        # -- Item 5: independent analog CDF forecaster (extend series to today) --
        try:
            cdf = build_analog_cdf_today(st, day_str, high_so_far=hsf)
        except Exception as e:
            skipped.append((st, f"analog_cdf_error:{type(e).__name__}"))
            continue

        # -- Item 4: trajectory analog gate -> p_trajectory + directional verdict --
        try:
            q_epoch = build_live_epoch(st, day_start, day_end, now_utc)
            if q_epoch is None:
                traj = None
            else:
                traj = shared_traj.fit(q_epoch)
        except Exception as e:
            traj = None
            diag.append({"station": st, "traj_error": f"{type(e).__name__}:{e}"})
        if traj is None:
            skipped.append((st, "no_trajectory_query_epoch"))
            continue

        # -- fuse per strike; select on the FUSED probability --
        p_fn, meta_fn = build_fused_p_fn(state, cdf, traj, hsf)
        picked = select_strike(state.mu, state.sigma, ladder, min_edge=0.0, p_fn=p_fn)
        if not picked or picked.get("ev", 0) <= 0:
            skipped.append((st, "no_positive_ev_strike"))
            continue
        strike = picked["strike"]
        meta = meta_fn(strike)
        # Item 5: wider required edge when forecasters disagree.
        if picked["ev"] <= min_edge * meta["edge_mult"]:
            skipped.append((st, f"edge_below_scaled_min:{meta['agreement']}"))
            continue

        row = next((s for s in ladder if abs(s["strike"] - strike) < 1e-9), None)
        if row is None:
            skipped.append((st, "no_real_price_for_picked_strike"))
            continue

        p_gefs = state.p_ge(strike)
        both = dict(meta)
        both.update({"p_gefs": meta["p_gefs"], "p_analog": meta["p_analog"],
                     "p_trajectory": meta["p_trajectory"],
                     "n_analog": cdf.n_analogs, "n_traj": traj.n_analogs})
        fused_at_strike = meta["p_fused"]
        our_prob = fused_at_strike if picked["side"] == "yes" else 1.0 - fused_at_strike
        mkt = row["yes_price"] if picked["side"] == "yes" else row["no_price"]
        ticker = row["ticker"]

        # directional gate verdict on the trade side (Item 4 evidence).
        # Our proposition is P(high>=strike): YES side = we expect the high to
        # reach/exceed the strike -> trajectory direction "up"; NO -> "down".
        gate_dir = "up" if picked["side"] == "yes" else "down"
        verdict = traj.gate_verdict(gate_dir, abs(our_prob - 0.5) * 2.0)

        diag.append({"station": st, "day": day_str,
                     "ens": [round(ens[0], 2), round(ens[1], 2)], "hsf": hsf,
                     "frac": round(frac, 3), "mu": round(state.mu, 2),
                     "sigma": round(state.sigma, 2), "strike": strike,
                     "side": picked["side"], "ev": picked["ev"],
                     "our_prob": round(our_prob, 4), "mkt": mkt,
                     "p_gefs": both["p_gefs"], "p_analog": both["p_analog"],
                     "p_trajectory": both["p_trajectory"], "p_fused": both["p_fused"],
                     "agreement": both["agreement"], "traj_verdict": both["traj_verdict"],
                     "n_analog": cdf.n_analogs, "n_traj": traj.n_analogs,
                     "gate": verdict["verdict"], "p_warm": verdict.get("p_warm"),
                     "n_strikes": len(ladder)})
        if report:
            continue

        # 15m timing overlay (opt-in, default OFF via WE_15M_TIMING_OVERLAY=1).
        # When a live trigger agrees with the engine direction, its confidence is
        # folded 50/50 into the engine's confidence argument; silent/disagreeing
        # overlays leave the engine decision unchanged (plan §5).
        engine_conf = abs(our_prob - 0.5) * 2.0
        overlay_sig = None
        if overlay_enabled():
            try:
                overlay_sig = live_overlay_signal(st, day_str, strike)
            except Exception as e:
                diag.append({"station": st,
                             "overlay_error": f"{type(e).__name__}:{e}"})
        sig = evaluate_with_overlay(
            engine, hour=now_utc.hour, hour_before_settlement=6,
            our_probability=our_prob, market_price=mkt,
            confidence=engine_conf, overlay_signal=overlay_sig,
            enabled=True,
        )
        if sig is None or sig.signal_type.name == "PASS":
            skipped.append((st, "entry_engine_pass"))
            continue
        if overlay_sig is not None:
            diag.append({"station": st, "overlay_trigger": overlay_sig.trigger,
                         "overlay_dir": overlay_sig.direction,
                         "overlay_conf": round(overlay_sig.confidence, 4)})

        # R/T (2026-10-01): Dan's sizing ladder replaces the old EntryTimingEngine
        # position_size, which was a fixed $100. Use our_prob-based ladder instead.
        confidence = abs(our_prob - 0.5) * 2.0
        size_usd = _position_size_usd(confidence)
        # Second half of A5 (2026-10-02): a $0 ladder tier means NO trade. Without
        # this guard a $0 size still records a phantom row (qty floored to 1).
        # The ladder is the only sizing path; below the lowest tier we pass.
        if size_usd <= 0:
            skipped.append((st, "below_min_confidence_tier"))
            continue

        prob_payload = {"p_gefs": both["p_gefs"], "p_analog": both["p_analog"],
                        "p_trajectory": both["p_trajectory"], "p_fused": both["p_fused"],
                        "n_analog": cdf.n_analogs, "n_traj": traj.n_analogs,
                        "agreement": both["agreement"], "edge_mult": both["edge_mult"],
                        "traj_verdict": both["traj_verdict"], "traj_mult": both["traj_mult"]}

        if dry_run:
            results.append({"status": "would_execute", "station": st,
                            "side": sig.signal_type.name, "strike": strike,
                            "our_prob": round(our_prob, 4), "market_price": mkt,
                            "size": size_usd,
                            **prob_payload})
        else:
            results.append(record_trade(
                conn, st, "HIGH", sig.signal_type.name, our_prob, mkt,
                abs(our_prob - 0.5) * 2.0, strike, ticker,
                size_usd, "intraday_ev_strike", day_str, probs=prob_payload))

    if conn:
        conn.close()
    out = {"date_utc": now_utc.strftime("%Y-%m-%d"), "n_executed": len(results),
           "n_skipped": len(skipped), "executed": results, "skipped": skipped[:25]}
    if report:
        out["diagnostics"] = diag
    return out


# =============================================================================
# Item 8 — combined trade + analytics report
# =============================================================================
def _crps_gauss(mu: float, sigma: float, y: float) -> float:
    """Closed-form CRPS of N(mu,sigma) vs scalar y."""
    if sigma <= 0:
        return abs(mu - y)
    z = (y - mu) / sigma
    from math import erf, exp, pi, sqrt
    phi = exp(-0.5 * z * z) / sqrt(2 * pi)
    Phi = 0.5 * (1 + erf(z / sqrt(2)))
    return sigma * (z * (2 * Phi - 1) + 2 * phi - 1 / sqrt(pi))


def pure_analytics_report(stations=None, n_days=20, dist=None) -> Dict:
    """Pure weather analytics: CRPS vs settlement + resolution for both
    distributional forecasters over the most recent settled station-days."""
    from core.intraday.multi_forecaster_fusion import build_analog_cdf_today
    stations = stations or STATIONS
    dist = dist or json.load(open(WS_P))

    # settlement truth: {station: {date: temp_f}}
    c = _ro(SETTLEMENTS)
    try:
        rows = c.execute(
            "SELECT station, target_date, kalshi_temp FROM kalshi_settlements "
            "WHERE kalshi_temp IS NOT NULL").fetchall()
    finally:
        c.close()
    truth: Dict[str, Dict[str, float]] = {}
    for st, d, t in rows:
        truth.setdefault(st, {})[d] = float(t)

    per_station = {}
    crps_analog, crps_gefs, n = [], [], 0
    for st in stations:
        tmap = truth.get(st, {})
        if not tmap:
            continue
        dates = sorted(tmap)[-n_days:]
        a_list, g_list = [], []
        for d in dates:
            y = tmap[d]
            try:
                cdf = build_analog_cdf_today(st, d)
                ca = cdf.crps(y)
            except Exception:
                ca = None
            # GEFS curve: seed from WS-P + ensemble for that date if available
            cg = None
            ens = gefs_ens(st, d)
            if ens:
                fit = dist["fit"]
                bias = fit["per_station_bias_F"].get(st, 0.0)
                off = fit["global_offset_F"]
                mb = fit.get("month_bias_F", {}).get(str(int(d[5:7])), 0.0)
                sigma = max(0.8, fit["spread_a0_F"] + fit["spread_a1"] * ens[1])
                cg = _crps_gauss(ens[0] + bias + off + mb, sigma, y)
            if ca is not None and ca == ca:
                crps_analog.append(ca)
                a_list.append(ca)
            if cg is not None and cg == cg:
                crps_gefs.append(cg)
                g_list.append(cg)
            if ca is not None or cg is not None:
                n += 1
        per_station[st] = {
            "n": len(dates),
            "crps_analog": round(sum(a_list) / len(a_list), 4) if a_list else None,
            "crps_gefs": round(sum(g_list) / len(g_list), 4) if g_list else None,
        }
    return {
        "windows": n,
        "crps_analog_mean": round(sum(crps_analog) / len(crps_analog), 4) if crps_analog else None,
        "crps_gefs_mean": round(sum(crps_gefs) / len(crps_gefs), 4) if crps_gefs else None,
        "per_station": per_station,
    }


def trade_results_report() -> Dict:
    """Trade results from SBOX rows carrying p_* columns."""
    c = _ro(SBOX_DB)
    try:
        cols = {d[1] for d in c.execute("PRAGMA table_info(trades)")}
        has = all(k in cols for k in ("p_gefs", "p_analog", "p_trajectory", "p_fused"))
        if not has:
            return {"error": "p_* columns absent (run a recording pass first)"}
        rows = c.execute(
            "SELECT station, trade_date_utc, signal_direction, "
            "forecast_prob, market_price, "
            "p_gefs, p_analog, p_trajectory, p_fused, "
            "status, settled_value, realized_pnl "
            "FROM trades WHERE p_fused IS NOT NULL").fetchall()
    except Exception as e:
        return {"error": str(e)}
    finally:
        c.close()
    n = len(rows)
    if n == 0:
        return {"n": 0, "note": "no trades carry p_* yet"}
    # settled when realized_pnl is populated
    acc = [r for r in rows if r[11] is not None]
    settled = len(acc)
    wins = sum(1 for r in acc if (r[11] or 0) > 0)
    pnl = sum((r[11] or 0) for r in acc)
    # ECE over settled trades: p_yes = p_fused for UP, 1-p_fused for DOWN;
    # outcome y = 1 if settled_value win (use realized_pnl>0 as win proxy).
    ece = None
    if settled:
        bins = [[] for _ in range(10)]
        for r in acc:
            direction, p_fused, rp = r[2], r[8], r[11]
            if p_fused is None:
                continue
            p_yes = p_fused if direction == "UP" else 1.0 - p_fused
            b = min(9, int(p_yes * 10))
            bins[b].append((p_yes, 1.0 if (rp or 0) > 0 else 0.0))
        tot = sum(len(b) for b in bins)
        if tot:
            ece = 0.0
            for b in bins:
                if not b:
                    continue
                mp = sum(x[0] for x in b) / len(b)
                my = sum(x[1] for x in b) / len(b)
                ece += (len(b) / tot) * abs(mp - my)
    # mean agreement mix
    agrees = {}
    for r in rows:
        key = ("agree" if abs((r[5] or 0) - (r[6] or 0)) <= 0.10 else
               "split" if abs((r[5] or 0) - (r[6] or 0)) <= 0.20 else "disagree")
        agrees[key] = agrees.get(key, 0) + 1
    return {
        "n_trades_with_probs": n,
        "n_settled": settled,
        "settled_win_rate": round(wins / settled, 4) if settled else None,
        "realized_pnl": round(pnl, 2),
        "ece": round(ece, 4) if ece is not None else None,
        "agreement_mix": agrees,
        "mean_p_gefs": round(sum(r[5] for r in rows if r[5] is not None) / n, 4),
        "mean_p_analog": round(sum(r[6] for r in rows if r[6] is not None) / n, 4),
        "mean_p_trajectory": round(sum(r[7] for r in rows if r[7] is not None) / n, 4),
        "mean_p_fused": round(sum(r[8] for r in rows if r[8] is not None) / n, 4),
    }


def combined_report(stations=None) -> Dict:
    return {"trade_results": trade_results_report(),
            "pure_weather_analytics": pure_analytics_report(stations=stations)}


# =============================================================================
# E1/E3 (2026-10-02) — SBOX trade-fail / zero-trade notification path
# =============================================================================
def _notify_bridge_result(out: Dict) -> Optional[Dict]:
    """Ping the SBOX Discord webhook when the bridge runs but places 0 trades.

    Reuses the existing alert_dispatcher (DISCORD_WEBHOOK_SBOX). No-ops safely
    when no webhook is configured, so it never breaks a cron run. Returns the
    dispatch result (or None when nothing was sent).
    """
    if out.get("error"):
        reason = f"bridge error: {out['error']}"
    elif out.get("n_executed", 0) == 0:
        reason = (f"bridge ran but placed 0 trades "
                  f"({out.get('n_skipped', 0)} skipped)")
    else:
        return None
    try:
        from core.alert_dispatcher import dispatch_alert  # noqa: E402
        content = (f"**[SBOX] Weather Engine bridge ATTENTION** — {reason}\n"
                   f"date_utc={out.get('date_utc')}")
        result = dispatch_alert(
            alert_data={"content": content},
            discord_payload={"content": content},
            instance="SBOX",
        )
        return result
    except Exception as e:  # never let alerting break the bridge
        print(f"[warn] bridge notify failed: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--ignore-window", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--combined-report", action="store_true")
    ap.add_argument("--stations", nargs="*", default=None)
    ap.add_argument("--min-edge", type=float, default=0.03)
    ap.add_argument("--max-stations", type=int, default=None)
    a = ap.parse_args()
    if a.combined_report:
        print(json.dumps(combined_report(a.stations), indent=2, default=str))
        return 0
    out = run(dry_run=a.dry_run, ignore_window=a.ignore_window,
              stations=a.stations, min_edge=a.min_edge, report=a.report,
              max_stations=a.max_stations)
    if not a.dry_run:
        note = _notify_bridge_result(out)
        if note is not None:
            out["notify"] = note
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
