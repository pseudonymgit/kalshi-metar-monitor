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
    fuse_strike, ensure_schema, FUSION_RULE_ID,
)
from core.station_time import (  # noqa: E402
    is_within_entry_window, station_timezone_name, settlement_time_utc,
)
from core.market_cost_model import MARKET_COST_MODEL  # noqa: E402

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
    ``core.strike_selection_ev.select_strike``. When the live probe is unavailable
    (or ``WE_LIVE_SPREAD_EV=0``), it returns the model's measured-mean spread, so
    the per-contract entry cost collapses to ``EXECUTION_COST_RATE``
    (measured half-spread + slippage) — the same fallback F1 specifies.
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
                probe = MARKET_COST_MODEL.get_dynamic_spread(ticker)
            except Exception as e:  # never break the pass on a live-data error
                print(f"[warn] live spread probe {ticker} failed: {type(e).__name__}: {e}",
                      file=sys.stderr)
                probe = None

    class _StationSpreadModel:
        slippage = slip_rate

        def get_dynamic_spread(self, ticker: str) -> float:
            if probe is not None and probe > 0:
                return float(probe)
            # Documented fallback: measured mean spread (=> EXECUTION_COST_RATE).
            return float(MARKET_COST_MODEL.spread)

    return _StationSpreadModel()


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
    """Dan's sizing ladder (revised 2026-10-12) — $200 bankroll.

    Regular trades are $5-10; only high-confidence "sure things" reach $25.
    The prior ladder (2026-10-01) returned $50 for confidence>0.85 — that is 25%
    of a $200 bankroll on a single trade and was the source of the 46 $50 rows in
    the SBOX book. The max is now $25 (12.5% of $200).

    - very high confidence (>0.85): $25 (sure-thing max)
    - high confidence (>0.7): $10
    - medium confidence (>0.5): $5
    - below 0.5: $0 (no trade)
    """
    if confidence > 0.85:
        return 25.0
    elif confidence > 0.7:
        return 10.0
    elif confidence > 0.5:
        return 5.0
    return 0.0


def _ensure_price_source_column(conn) -> None:
    """Additive provenance column (E1, 2026-10-05): trade_uuid -> snapshot row.

    Idempotent; existing rows keep NULL until a reconciliation backfill runs.
    """
    cols = {d[1] for d in conn.execute("PRAGMA table_info(trades)")}
    if "price_source" not in cols:
        conn.execute("ALTER TABLE trades ADD COLUMN price_source TEXT")
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


def record_trade(conn, station, market_type, side, our_prob, market_price,
                 confidence, strike, ticker, size_usd, functionality, day_str,
                 probs: Optional[Dict] = None, price_source: Optional[str] = None,
                 selected_side: Optional[str] = None):
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
    _ensure_trade_uniqueness(conn)
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
               p_gefs, p_analog, p_trajectory, p_fused, price_source, market_ticker)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (tuuid, day_str, station, market_type, direction, our_prob, price,
             ("buy_yes" if leg == "yes" else "buy_no"), qty, size_usd, price, cost,
             f"v3.1_{INSTANCE.lower()}", functionality, "open",
             f"multi-forecaster EV-strike {ticker} @ {strike}F; inst={INSTANCE}",
             price, our_prob, abs(our_prob - price), confidence, now,
             probs.get("p_gefs"), probs.get("p_analog"),
             probs.get("p_trajectory"), probs.get("p_fused"), price_source,
             ticker))
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
        conn.commit()
        return {"status": "duplicate_intent_noop", "station": station,
                "trade_uuid": tuuid, "picked_strike": strike,
                "market_ticker": ticker,
                "p_gefs": probs.get("p_gefs"), "p_analog": probs.get("p_analog"),
                "p_trajectory": probs.get("p_trajectory"), "p_fused": probs.get("p_fused")}
    _upsert_forecast(conn, tuuid, station, day_str, strike, side, ticker, probs)
    conn.commit()
    return {"status": "executed", "station": station, "signal_direction": direction,
            "trade_uuid": tuuid,
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
        # F1 (2026-10-13): price the EV net of the modeled execution cost
        # (half-spread + slippage) instead of the bare mid, so a strike that is
        # only +EV on the mid is correctly rejected once crossing the book counts.
        cost_model = _station_spread_model(ladder)
        # F6 (2026-10-13): fold the disagreement-scaled min-edge into selection so
        # a top-EV strike that fails its widened floor falls through to the
        # next-best candidate that clears its own floor, instead of skipping the
        # station outright. min_edge is the per-strike floor (base * edge_mult).
        picked = select_strike(state.mu, state.sigma, ladder, min_edge=min_edge,
                               p_fn=p_fn, cost_model=cost_model, fail_closed=True,
                               min_edge_scaler=lambda x: meta_fn(x)["edge_mult"])
        if not picked or picked.get("ev", 0) <= 0:
            skipped.append((st, "no_positive_ev_strike"))
            continue
        strike = picked["strike"]
        meta = meta_fn(strike)

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
        # E1 provenance: trade_uuid -> ticker -> snapshot_time -> mid_price.
        price_source = (f"{PRICE_SNAPSHOT_SOURCE}@{row.get('snapshot_time')}"
                        f"|mid={row.get('yes_price')}")

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
            # GR5 §2 pre-ingest gate: the candidate must pass the decidable
            # subset (G0 window-green · G1 identity/version · G4 axis) BEFORE it
            # is booked; the verdict is appended to the append-only gate ledger.
            # A rejected candidate is not booked. Fail-open (never break the
            # booking path on a gate error) but loud.
            if ENABLE_BOOKING_GATE:
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
