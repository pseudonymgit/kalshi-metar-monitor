#!/usr/bin/env python3
"""
SBOX Multi-Forecaster Intraday EV Bridge (2026-09-30)
=====================================================
Extends the Item-1 bridge (scripts/sbox_intraday_ev_bridge.py).

DEFAULT (Workstream AF, 2026-10-14) — SOLE om_gfs:
Behind USE_OM_GFS_NWP the forecast is the SINGLE bias-corrected om_gfs
DistributionState `build_om_gfs_sole_p_fn`; the analog CDF and trajectory gate
are NOT run. The sole path additionally FAILS CLOSED when the NWP data source is
not om_gfs (PWW-2026-10-15): if `select_nwp_state` fell through to the raw local
GFS (`nwp_src == "gfs_local"`) the station-day is skipped with
`sole_no_om_gfs` rather than booked under om_gfs provenance.

ROLLBACK PATH (flag OFF) — three-forecaster fusion:

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

Both paths are deterministic, no AI/ML. SBOX only — refuses to touch DEV/PROD.

Fusion rule (rollback path only — see
docs/plans/SBOX-MULTI-FORECASTER-BRIDGE-2026-09-30.md):
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
import os
import re
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
from core.intraday.exit_logic import evaluate_exits  # noqa: E402
from core.strike_selection_ev import select_strike, p_high_ge as gauss_p_high_ge  # noqa: E402
from core.intraday.multi_forecaster_fusion import (  # noqa: E402
    TrajectoryForecaster, build_live_epoch, build_analog_cdf_today,
    fuse_strike, ensure_schema, ensure_station_weights_defaults, load_weights,
    grade_from_confidence, DEFAULT_WEIGHTS, FUSION_RULE_ID,
)
# Honest decision-time recalibration (2026-10-14) — the EV gate must price off a
# CALIBRATED probability, not the raw fusion output. Single source of the
# formula, shared with scripts/recalibrate_companion.py.
from core.intraday.honest_calibration import (  # noqa: E402
    honest_calibrated_p, station_ceiling, POOLED_CEILING,
)

# ── om_gfs NWP substitution (USE_OM_GFS_NWP), 2026-10-13 ─────────────────────
# The om_gfs strike-crossing validation found Open-Meteo's bias-corrected GFS is
# a strictly better NWP input than our raw local GFS (level MAE 0.680 F vs
# 2.095 F; crossing Brier 0.1079 vs 0.1597) but is COLLINEAR with rate_1h
# (fcerr 0.99) — a LEVER-axis substitution, NOT a diversity gain. Behind the
# flag, the NWP component uses om_gfs(bias-corrected); off (default) it is the
# raw local GFS-seeded WS-P distribution, byte-identical to pre-flag behaviour.
# Raw local GFS is reachable ONLY when the sole-forecast flag is OFF. When the
# sole flag is ON (om_gfs IS the forecast) a missing/unavailable om_gfs makes
# `select_nwp_state` fall through to nwp_src == "gfs_local", which the sole gate
# below FAILS CLOSED on (`sole_no_om_gfs`) — the station-day is skipped, never
# booked under om_gfs provenance. It is NOT a fallback on the sole path.
# Import is guarded so a substrate import failure can never break the bridge.
try:
    from core.intraday.om_gfs_nwp import (
        om_gfs_enabled, om_gfs_fusion_enabled, om_gfs_state_for_day,
    )
    _OM_GFS_IMPORTED = True
except Exception:  # pragma: no cover - module missing => substitution unavailable
    _OM_GFS_IMPORTED = False


# ── Workstream AF (2026-10-14): om_gfs is the SOLE forecast ──────────────────
# Every analysis this week (fusion-weight sweep, 3-combiner comparison, LOW
# side-by-side) concluded the same thing: om_gfs(bc,ps) Tmax is the best single
# predictor (Brier 0.107) and every fusion variant loses to it. The GEFS/analog/
# trajectory/market ensemble is dead weight, so behind the NWP-substitution flag
# the bridge now forecasts on the single om_gfs value. The fusion machinery
# below stays importable and intact — re-enabling it is a one-line change — but
# it is NOT on the default path.
def sole_forecast_enabled() -> bool:
    """True when the bridge should forecast on om_gfs ALONE (Workstream AF).

    Keyed on USE_OM_GFS_NWP: when the om_gfs NWP substitution is on, om_gfs IS
    the sole forecast. When it is off (or the module is unavailable) the bridge
    falls back to the legacy multi-forecaster fusion — the rollback path.
    """
    return bool(_OM_GFS_IMPORTED and om_gfs_enabled())
from core.station_time import (  # noqa: E402
    is_within_entry_window, station_timezone_name, settlement_time_utc,
)
from core.station_params import get_station_params  # noqa: E402  (F4)
from core.market_cost_model import MARKET_COST_MODEL  # noqa: E402
from core.fees import execution_cost_rate as _exec_cost_rate  # noqa: E402  (B&B-K)

# ── GR5 §2/§6 live-path wiring (2026-10-10) ──────────────────────────────────
# The measurement substrate (pre-ingest gate G0–G5 + TradeCustodyRecord) is
# imported here and CALLED by the record path, so it decides whether a trade is
# booked and gives every booked trade a custody row. Import is guarded so a
# substrate import failure can never break the bridge's other jobs. Set
# WE_BOOKING_GATE=0 to disable. Never applies to --dry-run (no booking happens).
try:
    from core.booking_admission import (
        admit_decision_candidate, record_custody,
    )
    _ADMISSION_IMPORTED = True
except Exception:  # pragma: no cover - substrate missing => gate off, bridge runs
    _ADMISSION_IMPORTED = False
ENABLE_BOOKING_GATE = (_ADMISSION_IMPORTED
                       and os.environ.get("WE_BOOKING_GATE", "1") != "0")

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


PRICE_SNAPSHOT_SOURCE = "kalshi_market_snapshots.db:market_snapshots.mid_price"

# H3 canonical decision pin: 13:30 station-local (see
# scripts/b2_real_price_sweep.DEFAULT_ENTRY_UTC and GR5-WIRING-RECEIPT).
DECISION_PIN_LOCAL_HHMM = "13:30:00"


def pinned_decision_utc(station: str, day_str: str) -> Optional[str]:
    """The 13:30 station-local decision pin for a day, as a UTC ISO string."""
    if ZoneInfo is None:
        return None
    try:
        tz = ZoneInfo(station_timezone_name(station))
        local = datetime.fromisoformat(f"{day_str}T{DECISION_PIN_LOCAL_HHMM}")
        return local.replace(tzinfo=tz).astimezone(timezone.utc).isoformat()
    except Exception:
        return None


def sole_pin_reached(station: str, day_str: str,
                     now_utc: datetime) -> Optional[bool]:
    """ITEM1 (2026-10-16, H3): has the 13:30 station-local decision pin passed?

    Returns True when `now_utc` is at/after the station-day's pin, False when the
    pin is still in the future, and None when the pin cannot be resolved (missing
    tz data / bad zone). The sole (om_gfs) path must NOT enter a station-day
    before its pin: the caller SKIPS on False and FAILS CLOSED (skip) on None, so
    a missing measurement can never silently default the decision instant to
    `now`. The pin encodes "when the day's trajectory has developed enough to
    decide" — it is the minimum-information instant, not a diagnostic.
    """
    pin_iso = pinned_decision_utc(station, day_str)
    if pin_iso is None:
        return None
    try:
        pin = datetime.fromisoformat(pin_iso)
    except Exception:
        return None
    if pin.tzinfo is None:
        pin = pin.replace(tzinfo=timezone.utc)
    return now_utc >= pin


def market_mid_prob(ticker: str, pin_iso: Optional[str]) -> Optional[float]:
    """Market mid as an implied P(YES) at the snapshot nearest the decision pin.

    Read-only on kalshi_market_snapshots.db. The market's implied probability of
    YES is the YES mid price; the fusion component uses ``1 - mid`` for the NO
    side downstream. Returns None when no usable snapshot exists (fail-open: the
    market component simply drops out of the weighted blend).
    """
    if not ticker or not pin_iso:
        return None
    c = _ro(SNAPS)
    try:
        # PWW Loop-2 (MAJOR, 2026-10-16): bound to pre-pin snapshots only.
        # The old ABS(nearest) ordering could pick a snapshot AFTER the pin,
        # leaking post-decision market information into the decision.
        row = c.execute(
            "SELECT mid_price FROM market_snapshots WHERE market_ticker=? "
            "AND mid_price IS NOT NULL "
            "AND snapshot_time <= ? "
            "ORDER BY snapshot_time DESC LIMIT 1",
            (ticker, pin_iso)).fetchone()
    finally:
        c.close()
    if not row or row[0] is None:
        return None
    p = float(row[0])
    return p if 0.0 <= p <= 1.0 else None


def strike_ladder(station: str, date_yy: str, before_iso: Optional[str] = None,
                  min_volume: float = 50.0) -> List[Dict]:
    """Threshold (-T) strikes with LIQUID mid prices for one station-day.

    Provenance (E1 fix 2026-10-05): every entry carries the `snapshot_time` it
    was priced from, and dedup-by-strike happens BEFORE the price/volume filters.
    The query is ORDER BY snapshot_time DESC, so the FIRST row seen per strike is
    the most recent snapshot at/before `before_iso`.

    Prior bug: dedup ran AFTER filtering. When a strike's latest price was
    filtered (px <= 0.02 / >= 0.98 / thin volume) the loop `continue`d WITHOUT
    marking the strike seen, so it kept scanning older snapshots and silently
    priced the strike from a stale row. Observed: KOKC trade 9b50b640 booked
    market_price=0.025 from snapshot 2026-10-04T14:30 while the decision-time
    snapshot (2026-10-05T13:30) was 0.005 — a 5x overstatement. Dedup-first
    makes the latest snapshot authoritative: if it is filtered, the strike is
    simply absent from the ladder (no trade at a stale price).
    """
    c = _ro(SNAPS)
    try:
        sql = ("SELECT market_ticker, mid_price, yes_ask, volume_24h, snapshot_time "
               "FROM market_snapshots "
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
    decided = set()  # strikes whose MOST-RECENT snapshot has been read
    for tk, mid, ask, vol, snap in rows:
        tail = tk.rsplit("-", 1)[-1]
        if not tail.startswith("T"):
            continue
        try:
            strike = float(tail[1:])
        except ValueError:
            continue
        if strike in decided:
            continue
        # First (newest) snapshot for this strike decides it — permanently.
        # If that price is filtered below, the strike is absent from the ladder;
        # we never fall back to an older snapshot's price.
        decided.add(strike)
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
                        "volume_24h": vol, "snapshot_time": snap}
    return sorted(seen.values(), key=lambda d: d["strike"])


def _station_spread_model(ladder: List[Dict]):
    """F1 (2026-10-13): one live spread probe per station-day for the EV cost model.

    The picker now nets EV of the modeled execution cost (mid + half-spread +
    slippage at entry). A full ladder is ~30 strikes; probing each ticker would
    issue hundreds of Kalshi HTTP calls per cron pass, so the spread is resolved
    ONCE per station from the highest-liquidity strike and applied to the station's
    whole ladder (spread is a station-day-level market property).

    Returns an object exposing ``get_dynamic_spread(ticker)`` for
    ``core.strike_selection_ev.select_strike``.

    ITEM 2 (2026-10-16): the returned model no longer MASKES a probe failure with
    an untagged fallback. It exposes:
      * ``resolution``       — ``'measured'`` | ``'unresolved'`` |
                               ``'policy_fallback_we_live_spread_ev_0'``;
      * ``fallback_used``    — True whenever the value is an ASSUMPTION, not a
                               measurement;
      * ``policy_selected``  — True only when the operator EXPLICITLY chose the
                               fallback via ``WE_LIVE_SPREAD_EV=0``.

    A fail-closed caller (``select_strike(..., fail_closed=True)``) rejects any
    returned value that is ``fallback_used`` and not ``policy_selected`` — so a
    failed/absent live probe SKIPS the station instead of silently pricing off
    the measured-mean assumption. The fail-open path (``fail_closed=False``)
    keeps the documented measured-mean fallback (=> ``EXECUTION_COST_RATE``).
    """
    slippage = float(getattr(MARKET_COST_MODEL, "slippage", 0.005) or 0.005)
    slip_rate = slippage
    live_enabled = os.environ.get("WE_LIVE_SPREAD_EV", "1") != "0"
    probe = None
    if live_enabled and ladder:
        best = max(ladder, key=lambda r: r.get("volume_24h") or 0)
        ticker = best.get("ticker")
        if ticker:
            try:
                # unresolved_on_failure=True -> an explicit None on API/probe
                # failure (instead of the masked measured-mean fallback).
                probe = MARKET_COST_MODEL.get_dynamic_spread(
                    ticker, unresolved_on_failure=True)
            except Exception as e:  # never break the pass on a live-data error
                print(f"[warn] live spread probe {ticker} failed: {type(e).__name__}: {e}",
                      file=sys.stderr)
                probe = None

    class _StationSpreadModel:
        slippage = slip_rate

        def __init__(self):
            # The probe is resolved ONCE per station, so the resolution state is
            # fixed at construction — `get_dynamic_spread` just replays it.
            if probe is not None and float(probe) > 0:
                self._spread = float(probe)
                self.resolution = "measured"
                self.fallback_used = False
                self.policy_selected = False
            else:
                self._spread = float(MARKET_COST_MODEL.spread)
                self.fallback_used = True
                if not live_enabled:
                    # The operator EXPLICITLY disabled the live probe: an
                    # allowed, SELECTED-AND-RECORDED fallback policy.
                    self.resolution = "policy_fallback_we_live_spread_ev_0"
                    self.policy_selected = True
                else:
                    # The live probe FAILED/unresolved under a fail-closed ask.
                    self.resolution = "unresolved"
                    self.policy_selected = False

        def get_dynamic_spread(self, ticker: str) -> Optional[float]:
            return self._spread

    return _StationSpreadModel()


def _ensure_grade_columns(conn) -> None:
    """F7 (2026-10-13): grade columns on the companion table (idempotent).

    ``grade_entered`` (S/A/B/C/D) and ``grade_cap_usd`` (the grade's position
    cap) let the book be analysed by opportunity grade without re-deriving it.
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='trade_forecaster_probs'").fetchone()
    if not exists:
        return
    cols = {d[1] for d in conn.execute("PRAGMA table_info(trade_forecaster_probs)")}
    if "grade_entered" not in cols:
        conn.execute("ALTER TABLE trade_forecaster_probs ADD COLUMN grade_entered TEXT")
    if "grade_cap_usd" not in cols:
        conn.execute("ALTER TABLE trade_forecaster_probs ADD COLUMN grade_cap_usd REAL")
    conn.commit()


# F7 (2026-10-13): grade -> position cap. F is not enterable (0 == no trade).
GRADE_POSITION_CAP_USD = {"S": 25.0, "A": 15.0, "B": 10.0, "C": 5.0}


def compute_trade_grade(calibrated_p: float, market_price: float) -> tuple:
    """F7: (grade_str, edge) from the opportunity-grade scale on edge alone.

    The SBOX bridge does not model a Sharpe ratio, so the grade reduces to its
    edge ladder (GRADE_THRESHOLDS edge_min: S>=0.12, A>=0.09, B>=0.06, C>=0.03,
    D>=0.01, else F). ``calibrated_p`` is our calibrated probability for the
    booked side (the bridge's fused/our_prob).
    """
    from core.alert_builder import compute_opportunity_grade  # local: heavy import
    grade, edge = compute_opportunity_grade(calibrated_p, market_price, 99.0)
    return grade.value, edge


def grade_position_cap(grade: str) -> float:
    """F7: position cap in USD for a grade; 0.0 for F/unknown (no entry)."""
    return GRADE_POSITION_CAP_USD.get(grade, 0.0)


# F9 (2026-10-13): portfolio exposure limits. The bridge admitted per-candidate
# but applied no per-station cap, max-concurrent, or total-daily-exposure check.
MAX_OPEN_PER_STATION = 3
MAX_OPEN_PER_DAY = 10


def exposure_skip_reason(conn, station: str, day_str: str) -> Optional[str]:
    """F9: return the limit name that blocks this entry, or None if admitted.

    Counts the live book's `status='open'` rows: >= MAX_OPEN_PER_STATION open for
    this station, or >= MAX_OPEN_PER_DAY open for this station-day, blocks a new
    entry. Read-only.
    """
    per_station = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE status='open' AND station=?",
        (station,)).fetchone()[0]
    if per_station >= MAX_OPEN_PER_STATION:
        return f"exposure_station_cap_{per_station}"
    per_day = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE status='open' AND trade_date_utc=?",
        (day_str,)).fetchone()[0]
    if per_day >= MAX_OPEN_PER_DAY:
        return f"exposure_day_cap_{per_day}"
    return None


def _upsert_forecast(conn, tuuid, station, day_str, strike, side, ticker, probs):
    """Insert-or-replace the companion per-trade forecaster row (idempotent).

    ITEM4.2 (2026-10-16): the companion row in ``trade_forecaster_probs`` IS the
    live calibration record — the booking path writes the calibrated_p/ceiling/
    composite_conf/grade here at booking time. ``trade_calibration`` is a RETIRED
    one-off table populated only by ``migrate_fusion_calibration_2026-10-13.py``;
    the bridge never writes it and it is left in place (a drop is destructive and
    record-integrity discipline forbids rewriting history). ``calibrated_at`` is
    now stamped whenever a calibrated_p is written, so new bookings are never
    NULL.
    """
    now = datetime.now(timezone.utc).isoformat()
    wa = probs.get("weights_applied")
    if not isinstance(wa, str):
        wa = json.dumps(wa) if wa is not None else None
    calibrated_at = now if probs.get("calibrated_p") is not None else None
    # ITEM4.1 (2026-10-16): the companion `grade` MUST be the GATE-TIME grade.
    # `grade_entered` is that grade (the edge-ladder letter that gated entry);
    # the fusion meta `grade` is a DIFFERENT ladder (grade_from_confidence on the
    # composite confidence) and produced grade='F' on 5 entered rows. Coerce at
    # the writer so the booked row and its companion can never disagree.
    grade = probs.get("grade_entered") or probs.get("grade")
    conn.execute(
        """INSERT INTO trade_forecaster_probs
           (trade_uuid, station, trade_date_utc, strike, side, ticker,
            p_gefs, p_analog, p_trajectory, p_fused, n_analog, n_traj,
            agreement, edge_mult, traj_verdict, traj_mult, fusion_rule, created_at_utc,
            grade_entered, grade_cap_usd,
            calibrated_p, p_market, composite_conf, grade, ceiling, weights_applied,
            calibrated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(trade_uuid) DO UPDATE SET
             p_gefs=excluded.p_gefs, p_analog=excluded.p_analog,
             p_trajectory=excluded.p_trajectory, p_fused=excluded.p_fused,
             n_analog=excluded.n_analog, n_traj=excluded.n_traj,
             agreement=excluded.agreement, edge_mult=excluded.edge_mult,
             traj_verdict=excluded.traj_verdict, traj_mult=excluded.traj_mult,
             fusion_rule=excluded.fusion_rule, created_at_utc=excluded.created_at_utc,
             grade_entered=excluded.grade_entered, grade_cap_usd=excluded.grade_cap_usd,
             calibrated_p=excluded.calibrated_p, p_market=excluded.p_market,
             composite_conf=excluded.composite_conf, grade=excluded.grade,
             ceiling=excluded.ceiling, weights_applied=excluded.weights_applied,
             calibrated_at=COALESCE(excluded.calibrated_at, calibrated_at)""",
        (tuuid, station, day_str, strike, side, ticker,
         probs.get("p_gefs"), probs.get("p_analog"),
         probs.get("p_trajectory"), probs.get("p_fused"),
         probs.get("n_analog"), probs.get("n_traj"),
         probs.get("agreement"), probs.get("edge_mult"),
         probs.get("traj_verdict"), probs.get("traj_mult"),
         (probs.get("fusion_rule") or FUSION_RULE_ID), now,
         probs.get("grade_entered"), probs.get("grade_cap_usd"),
         probs.get("calibrated_p"), probs.get("p_market"),
         probs.get("composite_conf"), grade,
         probs.get("ceiling"), wa, calibrated_at))


# ── Kelly sizing (Workstream AF, 2026-10-14) ────────────────────────────────
# The retired ladder posed `quantity = position_size_usd / trade_price`, which
# produced absurd contract counts on cheap entries ($0.05 -> 200 contracts ->
# +$190 win vs -$10 loss, a 19:1 asymmetry on a binary option). Sizing is now
# standard binary Kelly on the CALIBRATED probability vs the price paid:
#
#     f* = (p - market_price) / market_price        (== edge / market_price)
#     position_size_usd = min(bankroll * f*, max_position)
#
# with f* additionally capped at MAX_KELLY_FRACTION of the bankroll, so the
# loss side (market_price * f* * bankroll) is ALWAYS <= the fraction cap — never
# the $190 asymmetry. `bankroll=200`, `MAX_KELLY_FRACTION=0.05` ($10) and
# `MAX_POSITION_USD=10` reproduce Dan's $200/$10 book.
BANKROLL_USD = 200.0
MAX_KELLY_FRACTION = 0.05     # 5% of bankroll per trade ($10 on a $200 book)
MAX_POSITION_USD = 10.0       # hard per-trade cap (Dan 2026-10-13: max $10)


def kelly_fraction(our_prob: float, market_price: float, *,
                   fee_rate: float = 0.0) -> float:
    """Standard binary Kelly fraction for a $1-payoff contract bought at mid.

    ``f* = (p - market_price - fee_rate) / market_price``, clamped to >= 0.
    ``our_prob`` is the CALIBRATED probability of the BOOKED side winning;
    ``market_price`` is that side's entry price. A non-positive edge yields 0.0
    (no trade). Never raises; a degenerate price (<=0 or >=1) yields 0.0.
    """
    p = float(our_prob)
    m = float(market_price)
    # PWW Loop-3 (MAJOR, 2026-10-16): reject non-finite inputs. NaN produced
    # NaN and +inf produced inf, and the downstream `size_usd <= 0` guard does
    # not reject NaN — so a non-finite probability could pass sizing and create
    # malformed/over-sized trade state.
    import math as _math
    if not _math.isfinite(p) or not _math.isfinite(m) or not _math.isfinite(float(fee_rate)):
        return 0.0
    if not (0.0 < m < 1.0):
        return 0.0
    edge = p - m - float(fee_rate)
    if edge <= 0.0:
        return 0.0
    return edge / m


def kelly_position_size(our_prob: float, market_price: float, *,
                        bankroll: float = BANKROLL_USD,
                        max_fraction: float = MAX_KELLY_FRACTION,
                        max_position: float = MAX_POSITION_USD,
                        fee_rate: float = 0.0) -> float:
    """Kelly-sized position in USD: ``min(bankroll * min(f*, cap), max_position)``.

    Returns 0.0 when the calibrated probability carries no edge at the price.
    """
    f = kelly_fraction(our_prob, market_price, fee_rate=fee_rate)
    if f <= 0.0:
        return 0.0
    f = min(f, float(max_fraction))
    return round(min(float(bankroll) * f, float(max_position)), 2)


def _position_size_usd(our_prob: float, market_price: float, **kw) -> float:
    """Bridge sizing entry point — Kelly (Workstream AF, 2026-10-14).

    Replaces the retired confidence ladder entirely. ``our_prob`` is the
    calibrated probability of the booked side, ``market_price`` its entry price.
    """
    return kelly_position_size(our_prob, market_price, **kw)


def _ensure_price_source_column(conn) -> None:
    """Additive provenance column (E1, 2026-10-05): trade_uuid -> snapshot row.

    Idempotent; existing rows keep NULL until a reconciliation backfill runs.
    """
    cols = {d[1] for d in conn.execute("PRAGMA table_info(trades)")}
    if "price_source" not in cols:
        conn.execute("ALTER TABLE trades ADD COLUMN price_source TEXT")
        conn.commit()


def _ensure_lane_column(conn) -> None:
    """Workstream AF (2026-10-14): additive `lane` column so HIGH and LOW books
    co-exist in one table. Pre-existing rows are HIGH (the only lane until now).
    Idempotent.
    """
    cols = {d[1] for d in conn.execute("PRAGMA table_info(trades)")}
    if "lane" not in cols:
        conn.execute("ALTER TABLE trades ADD COLUMN lane TEXT")
        conn.execute("UPDATE trades SET lane='HIGH' WHERE lane IS NULL")
        conn.commit()


# ── trade uniqueness (E4-b, 2026-10-11) ──────────────────────────────────────
# The SBOX `trades` DDL only carried `trade_uuid UNIQUE`. A second insert for the
# same decision intent (double-down, cron re-run) therefore multi-booked silently
# because each new row drew a fresh uuid4. The decision identity here is
# `(station, market_ticker, decision instant)` — the SBOX book has no
# `market_ticker` column, so the ticker is recovered from the `<functionality>`
# provenance (`... <ticker> @ <strike>F; inst=...`) with a stable fallback.
# `decision instant` = `created_at_utc`, which is deterministic within one bridge
# pass (the replay/live writer builds it once per run). A backfill stamps the
# legacy rows' ticker as `legacy:<functionality>` so the partial UNIQUE index
# always builds over pre-existing data without mutating real column semantics.
UNIQUE_INTENT_INDEX = "ux_trades_station_ticker_decision"


def _covered_ticker(blob: Optional[str]) -> Optional[str]:
    """Recover the market_ticker encoded in a provenance string.

    e.g. 'multi-forecaster EV-strike KXHIGHNY-26OCT04-B77 @ 77F; inst=SBOX'
    -> 'KXHIGHNY-26OCT04-B77'. Returns None when no ticker-shaped token exists.
    """
    if not blob:
        return None
    m = re.search(r'(KX[A-Z0-9]+-[A-Z0-9]+-[A-Z0-9]+)', blob)
    return m.group(1) if m else None


def _ensure_trade_uniqueness(conn) -> None:
    """Idempotent (E4-b): add the row-identity uniqueness constraint on `trades`.

    Adds a `market_ticker` provenance column (additive; matches the custody
    record's identity facets), backfills it from the row's provenance string so
    the UNIQUE index builds over legacy rows, then creates a partial UNIQUE index
    on (station, market_ticker, created_at_utc) WHERE market_ticker IS NOT NULL.
    A duplicate decision intent is refused by the DB, not merely by an
    application-level get-then-insert race.

    Legacy rows whose ticker cannot be recovered get a stable
    `legacy:<functionality>` placeholder so the index remains valid; those rows
    predate the constraint and cannot collide with a new intent (whose ticker is
    a real KX ticker).
    """
    cols = {d[1] for d in conn.execute("PRAGMA table_info(trades)")}
    if "market_ticker" not in cols:
        conn.execute("ALTER TABLE trades ADD COLUMN market_ticker TEXT")
    has_notes = "notes" in cols
    sel = ("SELECT trade_uuid, functionality, notes FROM trades "
           "WHERE market_ticker IS NULL" if has_notes else
           "SELECT trade_uuid, functionality, NULL FROM trades "
           "WHERE market_ticker IS NULL")
    for tuuid, functionality, notes in conn.execute(sel).fetchall():
        tk = (_covered_ticker(notes) or _covered_ticker(functionality)
              or f"legacy:{functionality}")
        conn.execute("UPDATE trades SET market_ticker=? WHERE trade_uuid=?",
                     (tk, tuuid))
    conn.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {UNIQUE_INTENT_INDEX} "
        f"ON trades(station, market_ticker, created_at_utc) "
        f"WHERE market_ticker IS NOT NULL")
    conn.commit()


def _fetch_trade_dict(conn, tuuid):
    """Return the booked trades row as a dict (provenance for the TCR writer)."""
    cur = conn.execute("SELECT * FROM trades WHERE trade_uuid=?", (tuuid,))
    cols = [d[0] for d in cur.description]
    row = cur.fetchone()
    return dict(zip(cols, row)) if row else None


def _booked_side_prob(calibrated_p, leg):
    """ITEM2' (2026-10-16): booked-side probability from the YES-side calibrated
    value. YES leg: ``calibrated_p``; NO leg: ``1 - calibrated_p``. None/NaN in
    => None out, so the caller leaves the columns untouched.
    """
    if calibrated_p is None or calibrated_p != calibrated_p:
        return None
    return float(calibrated_p) if leg == "yes" else 1.0 - float(calibrated_p)


def _booked_leg_from_trade_type(trade_type):
    """Map a booked ``trade_type`` ('buy_yes'/'buy_no'/...) to its leg."""
    t = (trade_type or "").lower()
    if "yes" in t:
        return "yes"
    if "no" in t:
        return "no"
    return None


def _canonical_booked_prob(conn, tuuid):
    """The SINGLE source of truth for the booked probability: the companion's
    YES-side ``calibrated_p`` transformed by the row's OWN booked leg. None when
    either the companion row or its ``calibrated_p`` is absent.
    """
    row = conn.execute(
        "SELECT t.trade_type, f.calibrated_p FROM trades t "
        "LEFT JOIN trade_forecaster_probs f ON f.trade_uuid = t.trade_uuid "
        "WHERE t.trade_uuid = ?", (tuuid,)).fetchone()
    if not row:
        return None
    return _booked_side_prob(row[1], _booked_leg_from_trade_type(row[0]))


def _write_booked_prob(conn, tuuid, prob):
    """The single writer of ``trades.forecast_prob``/``analytical_prob``.

    ``calibration_error`` is recomputed against the row's OWN booked price
    (never a re-fetched market mid) so the error stays tied to the fill;
    ``implied_prob`` is back-filled once (COALESCE).
    """
    if prob is None or prob != prob:
        return
    row = conn.execute(
        "SELECT market_price FROM trades WHERE trade_uuid=?", (tuuid,)).fetchone()
    mkt = row[0] if row and row[0] is not None else None
    err = abs(float(prob) - float(mkt)) if mkt is not None else None
    conn.execute(
        "UPDATE trades SET forecast_prob=?, analytical_prob=?, "
        "implied_prob=COALESCE(implied_prob, ?), calibration_error=? "
        "WHERE trade_uuid=?",
        (float(prob), float(prob), mkt, err, tuuid))


def _sync_booked_prob(conn, tuuid, our_prob=None):
    """ITEM2' (2026-10-16): canonicalise the BOOKED probability on a re-stamp.

    The booked ``forecast_prob``/``analytical_prob`` are a DERIVED quantity: the
    companion's YES-side ``calibrated_p`` transformed by the row's OWN booked leg
    (YES: ``calibrated_p``; NO: ``1 - calibrated_p``). Writing a caller-supplied
    value directly let the two sides drift (ids 285/287/288): a 15:15 companion
    re-stamp moved ``calibrated_p`` while the booked column stayed at the stale
    14:15 value, so booked != companion and the scoring/calibrating paths read a
    stale probability.

    The companion is now the SINGLE source of truth — this writer DERIVES the
    booked prob from it, so no caller can inject a raw or stale value. ``our_prob``
    is only a fallback for rows/callers that carry no companion ``calibrated_p``
    (legacy books). The caller invokes this in the SAME transaction as
    ``_upsert_forecast``, so a re-stamp stays atomic on both sides.

    Invariant (both sides): ``abs(booked - (cal if YES else 1-cal)) < 1e-6``.
    """
    prob = _canonical_booked_prob(conn, tuuid)
    if prob is None:
        prob = our_prob
    _write_booked_prob(conn, tuuid, prob)


def record_trade(conn, station, market_type, side, our_prob, market_price,
                 confidence, strike, ticker, size_usd, functionality, day_str,
                 probs: Optional[Dict] = None, price_source: Optional[str] = None,
                 selected_side: Optional[str] = None, lane: str = "HIGH"):
    """Record ONE SBOX trade per (station, day, functionality), idempotently.

    Extension (2026-09-30): the row also carries p_gefs/p_analog/p_trajectory/
    p_fused (additive columns) plus a companion row in trade_forecaster_probs.

    Idempotency / no-double-count rule: if a row already exists for
    (station, trade_date_utc, functionality) — e.g. an earlier single-forecaster
    bridge run already booked today's position — this pass does NOT insert a
    second trade. It ENRICHES the existing row in place with the multi-forecaster
    probabilities and refreshes the companion row (status="enriched"). The
    canonical `trades` columns and existing semantics are untouched.

    E4-b (2026-10-11): the book now carries a row-identity uniqueness constraint
    `(station, market_ticker, created_at_utc)`. The stamp is deterministic within
    one bridge pass, so a cron re-run/partial re-invoke is excluded by the
    DB-level constraint rather than relying only on the (race-prone)
    get-then-insert lookup above. The lookup remains the fast path; the index is
    the backstop.
    """
    probs = probs or {}
    _ensure_price_source_column(conn)
    _ensure_lane_column(conn)  # AF: HIGH/LOW co-existence
    _ensure_trade_uniqueness(conn)
    # Canonical additive companion schema: calibrated_p/p_market/composite_conf/
    # grade/ceiling/weights_applied/grade_entered/grade_cap_usd/calibrated_at.
    # The booking path writes those columns (see _upsert_forecast), so the schema
    # path MUST create them too — otherwise a pre-existing companion table without
    # them (or a fresh one) rejects the INSERT. Idempotent + additive.
    ensure_schema(conn)
    _ensure_grade_columns(conn)  # F7: companion-table grade columns (idempotent)
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
        if price_source:
            conn.execute(
                "UPDATE trades SET price_source=COALESCE(price_source, ?) "
                "WHERE trade_uuid=?", (price_source, tuuid))
        _upsert_forecast(conn, tuuid, station, day_str, strike, side, ticker, probs)
        # ITEM2: a re-stamp must update BOTH sides atomically — the companion
        # (above) AND the booked probability columns (here), from the SAME
        # calibrated value, so the scoring path never reads a stale prob.
        _sync_booked_prob(conn, tuuid, our_prob)
        conn.commit()
        return {"status": "enriched", "station": station,
                "trade_uuid": tuuid, "picked_strike": strike,
                "p_gefs": probs.get("p_gefs"), "p_analog": probs.get("p_analog"),
                "p_trajectory": probs.get("p_trajectory"), "p_fused": probs.get("p_fused"),
                "agreement": probs.get("agreement"), "traj_verdict": probs.get("traj_verdict")}
    now = datetime.now(timezone.utc).isoformat()
    price = float(market_price)
    qty = max(1, int(round(size_usd / max(price, 0.01))))
    cost = abs(qty * price)
    # F2 (2026-10-06): direction/trade_type describe the SELECTED leg the EV
    # strike-picker returned (yes = long YES, no = long NO), NOT the
    # EntryTimingEngine's BUY/SELL default. `side` (engine signal) still gates.
    leg = selected_side if selected_side in ("yes", "no") else (
        "yes" if side == "BUY" else "no")
    direction = "UP" if leg == "yes" else "DOWN"
    tuuid = str(uuid.uuid4())
    try:
        conn.execute(
            """INSERT INTO trades (trade_uuid, trade_date_utc, station, market_type,
               signal_direction, forecast_prob, market_price, trade_type, quantity,
               position_size_usd, trade_price, trade_cost, trade_version, functionality,
               status, notes, implied_prob, analytical_prob, calibration_error,
               confidence_indicator, created_at_utc,
               p_gefs, p_analog, p_trajectory, p_fused, price_source, market_ticker,
               lane)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (tuuid, day_str, station, market_type, direction, our_prob, price,
             ("buy_yes" if leg == "yes" else "buy_no"), qty, size_usd, price, cost,
             f"v3.1_{INSTANCE.lower()}", functionality, "open",
             f"{('LOW' if (lane or '').upper() == 'LOW' else 'multi-forecaster')} "
             f"EV-strike {ticker} @ {strike}F; inst={INSTANCE}",
             price, our_prob, abs(our_prob - price), confidence, now,
             probs.get("p_gefs"), probs.get("p_analog"),
             probs.get("p_trajectory"), probs.get("p_fused"), price_source,
             ticker, (lane or "HIGH").upper()))
    except sqlite3.IntegrityError:
        # E4-b: a duplicate decision intent is refused by the UNIQUE index. Treat
        # it as an idempotent no-op — re-read the intent's row and enrich it in
        # place, exactly like the fast-path hit above — never insert a twin row.
        dup = conn.execute(
            "SELECT trade_uuid FROM trades WHERE station=? AND market_ticker=? "
            "AND created_at_utc=?", (station, ticker, now)).fetchone()
        if dup is None:
            raise
        tuuid = dup[0]
        conn.execute(
            "UPDATE trades SET p_gefs=?, p_analog=?, p_trajectory=?, p_fused=? "
            "WHERE trade_uuid=?",
            (probs.get("p_gefs"), probs.get("p_analog"),
             probs.get("p_trajectory"), probs.get("p_fused"), tuuid))
        _upsert_forecast(conn, tuuid, station, day_str, strike, side, ticker, probs)
        # ITEM2: keep the booked prob consistent with the companion re-stamp.
        _sync_booked_prob(conn, tuuid, our_prob)
        conn.commit()
        return {"status": "duplicate_intent_noop", "station": station,
                "trade_uuid": tuuid, "picked_strike": strike,
                "market_ticker": ticker,
                "p_gefs": probs.get("p_gefs"), "p_analog": probs.get("p_analog"),
                "p_trajectory": probs.get("p_trajectory"), "p_fused": probs.get("p_fused")}
    _upsert_forecast(conn, tuuid, station, day_str, strike, side, ticker, probs)
    # ITEM2': the insert wrote `our_prob` into the booked columns BEFORE the
    # companion existed. Now that the companion row is in, reconcile the booked
    # prob to the companion's own calibrated_p (the single source) — so a caller
    # that passed a value inconsistent with the side transform cannot persist a
    # divergent row. No-op when there is no companion calibrated_p (legacy).
    _sync_booked_prob(conn, tuuid, our_prob)
    conn.commit()
    return {"status": "executed", "station": station, "signal_direction": direction,
            "trade_uuid": tuuid,
            "strike": strike, "ticker": ticker, "our_prob": round(our_prob, 4),
            "market_price": price, "position_size_usd": round(size_usd, 2),
            "edge": round(our_prob - price, 4),
            "p_gefs": probs.get("p_gefs"), "p_analog": probs.get("p_analog"),
            "p_trajectory": probs.get("p_trajectory"), "p_fused": probs.get("p_fused"),
            "agreement": probs.get("agreement"), "traj_verdict": probs.get("traj_verdict")}


def build_fused_p_fn(state, cdf, traj: TrajectoryForecaster, hsf: Optional[float],
                     weights: Optional[Dict] = None,
                     ticker_map: Optional[Dict[float, str]] = None,
                     pin_iso: Optional[str] = None,
                     p_om_gfs_member=None,
                     station: Optional[str] = None):
    """Return (p_fn, meta_fn): per-strike x -> CALIBRATED p and the fusion meta.

    Weighted fusion (WEIGHTED-FUSION-SPEC-2026-10-13): when `weights` is given
    the core is the per-station weighted blend over {gefs, analog, traj, market},
    and the market component is the YES mid at the decision pin. Weights default
    to equal (0.25 each) until tuned.

    ``p_om_gfs_member`` (2026-10-13 WIRE FLIP) is an optional per-strike callable
    for the FIFTH member's P(high>=X). When supplied it joins the core blend and
    the composite confidence ADDITIVELY; when None the fusion is unchanged.

    **EV gate (2026-10-14):** `p_fn` now returns the HONEST `calibrated_p`
    (recalibrated to the decision-time axis, clamped at the per-station ceiling)
    rather than the raw fused probability — every EV computation and the booked
    `our_prob` therefore price off the calibrated signal. The raw fused value is
    still carried as `p_fused` for the faithful record. A strike whose honest
    calibration is unavailable raises, so the caller falls closed (no entry).

    ``station`` selects the per-station honest ceiling; when omitted the pooled
    ceiling is used.
    """
    cache: Dict[float, Dict] = {}
    mkt_cache: Dict[str, Optional[float]] = {}

    def _market_for(x: float) -> Optional[float]:
        tk = (ticker_map or {}).get(x)
        if tk is None:
            return None
        if tk not in mkt_cache:
            mkt_cache[tk] = market_mid_prob(tk, pin_iso)
        return mkt_cache[tk]

    _ceiling = station_ceiling(station) if station else None

    def _calc(x: float) -> Dict:
        if x in cache:
            return cache[x]
        row = fuse_strike(state.p_ge, cdf.p_high_ge,
                          lambda xx: traj.p_high_ge(xx, hsf), x,
                          weights=weights, use_weights=bool(weights),
                          p_market=_market_for(x),
                          p_om_gfs=(p_om_gfs_member(x)
                                    if p_om_gfs_member is not None else None),
                          ceiling=_ceiling)
        p_fused = float(row["p_fused"])
        ce = _ceiling if _ceiling is not None else float(row.get("ceiling") or 0.84)
        # Recalibrate the raw fused value to the honest decision-time axis. A
        # non-finite raw value raises -> the strike can never be traded.
        cal = honest_calibrated_p(p_fused, ce)
        row = dict(row)
        row["ceiling"] = round(float(ce), 4)
        row["calibrated_p"] = cal
        row["composite_conf"] = round(abs(cal - 0.5) * 2.0, 4)
        cache[x] = row
        return row

    def p_fn(x: float) -> float:
        return _calc(x)["calibrated_p"]

    return p_fn, _calc


# Workstream AF (2026-10-14): the sole-forecast probability rule id, recorded in
# the companion table in place of the retired fusion rule.
SOLE_FORECAST_RULE_ID = "om_gfs_sole:bc(bias-corrected,per-station);calibrated(honest_ceiling)"


def build_om_gfs_sole_p_fn(state, station: Optional[str] = None):
    """Return (p_fn, meta_fn): per-strike x -> CALIBRATED p for the SOLE om_gfs.

    Workstream AF (2026-10-14): the forecast is the single om_gfs
    (bias-corrected, per-station) DistributionState — no GEFS/analog/trajectory/
    market members. ``state.p_ge(x)`` is P(high >= x) for the station-day; the
    value is recalibrated to the SAME honest decision-time axis the fusion path
    used (``honest_calibrated_p`` clamped at the per-station ceiling), so the EV
    gate and booked ``our_prob`` are directly comparable to the pre-AF book —
    only the raw signal changed (one om_gfs value instead of a blend).

    The meta row keeps the fusion row's SHAPE (p_gefs/p_analog/p_trajectory/
    p_fused/agreement/edge_mult/...) so the book, companion table and downstream
    reports stay schema-compatible. p_gefs and p_fused both carry the om_gfs raw
    P(high>=X) (under USE_OM_GFS_NWP the NWP member IS om_gfs); the dropped
    members are None, with the single-source provenance recorded explicitly.
    """
    cache: Dict[float, Dict] = {}
    _ceiling = station_ceiling(station) if station else float(POOLED_CEILING)

    def _calc(x: float) -> Dict:
        if x in cache:
            return cache[x]
        p_raw = float(state.p_ge(x))
        ce = _ceiling if _ceiling is not None else float(POOLED_CEILING)
        try:
            cal = honest_calibrated_p(p_raw, ce)
        except ValueError:
            cal = float("nan")
        conf = abs(cal - 0.5) * 2.0 if cal == cal else None
        row = {
            "p_gefs": round(p_raw, 4),
            "p_analog": None, "p_trajectory": None,
            "p_fused": round(p_raw, 4),
            "agreement": "sole", "edge_mult": 1.0,
            "traj_verdict": "unavailable", "traj_mult": 1.0,
            "p_market": None, "p_om_gfs": round(p_raw, 4),
            "ceiling": round(float(ce), 4),
            "calibrated_p": cal,
            "composite_conf": (round(conf, 4) if conf is not None else None),
            "grade": (grade_from_confidence(conf) if conf is not None else None),
            "use_weights": False,
            "agreement_boost": {"enabled": False},
            "weights_applied": {"om_gfs": 1.0},
            "sole_forecast": True, "fusion_rule": SOLE_FORECAST_RULE_ID,
        }
        cache[x] = row
        return row

    def p_fn(x: float) -> float:
        return _calc(x)["calibrated_p"]

    return p_fn, _calc


def select_om_gfs_fusion_member(nwp_src, st, day_str, hsf, diag=None):
    """om_gfs as the FIFTH fusion member (2026-10-13 WIRE FLIP, ADDITIVE).

    Returns a DistributionState whose ``p_ge`` votes as ``p_om_gfs`` in the core
    blend and composite confidence, or None. Suppressed in two cases:
      * the fusion flag (USE_OM_GFS_FUSION) is off (default) -> pre-flip fusion
        is byte-identical; and
      * the NWP slot is ALREADY om_gfs (USE_OM_GFS_NWP on) -> the substitution
        carries the same signal and it must not vote twice.
    Never raises; a fetch failure leaves the fifth member out (weights rebalance).
    """
    if not (_OM_GFS_IMPORTED and om_gfs_fusion_enabled()):
        return None
    if nwp_src == "om_gfs":
        return None  # already carried by the NWP member; no double count
    try:
        return om_gfs_state_for_day(st, day_str, live=True, high_so_far=hsf)
    except Exception as e:  # noqa: BLE001 - never break the pass
        if diag is not None:
            diag.append({"station": st,
                         "om_gfs_member_error": f"{type(e).__name__}:{e}"})
        return None


def select_nwp_state(st, day_str, hsf, dist, month, diag=None):
    """Select the NWP component's DistributionState for a station-day.

    om_gfs (Open-Meteo bias-corrected GFS, USE_OM_GFS_NWP=1) is the PREFERRED
    NWP member; this function returns the raw local GFS-seeded WS-P distribution
    (nwp_src == "gfs_local") when the flag is off or when om_gfs is unavailable.
    Under the SOLE-forecast flag (om_gfs IS the forecast) the caller's DATA gate
    rejects nwp_src == "gfs_local" and SKIPS the station-day (fail closed) — it
    is not a fallback there. On the legacy fusion path (flag off) gfs_local is
    the pre-flag NWP member.

    Returns (state, nwp_src, ens):
      * nwp_src == "om_gfs"        -> state built from om_gfs (ens=None)
      * nwp_src == "gfs_local"     -> raw local GFS-seeded WS-P (pre-flag path)
      * nwp_src == "no_gefs_step24"-> no NWP at all (state=None; caller skips)
    `state` is NOT yet intraday-truncated; the caller applies update_from_intraday.
    """
    if _OM_GFS_IMPORTED and om_gfs_enabled():
        try:
            om_state = om_gfs_state_for_day(st, day_str, live=True, high_so_far=hsf)
        except Exception as e:  # noqa: BLE001 - never break the pass
            if diag is not None:
                diag.append({"station": st, "om_gfs_error": f"{type(e).__name__}:{e}"})
            om_state = None
        if om_state is not None:
            return om_state, "om_gfs", None
    ens = gefs_ens(st, day_str)
    if not ens:
        return None, "no_gefs_step24", None
    state = from_ws_p(dist, st, ens_mean=ens[0], ens_std=ens[1],
                      month=month, date=day_str)
    return state, "gfs_local", ens


def run(dry_run=False, ignore_window=False, stations=None, min_edge=0.03,
        report=False, max_stations=None, now_utc=None):
    stations = stations or STATIONS
    if max_stations:
        stations = stations[:max_stations]
    # ITEM1 (2026-10-16): `now_utc` is injectable so the 13:30 pin gate is
    # DETERMINISTIC under test (default: wall clock). The pin enforcement is the
    # only consumer that depends on it beyond the existing window/hour math.
    now_utc = now_utc or datetime.now(timezone.utc)
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
        ensure_station_weights_defaults(conn)  # F2: equal per-station weights
        _ensure_price_source_column(conn)  # E1: trade_uuid -> snapshot provenance

    # ONE matcher/climatology for the whole pass (the climatology build is the
    # expensive part; constructing per-station rebuilt it every time).
    shared_traj = TrajectoryForecaster()

    results, skipped, diag = [], [], []
    for st in stations:
        day_str, day_start, day_end = local_day(st, now_utc)
        date_yy = _dt_yy(day_str)
        month = int(day_str[5:7])
        # F5 (2026-10-13): the real hours-to-settlement for THIS station, not a
        # hardcoded 6. Settlement is the station's local midnight (start of the
        # next local day), so the value varies by timezone — e.g. at 03:00Z KNYC
        # sits at ~T-1h while KLAX sits at ~T-4h. Fallback 6.0 only when the
        # settlement instant cannot be resolved.
        _hb = _hours_before_settlement(st, day_str, now_utc)
        hours_to_settlement = _hb if _hb is not None else 6.0
        # F4 (2026-10-13): per-station entry/position params. Defaults preserve the
        # historical T-18h..T-2h window; marine regimes open earlier and convective
        # regimes later, so the window varies by regime instead of one global bound.
        sp = get_station_params(st)

        if not ignore_window:
            ok, reason = is_within_entry_window(st, day_str, now_utc=now_utc)
            # Keep the trading-day / already-settled guards, but defer the hour
            # bounds to the per-station window below (the global constants are
            # only the default regime's bound).
            if not ok and not reason.startswith(
                    ("too_far_from_settlement", "too_close_to_settlement")):
                skipped.append((st, reason))
                continue
            if hours_to_settlement > sp["entry_window_start_hb"]:
                skipped.append((st, f"outside_station_window_before_T-{hours_to_settlement:.1f}h"))
                continue
            if hours_to_settlement < sp["entry_window_end_hb"]:
                skipped.append((st, f"outside_station_window_after_T-{hours_to_settlement:.1f}h"))
                continue

        ens = None
        hsf = high_so_far(st, day_start, day_end, now_utc)
        if hsf is None:
            skipped.append((st, "no_intraday_obs_yet"))
            continue

        frac = max(0.0, min(1.0, (now_utc - day_start).total_seconds()
                             / (day_end - day_start).total_seconds()))
        # NWP component: om_gfs(bias-corrected) substitution behind USE_OM_GFS_NWP.
        # Default OFF -> raw local GFS-seeded WS-P (pre-flag behaviour). When the
        # sole flag is ON, nwp_src == "gfs_local" is skipped by the sole DATA gate
        # below (fail closed), NOT treated as a fallback.
        state, nwp_src, ens = select_nwp_state(st, day_str, hsf, dist, month, diag)
        if state is None:
            skipped.append((st, nwp_src))
            continue
        state = update_from_intraday(state, hsf, frac)

        # PWW Loop-2 (BLOCKER, 2026-10-16): bound the ladder by the canonical
        # decision pin, NOT now. Bounding by now let the selected market price
        # be hours newer than the 13:30 pin, making EV/side/size/price
        # non-reproducible and leaking post-decision market data.
        # PWW Loop-3 (BLOCKER, 2026-10-16): an UNRESOLVED pin must FAIL CLOSED.
        # The prior `_pin_iso or now_iso` silently substituted wall-clock time,
        # reintroducing the exact post-pin leak. Skip with an explicit reason.
        _pin_iso = pinned_decision_utc(st, day_str)
        if _pin_iso is None:
            skipped.append((st, "pin_unresolved"))
            diag.append({"station": st, "day": day_str,
                         "skip": "pin_unresolved", "now_utc": now_iso})
            continue
        ladder = strike_ladder(st, date_yy, before_iso=_pin_iso)
        if not ladder:
            skipped.append((st, "no_strike_ladder"))
            continue

        # Workstream AF (2026-10-14): behind USE_OM_GFS_NWP, om_gfs is the SOLE
        # forecast. The analog CDF and trajectory gate are fusion MEMBERS only,
        # so the sole path skips them entirely (also a real perf win — no
        # climatology build). The legacy fusion stays available when the flag is
        # off, so rollback is a flag flip.
        sole = sole_forecast_enabled()
        # PWW-2026-10-15 (ITEM1, BLOCKER): the sole path must be gated on the
        # DATA, not just on the flag. `select_nwp_state` falls through to the raw
        # local GFS-seeded WS-P (nwp_src == "gfs_local") whenever om_gfs is
        # unavailable or its fetch fails; the sole branch below would then run
        # `build_om_gfs_sole_p_fn` on that GFS-local state and hard-code om_gfs
        # provenance onto a non-om_gfs forecast (a silent fallback that books a
        # mislabelled trade). Fail closed exactly as the LOW lane's F5 fix does:
        # skip loudly, never book. (If gfs_local is ever intended as an allowed
        # fallback it must be booked as `gfs_local`, never `om_gfs`.)
        if sole and nwp_src != "om_gfs":
            skipped.append((st, "sole_no_om_gfs"))
            diag.append({"station": st, "day": day_str,
                         "skip": "sole_no_om_gfs", "nwp_src": nwp_src,
                         "forecast_src": None})
            continue
        # ITEM1 (2026-10-16, H3): enforce the canonical 13:30 station-local
        # decision pin on the SOLE path. `build_om_gfs_sole_p_fn` has no pin
        # argument, so the sole branch previously consumed the pin only as a
        # diagnostic and could book a station-day the moment the F4 window
        # opened (observed: all 10 day-1 fills stamped 14:15Z, ~3.5h before the
        # 17:30Z pin). A station-day must NOT be entered until
        # `now >= pinned_decision_utc(station, day)`: a pin still in the FUTURE
        # is SKIPPED (do not book early); an UNRESOLVABLE pin FAILS CLOSED
        # (skip), never defaulting the decision instant to `now`.
        if sole:
            pin_ok = sole_pin_reached(st, day_str, now_utc)
            if pin_ok is None:
                skipped.append((st, "sole_pin_unresolved"))
                diag.append({"station": st, "day": day_str,
                             "skip": "sole_pin_unresolved",
                             "pin_iso": None, "now_utc": now_iso})
                continue
            if not pin_ok:
                skipped.append((st, "sole_before_pin"))
                diag.append({"station": st, "day": day_str,
                             "skip": "sole_before_pin", "now_utc": now_iso,
                             "pin_iso": pinned_decision_utc(st, day_str)})
                continue
        cdf = None
        traj = None
        if not sole:
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

        # -- forecast per strike; select on the CALIBRATED probability --
        # AF (2026-10-14): sole om_gfs when the NWP-substitution flag is on, else
        # the legacy weighted fusion (F2 2026-10-13: per-station weights + the
        # market mid at the 13:30 pin as the 4th component).
        weights = load_weights(conn, st) if conn is not None else dict(DEFAULT_WEIGHTS)
        pin_iso = pinned_decision_utc(st, day_str)
        ticker_map = {r["strike"]: r.get("ticker") for r in ladder}
        if sole:
            p_fn, meta_fn = build_om_gfs_sole_p_fn(state, station=st)
        else:
            # om_gfs 5th fusion member is a NO-OP now (om_gfs_fusion_enabled() is
            # False); the call is retained so the legacy path is byte-identical.
            om_member = select_om_gfs_fusion_member(nwp_src, st, day_str, hsf, diag)
            p_fn, meta_fn = build_fused_p_fn(
                state, cdf, traj, hsf, weights=weights,
                ticker_map=ticker_map, pin_iso=pin_iso, station=st,
                p_om_gfs_member=(om_member.p_ge if om_member is not None else None))
        # F1 (2026-10-13): price the EV net of the modeled execution cost
        # (half-spread + slippage) instead of the bare mid, so a strike that is
        # only +EV on the mid is correctly rejected once crossing the book counts.
        cost_model = _station_spread_model(ladder)
        # F6 (2026-10-13): fold the disagreement-scaled min-edge into selection so
        # a top-EV strike that fails its widened floor falls through to the
        # next-best candidate that clears its own floor, instead of skipping the
        # station outright. min_edge is the per-strike floor (base * edge_mult).
        # ITEM 2: a genuinely fail-closed selection (unresolved live spread under
        # fail_closed=True) raises; that is a SKIP for THIS station, not a crash
        # of the pass — record it loudly and move on.
        try:
            picked = select_strike(state.mu, state.sigma, ladder, min_edge=min_edge,
                                   p_fn=p_fn, cost_model=cost_model, fail_closed=True,
                                   min_edge_scaler=lambda x: meta_fn(x)["edge_mult"])
        except ValueError as e:
            skipped.append((st, "no_resolvable_exec_spread_fail_closed"))
            diag.append({"station": st, "day": day_str,
                         "skip": "no_resolvable_exec_spread_fail_closed",
                         "resolution": getattr(cost_model, "resolution", None),
                         "error": str(e)})
            continue
        if not picked or picked.get("ev", 0) <= 0:
            skipped.append((st, "no_positive_ev_strike"))
            continue
        strike = picked["strike"]
        meta = meta_fn(strike)

        # EV gate (2026-10-14): no entry without a usable honest calibrated
        # probability. `p_fn` (which priced the EV) returns calibrated_p; if it
        # is absent/null/0 the calibration failed and we FAIL CLOSED.
        calibrated_p = meta.get("calibrated_p")
        if not calibrated_p or calibrated_p != calibrated_p or float(calibrated_p) <= 0.0:
            skipped.append((st, "no_calibrated_p_fail_closed"))
            continue

        row = next((s for s in ladder if abs(s["strike"] - strike) < 1e-9), None)
        if row is None:
            skipped.append((st, "no_real_price_for_picked_strike"))
            continue

        p_gefs = state.p_ge(strike)
        n_analog = cdf.n_analogs if cdf is not None else None
        n_traj = traj.n_analogs if traj is not None else None
        both = dict(meta)
        both.update({"p_gefs": meta["p_gefs"], "p_analog": meta["p_analog"],
                     "p_trajectory": meta["p_trajectory"],
                     "n_analog": n_analog, "n_traj": n_traj})
        # EV gate (2026-10-14): estimate on the HONEST calibrated probability,
        # not the raw fused value. `calibrated_p` was captured at the gate above.
        our_prob = (calibrated_p if picked["side"] == "yes"
                    else 1.0 - float(calibrated_p))
        mkt = row["yes_price"] if picked["side"] == "yes" else row["no_price"]
        ticker = row["ticker"]
        # E1 provenance: trade_uuid -> ticker -> snapshot_time -> mid_price.
        price_source = (f"{PRICE_SNAPSHOT_SOURCE}@{row.get('snapshot_time')}"
                        f"|mid={row.get('yes_price')}")

        # directional gate verdict on the trade side (Item 4 evidence).
        # Our proposition is P(high>=strike): YES side = we expect the high to
        # reach/exceed the strike -> trajectory direction "up"; NO -> "down".
        gate_dir = "up" if picked["side"] == "yes" else "down"
        # AF: on the sole path the trajectory gate is absent, so the verdict is
        # simply "unavailable" (diagnostic only — it never gates the entry).
        verdict = (traj.gate_verdict(gate_dir, abs(our_prob - 0.5) * 2.0)
                   if traj is not None else {"verdict": "unavailable"})

        diag.append({"station": st, "day": day_str,
                     "nwp_src": nwp_src,
                     "ens": ([round(ens[0], 2), round(ens[1], 2)] if ens else None),
                     "hsf": hsf,
                     "frac": round(frac, 3), "mu": round(state.mu, 2),
                     "sigma": round(state.sigma, 2), "strike": strike,
                     "side": picked["side"], "ev": picked["ev"],
                     "our_prob": round(our_prob, 4), "mkt": mkt,
                     "p_gefs": both["p_gefs"], "p_analog": both["p_analog"],
                     "p_trajectory": both["p_trajectory"], "p_fused": both["p_fused"],
                     "calibrated_p": both.get("calibrated_p"),
                     "p_market": both.get("p_market"),
                     "p_om_gfs": both.get("p_om_gfs"),
                     "composite_conf": both.get("composite_conf"),
                     "ceiling": both.get("ceiling"),
                     "agreement": both["agreement"], "traj_verdict": both["traj_verdict"],
                     "n_analog": n_analog, "n_traj": n_traj,
                     "gate": verdict["verdict"], "p_warm": verdict.get("p_warm"),
                     "sole_forecast": sole,
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
            engine, hour=now_utc.hour, hour_before_settlement=hours_to_settlement,
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

        # Kelly sizing (Workstream AF, 2026-10-14): size on the calibrated
        # probability of the BOOKED side vs the price paid. Replaces the retired
        # confidence ladder that produced 19:1 win/loss asymmetry on cheap entries.
        confidence = abs(our_prob - 0.5) * 2.0
        # B&B-K (2026-10-16): size net of the per-contract execution cost so the
        # Kelly edge matches the honest gate (execution_cost_rate = ~0.0205). The
        # prior call defaulted fee_rate=0.0, admitting marginal trades that fail
        # the honest-EV gate by anything up to the fee.
        size_usd = _position_size_usd(our_prob, mkt, fee_rate=_exec_cost_rate())
        # A $0 Kelly size means no edge at this price -> NO trade. Without this
        # guard a $0 size still records a phantom row (qty floored to 1).
        if size_usd <= 0:
            skipped.append((st, "kelly_zero_edge"))
            continue
        # F4 (2026-10-13): per-station confidence floor (default 0.0 == no change).
        if confidence < sp["confidence_floor"]:
            skipped.append((st, f"below_station_confidence_floor_{sp['confidence_floor']}"))
            continue

        # F7 (2026-10-13): opportunity grade gate. Grade on the booked side's
        # calibrated probability vs the price paid; F grade does not enter and the
        # grade caps the position (S=$25, A=$15, B=$10, C=$5). The ladder size was
        # already computed, so the cap only ever tightens it.
        grade, grade_edge = compute_trade_grade(our_prob, mkt)
        if grade == "F":
            skipped.append((st, f"grade_F_edge:{round(grade_edge, 4)}"))
            continue
        grade_cap = grade_position_cap(grade)
        size_usd = min(size_usd, grade_cap)
        # F4: per-station position cap (default $25 == the current max, so no change
        # for defaults; a station may be tightened below the grade cap).
        size_usd = min(size_usd, sp["position_cap_usd"])
        if size_usd <= 0:
            skipped.append((st, f"grade_{grade}_zero_cap"))
            continue

        prob_payload = {"p_gefs": both["p_gefs"], "p_analog": both["p_analog"],
                        "p_trajectory": both["p_trajectory"], "p_fused": both["p_fused"],
                        "n_analog": n_analog, "n_traj": n_traj,
                        "agreement": both["agreement"], "edge_mult": both["edge_mult"],
                        "traj_verdict": both["traj_verdict"], "traj_mult": both["traj_mult"],
                        "grade_entered": grade, "grade_cap_usd": grade_cap,
                        "fusion_rule": both.get("fusion_rule"),
                        # F2: calibrated p_fused + weighted-fusion provenance.
                        "calibrated_p": both.get("calibrated_p"),
                        "p_market": both.get("p_market"),
                        "p_om_gfs": both.get("p_om_gfs"),
                        "composite_conf": both.get("composite_conf"),
                        # ITEM4 (2026-10-16): the companion `grade` MUST be the
                        # GATE-TIME grade (the edge-ladder grade that actually
                        # gated entry), not the fusion meta's
                        # `grade_from_confidence` re-score. They are different
                        # ladders: the meta grade is a composite-confidence
                        # letter (F for a coin-flip-ish conf) while the gate
                        # grade is the edge letter. On the live book the two
                        # disagreed on 5 entered rows (grade='F' while
                        # grade_entered gated entry). Store the gate grade so
                        # the booked row and its companion agree.
                        "grade": grade,
                        "ceiling": both.get("ceiling"),
                        "weights_applied": both.get("weights_applied")}

        if dry_run:
            results.append({"status": "would_execute", "station": st,
                            "side": sig.signal_type.name, "strike": strike,
                            "our_prob": round(our_prob, 4), "market_price": mkt,
                            "size": size_usd, "grade": grade,
                            **prob_payload})
        else:
            # F9 (2026-10-13): portfolio exposure gate BEFORE booking. Count the
            # live open book: >=3 open for this station or >=10 open for this day
            # blocks the candidate (and is logged, never silent).
            skip_reason = exposure_skip_reason(conn, st, day_str)
            if skip_reason:
                skipped.append((st, skip_reason))
                continue
            # GR5 §2 pre-ingest gate: the candidate must pass the decidable
            # subset (G0 window-green · G1 identity/version · G4 axis) BEFORE it
            # is booked; the verdict is appended to the append-only gate ledger.
            # A rejected candidate is not booked. Fail-open (never break the
            # booking path on a gate error) but loud.
            if ENABLE_BOOKING_GATE:
                # ITEM4.3 (2026-10-16): the ledger's `trade_uuid` is the
                # DETERMINISTIC DECISION-INTENT key `SBOX-{station}-{day}`, not
                # the booked row's random uuid4. This is BY DESIGN: the ledger
                # key must be reproducible across a cron re-run so the append is
                # idempotent (verdict_idempotency_key is a hash of the key — a
                # per-run uuid4 would enqueue a duplicate verdict on every
                # re-invoke). The intent key maps 1:1 to the booked row via
                # (station, trade_date_utc), which is exactly how the book is
                # keyed; the join is `gate_ledger.station/day` <-> `trades`.
                # See docs/plans/GATE-LEDGER-INTENT-KEY-2026-10-16.md.
                try:
                    gv, _g_admitted = admit_decision_candidate(
                        trade_uuid=f"{INSTANCE}-{st}-{day_str}",
                        instance=INSTANCE, station=st, market_type="HIGH",
                        settlement_date=day_str, label_axis="strike_crossing",
                        conn=conn, repo=REPO, suite_green=True)
                except Exception as e:  # pragma: no cover
                    diag.append({"station": st,
                                 "booking_gate_error": f"{type(e).__name__}:{e}"})
                else:
                    if not _g_admitted:
                        skipped.append((st, f"booking_gate_{gv.verdict}"))
                        continue
            rec = record_trade(
                conn, st, "HIGH", sig.signal_type.name, our_prob, mkt,
                abs(our_prob - 0.5) * 2.0, strike, ticker,
                size_usd, "intraday_ev_strike", day_str, probs=prob_payload,
                price_source=price_source, selected_side=picked["side"])
            # GR5 §1 TradeCustodyRecord: one chain-of-custody row per booked
            # trade (idempotent). Read the just-booked row back for real
            # provenance; never let a TCR failure un-book the trade.
            if ENABLE_BOOKING_GATE:
                tuuid = rec.get("trade_uuid")
                if rec.get("status") == "executed" and tuuid:
                    try:
                        trow = _fetch_trade_dict(conn, tuuid)
                        if trow is not None:
                            rec["custody_hash"] = record_custody(
                                conn, trow, instance=INSTANCE)
                    except Exception as e:  # pragma: no cover
                        diag.append({"station": st,
                                     "custody_error": f"{type(e).__name__}:{e}"})
            results.append(rec)

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
# F3 (2026-10-13) — production exit pass over real open SBOX holdings
# =============================================================================
# Before F3, core/intraday/exit_logic.evaluate_exits had no production caller:
# intraday_runner built `open_positions` from THIS cycle's entries only, so no
# exit rule ever saw a real holding. This pass loads the actual status='open'
# SBOX rows, prices them from the snapshot DB, and closes the triggered ones.
#
# Rule activation (safest first): only h-1 auto-close is enabled by default.
# Take-profit / stop-loss / signal-flip are opt-in via env flags so they can be
# rolled out behind a config switch, exactly as the sprint asks.
EXIT_RULE_FLAGS = {
    "take_profit": "WE_SBOX_EXIT_TAKE_PROFIT",
    "stop_loss": "WE_SBOX_EXIT_STOP_LOSS",
    "signal_flip": "WE_SBOX_EXIT_SIGNAL_FLIP",
}


def _enabled_exit_rules() -> set:
    """Enabled exit rules: h-1 always on; the others gated on env flags."""
    rules = {"auto_close_h1"}
    for rule, flag in EXIT_RULE_FLAGS.items():
        if os.environ.get(flag, "0") == "1":
            rules.add(rule)
    return rules


def _exit_live_mid(ticker: Optional[str], before_iso: str) -> Optional[float]:
    """Newest snapshot mid (at/before now) for a market ticker, or None."""
    if not ticker:
        return None
    c = _ro(SNAPS)
    try:
        row = c.execute(
            "SELECT mid_price FROM market_snapshots WHERE market_ticker=? "
            "AND mid_price IS NOT NULL AND snapshot_time < ? "
            "ORDER BY snapshot_time DESC LIMIT 1", (ticker, before_iso)).fetchone()
    except sqlite3.Error:
        row = None
    finally:
        c.close()
    if row and row[0] is not None:
        return float(row[0])
    return None


def open_exit_positions(conn) -> List[Dict]:
    """Real open SBOX holdings -> exit_logic position dicts (F3).

    entry_price is the mid the trade was booked at (market_price); direction is
    the selected leg (UP=yes / DOWN=no). These are holdings, not this cycle's
    entries — the distinction intraday_runner got wrong.
    """
    rows = conn.execute(
        "SELECT trade_uuid, station, trade_date_utc, signal_direction, "
        "market_price, quantity, position_size_usd, created_at_utc, market_ticker, notes "
        "FROM trades WHERE status='open'").fetchall()
    out = []
    for (tuuid, station, dstr, direction, mprice, qty, pos_usd,
         created, ticker, notes) in rows:
        try:
            entry_hour = datetime.fromisoformat(created).hour if created else 0
        except (TypeError, ValueError):
            entry_hour = 0
        out.append({
            "trade_uuid": tuuid, "station": station, "trade_date_utc": dstr,
            "direction": (direction or "UP").upper(),
            "entry_price": float(mprice) if mprice is not None else 0.5,
            "position_size": float(qty) if qty is not None else 1.0,
            "position_size_usd": float(pos_usd) if pos_usd is not None else 0.0,
            "entry_hour": entry_hour, "ticker": ticker, "notes": notes,
        })
    return out


def _hours_before_settlement(station: str, trading_date: str, now_utc: datetime) -> Optional[float]:
    """Hours from now to the station's local-day settlement (negative = past)."""
    settle = settlement_time_utc(station, trading_date)
    if settle is None:
        return None
    return (settle - now_utc).total_seconds() / 3600.0


def run_exit_pass(dry_run: bool = False, stations=None) -> Dict:
    """Close open SBOX holdings whose exit rules have triggered (F3).

    Read-only on every source DB other than the SBOX book itself. With
    ``dry_run=True`` it reports the would-be closes and writes nothing.
    """
    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()
    rules = _enabled_exit_rules()
    conn = sqlite3.connect(SBOX_DB)
    try:
        # PWW Loop-2 (MINOR, 2026-10-16): a dry run must not mutate the DB.
        # The WAL journal-mode switch writes to the file, so only apply it when
        # we actually intend to write.
        if not dry_run:
            conn.execute("PRAGMA journal_mode=WAL;")
        positions = open_exit_positions(conn)
        if stations:
            positions = [p for p in positions if p["station"] in stations]

        closed, skipped = [], []
        for pos in positions:
            ticker = pos.get("ticker")
            if not ticker:
                skipped.append((pos["trade_uuid"], "no_market_ticker"))
                continue
            mid = _exit_live_mid(ticker, now_iso)
            if mid is None:
                skipped.append((pos["trade_uuid"], "no_live_price"))
                continue
            hb = _hours_before_settlement(pos["station"], pos["trade_date_utc"], now_utc)
            if hb is None:
                skipped.append((pos["trade_uuid"], "settlement_time_unknown"))
                continue
            if hb < 0:
                # Already past settlement: this row is unreconciled, not an open
                # holding. The exit pass must not fabricate a mid-based PnL for a
                # market that has already settled — leave it to settlement
                # reconciliation and surface it loudly.
                skipped.append((pos["trade_uuid"], "already_settled_pending_reconciliation"))
                continue
            # Reason strings are the trade_uuid: one position per call keeps each
            # station's own hours-to-settlement honest (stations settle at
            # different UTC instants).
            sigs = evaluate_exits(
                [pos], fused_results={}, live_prices={pos["station"]: mid},
                hour_before_settlement=hb, current_hour=now_utc.hour,
                enabled_rules=rules)
            if not sigs:
                skipped.append((pos["trade_uuid"], "no_exit_rule_triggered"))
                continue
            sig = sigs[0]
            pnl = round(sig.estimated_pnl, 4)
            entry = {"trade_uuid": pos["trade_uuid"], "station": pos["station"],
                     "reason": sig.reason, "entry_price": pos["entry_price"],
                     "current_price": round(mid, 4), "size": pos["position_size"],
                     "pnl": pnl, "hours_before_settlement": round(hb, 2)}
            if not dry_run:
                conn.execute(
                    "UPDATE trades SET status='closed', realized_pnl=?, "
                    "notes=COALESCE(notes,'') || ? WHERE trade_uuid=?",
                    (pnl, f"; exit={sig.reason}@{now_iso[:19]}Z", pos["trade_uuid"]))
                conn.commit()
            closed.append(entry)
        return {"date_utc": now_utc.strftime("%Y-%m-%d"),
                "enabled_rules": sorted(rules),
                "n_open": len(positions), "n_closed": len(closed),
                "n_skipped": len(skipped), "closed": closed,
                "skipped": skipped[:25], "dry_run": dry_run}
    finally:
        conn.close()


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
    ap.add_argument("--exit-pass", action="store_true",
                    help="close open SBOX holdings whose exit rules triggered (F3)")
    ap.add_argument("--stations", nargs="*", default=None)
    ap.add_argument("--min-edge", type=float, default=0.03)
    ap.add_argument("--max-stations", type=int, default=None)
    a = ap.parse_args()
    if a.combined_report:
        print(json.dumps(combined_report(a.stations), indent=2, default=str))
        return 0
    if a.exit_pass:
        out = run_exit_pass(dry_run=a.dry_run, stations=a.stations)
        print(json.dumps(out, indent=2, default=str))
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
