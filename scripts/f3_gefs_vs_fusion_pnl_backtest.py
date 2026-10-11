#!/usr/bin/env python3
"""
F3 — Real P&L comparison: GEFS-alone vs the fused stack (SBOX book).

The prior GEFS-alone vs fusion comparison was DIRECTIONAL (win/loss) only. This
backtest answers the P&L question instead:

    If we replaced the fused probability with a single forecaster's probability
    and let the SAME EV-maximizing strike selection / entry / sizing rules run,
    would the realized P&L differ?

Scope
-----
The universe is the 68 SETTLED SBOX trades in ``data/paper_trading_sbox.db``
(trade_version v3.1_sbox, functionality intraday_ev_strike). For every trade we
REPLAY the decision at its original decision timestamp and re-select the
EV-maximizing strike four ways:

    fused        — fuse_v1(p_gefs, p_analog, p_trajectory)   (the live rule)
    gefs_only    — p_gefs alone (drop analog + trajectory)
    analog_only  — p_analog alone
    trajectory_only — p_trajectory alone (leak-safe)

Every arm then runs the SAME downstream path the live bridge runs:
    select_strike(...) -> entry engine gate -> Dan sizing ladder -> settle.

Accounting: P&L is reported on the booked convention (qty*outcome - cost, entry
fee EXCLUDED, matching how the live book records realized_pnl) AND fee-adjusted
(minus the canonical Kalshi taker fee per contract). The fused arm's no-fee P&L
must reconcile to the live book's realized P&L — that is the reconstruction
validity check.

Constraints honoured
--------------------
* Read-only on every source DB (``mode=ro`` URIs); never opens the live book.
* Works on a private copy of the SBOX trades table (``--trades-db``).
* No live wiring; no trade is booked anywhere.
* Deterministic; no AI/ML in the loop.

Known replay caveats (surfaced, not hidden)
-------------------------------------------
* GEFS fetch-date handling: the committed fetcher used the latest fetch_date for
  the target, which can include post-decision cycles. This is the same input the
  live probabilities were derived from, so it is held identical across arms.
* The trajectory AnalogMatcher only excludes +/-14 days; we add a strict
  ``date < query_date`` guard to remove forward leakage.
* ``AnalogMatcher._load_epochs_for_zone`` mutates the module-level
  ``CLIMATE_ZONES`` lists in place (aliasing bug); we snapshot and restore them
  before every ``find_analogs`` call so repeated calls cannot overflow SQLite.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from core.intraday.distribution_adapter import from_ws_p, update_from_intraday  # noqa: E402
from core.intraday.entry_engine import EntryTimingEngine  # noqa: E402
from core.strike_selection_ev import select_strike  # noqa: E402
from core.fees import fee_per_contract  # noqa: E402
from core.station_time import station_timezone_name  # noqa: E402
import core.intraday.multi_forecaster_fusion as mff  # noqa: E402
import scripts.sbox_intraday_ev_bridge as bridge  # noqa: E402

DATA = REPO / "data"
WS_P = DATA / "ws_p_distribution.json"
NWP = DATA / "nwp_forecasts.db"
METAR = DATA / "metar_backfill.db"
DEFAULT_TRADES_DB = DATA / "paper_trading_sbox.db"
SETTLEMENTS = DATA / "kalshi_settlements.db"

MIN_EDGE = 0.03                 # live bridge default
BANKROLL = 10000.0
ENTRY_ENGINE = EntryTimingEngine()

# ── snapshot the climate-zone lists once so we can defeat the aliasing bug ─────
_ZONE_SNAPSHOT = {k: list(v) for k, v in mff.__dict__.get("CLIMATE_ZONES", {}).items()}
if not _ZONE_SNAPSHOT:
    from core.trajectory_confirmation_gate import CLIMATE_ZONES, ZONE_ADJACENCY
    _ZONE_SNAPSHOT = {k: list(v) for k, v in CLIMATE_ZONES.items()}


def _ro(p) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def _reset_zones() -> None:
    """Restore CLIMATE_ZONES lists mutated by AnalogMatcher._load_epochs_for_zone."""
    from core.trajectory_confirmation_gate import CLIMATE_ZONES
    for k, v in _ZONE_SNAPSHOT.items():
        CLIMATE_ZONES[k] = list(v)


# =============================================================================
# Reconstruction helpers (mirror the live bridge exactly)
# =============================================================================
def _c2f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def local_day(station: str, now_utc: datetime) -> Tuple[str, datetime, datetime]:
    tz = ZoneInfo(station_timezone_name(station))
    d = now_utc.astimezone(tz).date()
    start = datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(timezone.utc)
    return start.astimezone(tz).strftime("%Y-%m-%d"), start, start + timedelta(days=1)


def _dt_yy(d: str) -> str:
    return bridge._dt_yy(d)


def gefs_ens(station: str, target: str) -> Optional[Tuple[float, float]]:
    """(ens_mean_f, ens_std_f) of the pinned GEFS ensemble — bridge-identical."""
    c = _ro(NWP)
    try:
        rows = c.execute(
            "SELECT variable, value, fetch_date FROM nwp_forecasts "
            "WHERE model='gefs_ens' AND station=? AND target_date=? "
            "AND variable LIKE 'temperature_2m_max_member%'",
            (station, target)).fetchall()
    finally:
        c.close()
    latest: Dict[str, Tuple[str, float]] = {}
    for var, val, fd in rows:
        if val is None:
            continue
        if var not in latest or fd > latest[var][0]:
            latest[var] = (fd, val)
    vals = [_c2f(v) for _, v in latest.values()]
    if len(vals) < 3:
        return None
    m = sum(vals) / len(vals)
    sd = (sum((v - m) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5
    return (m, sd)


def high_so_far(station: str, day_start: datetime, day_end: datetime,
                now_utc: datetime) -> Optional[float]:
    hi = min(now_utc, day_end).isoformat()
    c = _ro(METAR)
    try:
        row = c.execute(
            "SELECT MAX(temp_f) FROM metar_observations WHERE station=? "
            "AND timestamp_utc >= ? AND timestamp_utc < ?",
            (station, day_start.isoformat(), hi)).fetchone()
    finally:
        c.close()
    return float(row[0]) if row and row[0] is not None else None


def decision_snapshot(station: str, before_iso: str) -> Optional[Dict]:
    """The exact snapshot row (mid price) the ladder was built from, if present."""
    c = _ro(DATA / "kalshi_market_snapshots.db")
    try:
        row = c.execute(
            "SELECT snapshot_time, mid_price FROM market_snapshots WHERE station=? "
            "AND market_ticker LIKE 'KXHIGH%' AND mid_price IS NOT NULL "
            "AND snapshot_time < ? ORDER BY snapshot_time DESC LIMIT 1",
            (station, before_iso)).fetchone()
    finally:
        c.close()
    return {"snapshot_time": row[0], "mid": row[1]} if row else None


def make_gefs_p_fn(state):
    return lambda x: state.p_ge(x)


def make_analog_p_fn(cdf):
    return lambda x: cdf.p_high_ge(x)


def make_traj_p_fn(station: str, day_start: datetime, day_end: datetime,
                   now_utc: datetime, query_date: str,
                   shared: "mff.TrajectoryForecaster"):
    """Leak-safe P(high>=X) from the trajectory analog outcome CDF."""
    try:
        q_epoch = mff.build_live_epoch(station, day_start, day_end, now_utc)
    except Exception:
        return None
    if q_epoch is None:
        return None
    _reset_zones()
    try:
        analogs = shared.gate.matcher.find_analogs(q_epoch, top_k=200)
    except Exception:
        return None
    analogs = [a for a in analogs if a["epoch"].date < query_date]
    outs, ws = [], []
    for a in analogs:
        s = a["epoch"].settlement_temp
        if s is None:
            continue
        outs.append(float(s))
        ws.append(max(1e-6, float(a["match_score"])))
    if not outs:
        return None
    o = np.asarray(outs, dtype=float)
    w = np.asarray(ws, dtype=float)
    denom = float(w.sum())

    def p(x: float, hsf: Optional[float]) -> float:
        if hsf is not None and x <= hsf:
            return 1.0
        num = float(w[o >= x].sum())
        if hsf is None:
            return num / denom if denom > 0 else float("nan")
        base = float(w[o >= hsf].sum())
        if base <= 1e-12:
            return 0.0
        return min(1.0, num / base)

    return p


# =============================================================================
# One arm's decision + settlement
# =============================================================================
def run_arm(p_fn: Callable[[float], float], edge_mult_fn, state, ladder: List[Dict],
            now_utc: datetime):
    """Select a strike with p_fn and apply the live entry/sizing rules.

    Returns a dict {strike, side, ev, price, our_prob, size_usd} or None (skip).
    """
    picked = select_strike(state.mu, state.sigma, ladder, min_edge=0.0, p_fn=p_fn)
    if not picked or picked.get("ev", 0) <= 0:
        return None
    edge_mult = edge_mult_fn(picked["strike"])
    if picked["ev"] <= MIN_EDGE * edge_mult:
        return None
    strike = picked["strike"]
    side = picked["side"]
    row = next((s for s in ladder if abs(s["strike"] - strike) < 1e-9), None)
    if row is None:
        return None
    p_at = p_fn(strike)
    our_prob = p_at if side == "yes" else 1.0 - p_at
    mkt = row["yes_price"] if side == "yes" else row["no_price"]
    conf = abs(our_prob - 0.5) * 2.0
    sig = entry_signal(now_utc.hour, our_prob, mkt, conf)
    if sig is None:
        return None
    size = bridge._position_size_usd(conf)
    if size <= 0:
        return None
    qty = max(1, int(round(size / max(mkt, 0.01))))
    cost = abs(qty * mkt)
    return {"strike": strike, "side": side, "ev": picked["ev"], "price": mkt,
            "our_prob": our_prob, "qty": qty, "size_usd": size, "cost": cost,
            "confidence": conf}


def entry_signal(hour: int, our_prob: float, mkt: float, conf: float):
    """Live entry gate (overlay disabled -> raw engine decision)."""
    sig = ENTRY_ENGINE.evaluate_hour(hour=hour, hour_before_settlement=6,
                                     our_probability=our_prob, market_price=mkt,
                                     confidence=conf)
    if sig is None or sig.signal_type.name == "PASS":
        return None
    return sig


def settled_win(side: str, settled_f: float, strike: float) -> bool:
    """Strict-greater settlement (Kalshi finalized): UP wins iff high > strike."""
    if side == "yes":
        return settled_f > strike
    return settled_f <= strike


# =============================================================================
# Main replay
# =============================================================================
def load_trades(trades_db: str) -> List[Dict]:
    c = sqlite3.connect(trades_db)
    cols = {d[1] for d in c.execute("PRAGMA table_info(trades)")}
    has_custody = c.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='trade_custody'").fetchone()[0] > 0
    custody_join = ("LEFT JOIN trade_custody tc ON tc.trade_uuid=t.trade_uuid"
                    if has_custody else "")
    ts_expr = "COALESCE(tc.decision_ts_utc,t.created_at_utc)" if has_custody else "t.created_at_utc"
    q = f"""SELECT t.trade_uuid,t.station,t.trade_date_utc,t.notes,t.trade_type,
                   t.trade_price,t.quantity,t.realized_pnl,t.settled_value,
                   t.canonical_settled_f,{ts_expr}
            FROM trades t {custody_join}
            WHERE t.realized_pnl IS NOT NULL AND t.canonical_settled_f IS NOT NULL
            ORDER BY t.trade_date_utc, {ts_expr}"""
    out = []
    for r in c.execute(q):
        m = re.search(r"@\s*([0-9.]+)F", r[3] or "")
        if not m:
            continue
        out.append({"trade_uuid": r[0], "station": r[1], "date": r[2],
                    "booked_strike": float(m.group(1)), "trade_type": r[4],
                    "trade_price": r[5], "quantity": r[6], "realized_pnl": r[7],
                    "settled_value": r[8], "settled_f": r[9], "decision_ts": r[10]})
    c.close()
    return out


def replay(trades: List[Dict]) -> Tuple[List[Dict], List[str]]:
    dist = json.load(open(WS_P))
    shared = mff.TrajectoryForecaster()
    arms = ["fused", "gefs_only", "analog_only", "trajectory_only"]
    rows: List[Dict] = []
    notes: List[str] = []
    for t in trades:
        st, date = t["station"], t["date"]
        now = datetime.fromisoformat(t["decision_ts"])
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        ds, dstart, dend = local_day(st, now)
        ens = gefs_ens(st, date)
        hsf = high_so_far(st, dstart, dend, now)
        if not ens or hsf is None:
            notes.append(f"skip {st} {date}: no gefs/obs")
            continue
        date_yy = _dt_yy(date)
        ladder = bridge.strike_ladder(st, date_yy, before_iso=now.isoformat())
        if not ladder:
            notes.append(f"skip {st} {date}: empty ladder")
            continue
        frac = max(0.0, min(1.0, (now - dstart).total_seconds() / (dend - dstart).total_seconds()))
        state = from_ws_p(dist, st, ens_mean=ens[0], ens_std=ens[1],
                          month=int(date[5:7]), date=date)
        state = update_from_intraday(state, hsf, frac)

        try:
            cdf = mff.build_analog_cdf_today(st, date, high_so_far=hsf)
        except Exception as e:
            cdf = None
            notes.append(f"{st} {date}: analog build err {type(e).__name__}")
        traj_p = make_traj_p_fn(st, dstart, dend, now, date, shared)

        p_fns: Dict[str, Optional[Callable]] = {
            "gefs_only": make_gefs_p_fn(state),
            "analog_only": make_analog_p_fn(cdf) if cdf is not None else None,
            "trajectory_only": (lambda x, _p=traj_p: _p(x, hsf)) if traj_p is not None else None,
        }
        # fused needs all three
        fused_meta_fn = None
        if cdf is not None and traj_p is not None:
            def fused_meta(x: float, _s=state, _c=cdf, _t=traj_p, _h=hsf) -> Dict:
                return mff.fuse_strike(_s.p_ge, _c.p_high_ge, lambda xx: _t(xx, _h), x)
            p_fns["fused"] = lambda x, _m=fused_meta: _m(x)["p_fused"]
            fused_meta_fn = fused_meta
        else:
            p_fns["fused"] = None

        # per-arm edge multiplier: fused uses its agreement-scaled edge_mult at
        # the picked strike (live rule); single-forecaster arms use 1.0
        def _em_fused(x: float) -> float:
            return fused_meta_fn(x)["edge_mult"] if fused_meta_fn else 1.0
        em_fns = {a: (lambda x: 1.0) for a in arms}
        if fused_meta_fn is not None:
            em_fns["fused"] = _em_fused

        result = {"station": st, "date": date, "decision_ts": now.isoformat(),
                  "booked_strike": t["booked_strike"], "settled_f": t["settled_f"],
                  "booked_pnl": t["realized_pnl"], "trade_price": t["trade_price"],
                  "quantity": t["quantity"], "trade_type": t["trade_type"],
                  "arms": {}}
        for arm in arms:
            fn = p_fns.get(arm)
            if fn is None:
                result["arms"][arm] = {"status": "unavailable"}
                continue
            dec = run_arm(fn, em_fns[arm], state, ladder, now)
            if dec is None:
                result["arms"][arm] = {"status": "no_trade"}
                continue
            win = settled_win(dec["side"], t["settled_f"], dec["strike"])
            gross = dec["qty"] * (1.0 if win else 0.0) - dec["cost"]
            fee = fee_per_contract(dec["price"]) * dec["qty"]
            result["arms"][arm] = {
                "status": "traded", "strike": dec["strike"], "side": dec["side"],
                "price": dec["price"], "qty": dec["qty"], "cost": dec["cost"],
                "our_prob": dec["our_prob"], "win": win,
                "pnl": gross, "pnl_fee": gross - fee, "fee": fee,
                "ev": dec["ev"]}
        rows.append(result)
    return rows, notes


# =============================================================================
# Metrics
# =============================================================================
def metrics(rows: List[Dict], arm: str) -> Dict:
    tds = [(r["date"], a) for r in rows for a in [r["arms"].get(arm)] if a and a.get("status") == "traded"]
    if not tds:
        return {"n": 0}
    pnls = [a["pnl"] for _, a in tds]
    pnls_fee = [a["pnl_fee"] for _, a in tds]
    wins = sum(1 for _, a in tds if a["win"])
    daily = defaultdict(float)
    daily_fee = defaultdict(float)
    for d, a in tds:
        daily[d] += a["pnl"]
        daily_fee[d] += a["pnl_fee"]

    def sharpe(dmap):
        rets = []
        for d in sorted(dmap):
            rets.append(dmap[d] / BANKROLL)
        if len(rets) < 2:
            return 0.0
        m = float(np.mean(rets)); s = float(np.std(rets, ddof=1))
        return (m / s * math.sqrt(252)) if s > 0 else 0.0

    def max_dd(pnl_series):
        cum = 0.0; peak = 0.0; dd = 0.0
        for p in pnl_series:
            cum += p
            peak = max(peak, cum)
            dd = max(dd, peak - cum)
        return dd

    chron = [a["pnl"] for d, a in sorted(tds, key=lambda x: x[0])]
    chron_fee = [a["pnl_fee"] for d, a in sorted(tds, key=lambda x: x[0])]
    return {
        "n": len(tds), "wins": wins, "win_rate": wins / len(tds),
        "net_pnl": round(sum(pnls), 2), "net_pnl_fee": round(sum(pnls_fee), 2),
        "avg_pnl": round(sum(pnls) / len(tds), 2),
        "sharpe": round(sharpe(daily), 2), "sharpe_fee": round(sharpe(daily_fee), 2),
        "max_dd": round(max_dd(chron), 2), "max_dd_fee": round(max_dd(chron_fee), 2),
        "total_fees": round(sum(a["fee"] for _, a in tds), 2),
    }


def booked_metrics(rows: List[Dict]) -> Dict:
    """Reference: the live book itself (all settled rows)."""
    pnls = [r["booked_pnl"] for r in rows]
    wins = sum(1 for r in rows if r["booked_pnl"] > 0)
    daily = defaultdict(float)
    for r in rows:
        daily[r["date"]] += r["booked_pnl"]
    rets = [daily[d] / BANKROLL for d in sorted(daily)]
    sh = 0.0
    if len(rets) >= 2:
        m = float(np.mean(rets)); s = float(np.std(rets, ddof=1))
        sh = (m / s * math.sqrt(252)) if s > 0 else 0.0
    cum = 0.0; peak = 0.0; dd = 0.0
    for r in sorted(rows, key=lambda x: x["decision_ts"]):
        cum += r["booked_pnl"]; peak = max(peak, cum); dd = max(dd, peak - cum)
    fees = sum(fee_per_contract(r["trade_price"]) * r["quantity"] for r in rows)
    return {"n": len(rows), "wins": wins, "win_rate": wins / len(rows),
            "net_pnl": round(sum(pnls), 2), "net_pnl_fee": round(sum(pnls) - fees, 2),
            "avg_pnl": round(sum(pnls) / len(rows), 2), "sharpe": round(sh, 2),
            "max_dd": round(dd, 2), "total_fees": round(fees, 2)}


def render(rows: List[Dict], notes: List[str]) -> str:
    arms = ["fused", "gefs_only", "analog_only", "trajectory_only"]
    bk = booked_metrics(rows)
    lines = []
    lines.append("# F3 — GEFS-alone vs fusion: real P&L (68 settled SBOX trades)\n")
    lines.append(f"Replayed {len(rows)} settled trades at their decision timestamps.\n")
    hdr = ("| Strategy | n | Win rate | Net P&L (no fee) | Net P&L (fee) | "
           "Sharpe | Max DD ($) | Avg/trade |")
    sep = "|---|---|---|---|---|---|---|---|"
    lines.append(hdr); lines.append(sep)

    def fmt_row(name, m):
        if m["n"] == 0:
            return f"| {name} | 0 | — | — | — | — | — | — |"
        return (f"| {name} | {m['n']} | {m['win_rate']*100:.1f}% | "
                f"${m['net_pnl']:,.2f} | ${m['net_pnl_fee']:,.2f} | {m['sharpe']:.2f} | "
                f"${m['max_dd']:,.2f} | ${m['avg_pnl']:,.2f} |")

    lines.append(fmt_row("**fused (live)**", bk))
    for a in arms:
        lines.append(fmt_row(f"{a}", metrics(rows, a)))
    lines.append("")
    lines.append("## Reconstruction validity")
    mf = metrics(rows, "fused")
    lines.append(f"- Booked (live) net P&L: ${bk['net_pnl']:,.2f} on {bk['n']} trades.")
    lines.append(f"- Replayed fused net P&L: ${mf['net_pnl']:,.2f} on {mf['n']} trades "
                 f"(same strikes/decisions if reconstruction is faithful).")
    lines.append("")
    lines.append("## Per-strategy arm status")
    for a in arms:
        st = defaultdict(int)
        for r in rows:
            v = r["arms"].get(a, {"status": "missing"})
            st[v.get("status", "missing")] += 1
        lines.append(f"- {a}: {dict(st)}")
    if notes:
        lines.append("\n## Notes / skips")
        for n in notes[:40]:
            lines.append(f"- {n}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades-db", default=str(DEFAULT_TRADES_DB),
                    help="private COPY of the SBOX trades DB (never the live book)")
    ap.add_argument("--json-out", default="")
    ap.add_argument("--md-out", default="")
    args = ap.parse_args()

    trades = load_trades(args.trades_db)
    rows, notes = replay(trades)
    md = render(rows, notes)
    print(md)
    if args.md_out:
        Path(args.md_out).write_text(md)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(
            {"rows": rows, "notes": notes,
             "metrics": {a: metrics(rows, a) for a in
                         ["fused", "gefs_only", "analog_only", "trajectory_only"]},
             "booked": booked_metrics(rows)}, indent=2))


if __name__ == "__main__":
    main()
