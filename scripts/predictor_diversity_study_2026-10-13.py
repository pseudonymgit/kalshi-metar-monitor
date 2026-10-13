#!/usr/bin/env python3
"""
predictor_diversity_study_2026-10-13.py — are our independent forecast sources
actually diverse, and does combining them beat any single one?

READ-ONLY on the live book. Writes ONLY:
  * artifacts/predictor-diversity/predictor_diversity_2026-10-13.json  (results)
  * trade_calibration.p_market (additive column, 0/143 -> filled)  [--write-p-market]

WHAT IT DOES
------------
Task 1  Per-source accuracy on the STRICT strike-crossing axis
        (label `crossed = settled_max > strike`; `==` is a LOSS).
          ecmwf / gefs / gfs / icon / gem  (data/nwp_forecasts.db, °C -> °F)
          rate_1h   (derived from data/iem_asos_1min.db, native °F, NO C->F)
          market    (data/kalshi_market_snapshots.db mid at the 13:30 pin)
Task 2  Pairwise correlation of the per-station-day forecasts (P(cross) and,
        for NWP, the underlying temperature in °F). Flag |r| > 0.85 as
        "effectively one predictor".
Task 3  Equal-weight blend of the sources that survive the correlation filter
        vs the best single source, on the overlapping station-days.
Task 4  Fill trade_calibration.p_market from the snapshots using the SBOX
        bridge's market-mid resolution (replicated read-only).

UNITS — VERIFIED
  * data/nwp_forecasts.db values are DEGREES CELSIUS -> *9/5+32 (checked: KNYC
    2026-08-01 ecmwf tmax 28.7C = 83.7F, matches a late-July NYC high).
  * data/iem_asos_1min.db.observations.tmpf is ALREADY Fahrenheit. Do NOT C->F.
    (The load_intraday_hsf double-conversion bug is exactly this trap.)

THE STRIKE
  Uniform across sources: T = floor(day-of-year climatological mean_high) + 1,
  the next integer above the seasonal normal. Market is interpolated to that T
  from its ladder near the 13:30 station-local decision pin.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from core.station_time import station_timezone_name  # noqa: E402

NWP = REPO / "data" / "nwp_forecasts.db"
ASOS = REPO / "data" / "iem_asos_1min.db"
SNAPS = REPO / "data" / "kalshi_market_snapshots.db"
METAR = REPO / "data" / "metar_backfill.db"
SBOX = REPO / "data" / "paper_trading_sbox.db"
OUT = REPO / "artifacts" / "predictor-diversity" / "predictor_diversity_2026-10-13.json"

MIGRATION_END = "2026-10-09"       # last canonical ASOS settlement day
MIGRATION_START = "2026-04-21"     # ecmwf backfill start
SIGMA_DET_F = 4.0                  # deterministic-NWP daily-max sigma (°F)
SIGMA_GEFS_MIN = 1.5               # GEFS ensemble-sigma floor
SIGMA_RATE = 1.5                   # rate_1h nowcast sigma (°F)
ROLL_FACTOR = 2.5                  # deflate the 1h rate by the 45-min roll
ROLL_FLOOR = 1.0
RATE_CAP = 8.0
CORR_FLAG = 0.85
DECISION_PIN_LOCAL_HHMM = "13:30:00"
DECISION_PIN_MOD = 13 * 60 + 30        # 13:30 station-local, minutes of day
# intraday window (station-local minutes-of-day) over which we take the BEST
# rate_1h nowcast for the day (13:00 .. 16:59); rate needs 4 prior buckets.
RATE_MIN_MOD = 13 * 60
RATE_MAX_MOD = 16 * 60 + 45
BUCKET_MIN = 15

STATIONS = [
    "KATL", "KAUS", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU", "KLAS", "KLAX",
    "KMDW", "KMIA", "KMSP", "KMSY", "KNYC", "KOKC", "KPHL", "KPHX", "KSAT",
    "KSEA", "KSFO",
]


def ro(p: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def p_ge(mu: float, sigma: float, x: float) -> float:
    if sigma <= 0:
        return 1.0 if mu >= x else 0.0
    return cdf((mu - x) / sigma)


def brier(ps, ys) -> float:
    return sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(ps)


def dir_acc(ps, ys) -> float:
    return sum(1 for p, y in zip(ps, ys) if (p > 0.5) == bool(y)) / len(ps)


def calibration_metrics(ps, ys, n_bins=10):
    """Reliability + Murphy Brier decomposition for P(cross) vs settled truth.

    PRIMARY evidence for the crossing-probability axis:
      brier        = mean((p - y)^2)
      reliability  = sum_k n_k/N (p_k - o_k)^2      (calibration;  LOWER better)
      resolution   = sum_k n_k/N (o_k - o_base)^2   (discrimination; HIGHER better)
      uncertainty  = o_base (1 - o_base)            (irreducible)
      brier        = reliability - resolution + uncertainty
      ece          = sum_k n_k/N |p_k - o_k|        (expected calibration error)
      mce          = max_k |p_k - o_k|              (max calibration error)
    Bins are FIXED probability deciles (0.0,0.1,...,1.0] so they are comparable
    across sources regardless of each source's p-distribution.
    """
    n = len(ps)
    base = sum(ys) / n
    edges = [i / n_bins for i in range(n_bins + 1)]
    bins = [[] for _ in range(n_bins)]
    for p, y in zip(ps, ys):
        idx = min(n_bins - 1, max(0, int(p * n_bins)))
        bins[idx].append((p, y))
    rel = res = ece = 0.0
    mce = 0.0
    table = []
    for i, b in enumerate(bins):
        if not b:
            table.append({"bin": f"{edges[i]:.1f}-{edges[i+1]:.1f}", "n": 0,
                          "mean_p": None, "emp_rate": None, "gap": None})
            continue
        nk = len(b)
        pk = sum(p for p, _ in b) / nk
        ok = sum(y for _, y in b) / nk
        rel += nk / n * (pk - ok) ** 2
        res += nk / n * (ok - base) ** 2
        ece += nk / n * abs(pk - ok)
        mce = max(mce, abs(pk - ok))
        table.append({"bin": f"{edges[i]:.1f}-{edges[i+1]:.1f}", "n": nk,
                      "mean_p": round(pk, 4), "emp_rate": round(ok, 4),
                      "gap": round(pk - ok, 4)})
    return {"reliability": round(rel, 4), "resolution": round(res, 4),
            "uncertainty": round(base * (1 - base), 4),
            "ece": round(ece, 4), "mce": round(mce, 4),
            "calibration_table": table}


def pearson(xs, ys) -> float:
    n = len(xs)
    if n < 2:
        return float("nan")
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return float("nan")
    return sxy / math.sqrt(sxx * syy)


def _rank(vals):
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    r = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r


def spearman(xs, ys) -> float:
    return pearson(_rank(xs), _rank(ys))


# ---------------------------------------------------------------------------
# climatology -> strike
# ---------------------------------------------------------------------------
def load_clim():
    """{(station, doy): mean_high_F} climatology over 1991-2020.

    Built from canonical GHCN settlement truth (settlement_truth, HIGH,
    trustworthy=1) rather than the `station_climatology` table, which contains
    151 corrupt mean_high rows (values up to ~10,900 F from a mixed-unit
    source). Values outside [20, 130] F are rejected.
    """
    c = ro(METAR)
    acc = defaultdict(lambda: [0.0, 0])
    for st, ds, v in c.execute(
            "SELECT station, local_trading_date, settlement_temp_f FROM settlement_truth "
            "WHERE market_type='HIGH' AND trustworthy=1 AND settlement_temp_f IS NOT NULL "
            "AND local_trading_date >= '1991-01-01' AND local_trading_date <= '2020-12-31'"):
        v = float(v)
        if v < 20.0 or v > 130.0:
            continue
        try:
            doy = date.fromisoformat(ds).timetuple().tm_yday
        except ValueError:
            continue
        a = acc[(st, doy)]
        a[0] += v
        a[1] += 1
    c.close()
    out = {k: a[0] / a[1] for k, a in acc.items() if a[1] > 0}
    # fill any missing day-of-year by interpolating the station's own curve
    by_st = defaultdict(dict)
    for (st, doy), v in out.items():
        by_st[st][doy] = v
    for st, curve in by_st.items():
        for doy in range(1, 367):
            if (st, doy) in out or not curve:
                continue
            near = min(curve, key=lambda d: min(abs(d - doy), 366 - abs(d - doy)))
            out[(st, doy)] = curve[near]
    return out


def load_truth():
    """{(station, date): canonical settled max F} from settlement_truth (HIGH)."""
    c = ro(METAR)
    out = {}
    for st, ds, v in c.execute(
            "SELECT station, local_trading_date, settlement_temp_f FROM settlement_truth "
            "WHERE market_type='HIGH' AND trustworthy=1 AND settlement_temp_f IS NOT NULL"):
        out[(st, ds)] = float(v)
    c.close()
    return out


def strike_for(clim, station, ds):
    """Climatological next-integer-above-normal strike (fallback when ASOS is absent)."""
    try:
        d = date.fromisoformat(ds)
    except ValueError:
        return None
    v = clim.get((station, d.timetuple().tm_yday))
    if v is None:
        return None
    return float(math.floor(v) + 1.0)


def hsf_at_pin(rec):
    """Max 15-min bucket MEAN with minute-of-day <= the 13:30 decision pin.

    Bucket means (not raw 1-min max) so single-minute ASOS spikes (observed
    KSEA +12 F on 2026-08-01) do not inflate the running high.
    """
    if not rec:
        return None
    buckets = rec["buckets"]
    vals = [buckets[b] for b in buckets if b <= DECISION_PIN_MOD]
    return max(vals) if vals else None


# ---------------------------------------------------------------------------
# ASOS -> settled max + rate_1h nowcast
# ---------------------------------------------------------------------------
def load_asos():
    """Scan the 20M-row ASOS 1-min archive once, in STATION-LOCAL time.

    `observations.valid` is UTC. Both the local trading date and the local
    minute-of-day matter (the decision pin is 13:30 station-local), so every
    timestamp is shifted to the station's local time before bucketing.

    Returns {(station, local_date): {'dmax': F, 'buckets': {local_mod: mean_F}}}.
    """
    tz = {st: ZoneInfo(station_timezone_name(st)) for st in STATIONS}
    offcache = {}    # (station, utc_date, utc_hour) -> offset seconds
    bs = {}          # (station, ldate, mod) -> [sum, n]
    dmax = {}        # (station, ldate) -> max tmpf
    c = ro(ASOS)
    cur = c.execute("SELECT station, valid, tmpf FROM observations WHERE tmpf IS NOT NULL")
    n = 0
    for st, valid, tmpf in cur:
        n += 1
        if n % 5_000_000 == 0:
            sys.stderr.write(f"  asos rows scanned: {n}\n")
            sys.stderr.flush()
        tzs = tz.get(st)
        if tzs is None:
            continue
        try:
            ds = valid[0:10]
            hh = int(valid[11:13])
            mm = int(valid[14:16])
        except (ValueError, TypeError):
            continue
        key = (st, ds, hh)
        off = offcache.get(key)
        if off is None:
            try:
                rep = datetime.strptime(ds + f" {hh:02d}:00", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                off = int(rep.astimezone(tzs).utcoffset().total_seconds())
            except Exception:
                off = 0
            offcache[key] = off
        tot = hh * 60 + mm + off // 60
        ldate = (date.fromisoformat(ds) + timedelta(days=tot // 1440)).isoformat()
        mod = ((tot % 1440) // BUCKET_MIN) * BUCKET_MIN
        k = (st, ldate, mod)
        a = bs.get(k)
        if a is None:
            bs[k] = [tmpf, 1]
        else:
            a[0] += tmpf
            a[1] += 1
        mk = (st, ldate)
        v = dmax.get(mk)
        if v is None or tmpf > v:
            dmax[mk] = tmpf
    c.close()

    buckets = defaultdict(dict)
    for (st, ldate, mod), (s, cnt) in bs.items():
        buckets[(st, ldate)][mod] = s / cnt

    out = {}
    for k in set(list(dmax.keys()) + list(buckets.keys())):
        out[k] = {"dmax": dmax.get(k), "buckets": buckets.get(k, {})}
    return out


def _rate_and_roll(buckets, mod):
    """rate_1h (mean - mean of 4 prior 15-min buckets) and the 45-min roll.

    roll = mean(mod-30 .. mod+15) (4 buckets centred on `mod`); used to deflate
    the 1h rate so the daily-max rise is not double-counted by the nowcast.
    """
    need_rate = [mod - 60, mod - 45, mod - 30, mod - 15, mod]
    if any(b not in buckets for b in need_rate):
        return None, None
    rate = buckets[mod] - sum(buckets[b] for b in need_rate[:-1]) / 4.0
    need_roll = [mod - 30, mod - 15, mod, mod + 15]
    roll = (sum(buckets[b] for b in need_roll) / 4.0
            if all(b in buckets for b in need_roll) else None)
    return rate, roll


def deflate(rate, roll):
    if rate is None:
        return None
    if roll is not None and roll > 0.0 and rate > 0.0:
        return min(rate / max(ROLL_FLOOR, ROLL_FACTOR * roll), RATE_CAP)
    return min(rate, RATE_CAP)


def rate_nowcast(rec, strike):
    """Best (largest) P(cross) from the rate_1h nowcast over the intraday window.

    mu = high_so_far + deflated rate_1h ; P(cross) = P(high >= strike) = Phi((mu-strike)/sigma).
    """
    if rec is None or strike is None:
        return None
    buckets = rec["buckets"]
    if not buckets:
        return None
    mods = sorted(buckets)
    best = None
    first = None
    for mod in range(RATE_MIN_MOD, RATE_MAX_MOD + 1, BUCKET_MIN):
        if mod not in buckets:
            continue
        hsf = max(buckets[b] for b in mods if b <= mod)
        if hsf <= 0:
            continue
        rate, roll = _rate_and_roll(buckets, mod)
        d = deflate(rate, roll)
        if d is None:
            continue
        mu = hsf + d
        p = p_ge(mu, SIGMA_RATE, strike)
        item = (p, mod, rate, roll, hsf, mu)
        if first is None:
            first = item
        if best is None or p > best[0]:
            best = item
    if best is None:
        return None
    return {"p": best[0], "mod": best[1], "rate": best[2], "roll": best[3],
            "high_so_far": best[4], "mu": best[5],
            "p_first": first[0], "mod_first": first[1]}


# ---------------------------------------------------------------------------
# NWP
# ---------------------------------------------------------------------------
def load_nwp():
    c = ro(NWP)
    det = {}
    for model in ("ecmwf", "gfs", "icon", "gem"):
        m = {}
        for st, td, v in c.execute(
                "SELECT station, target_date, value FROM nwp_forecasts "
                "WHERE model=? AND variable='temperature_2m_max'", (model,)):
            if v is not None:
                m[(st, td)] = float(v) * 9.0 / 5.0 + 32.0
        det[model] = m
    # GEFS ensemble: dedupe by member variable name, then mean/std.
    members = defaultdict(dict)
    for st, td, var, v in c.execute(
            "SELECT station, target_date, variable, value FROM nwp_forecasts "
            "WHERE model='gefs_ens' AND variable LIKE 'temperature_2m_max_member%'"):
        if v is not None:
            members[(st, td)][var] = float(v) * 9.0 / 5.0 + 32.0
    c.close()
    gefs = {}
    for k, d in members.items():
        vals = list(d.values())
        n = len(vals)
        mu = sum(vals) / n
        var = sum((x - mu) ** 2 for x in vals) / n
        gefs[k] = {"mu": mu, "sd": math.sqrt(var), "n": n}
    return det, gefs


# ---------------------------------------------------------------------------
# Market snapshots -> per-(station,date) ladder of mid at the 13:30 pin
# ---------------------------------------------------------------------------
def load_market():
    """Per-(station, local_date) ladder of live YES mids at the 13:30 pin.

    A quote counts only when it is NON-DEGENERATE (yes_bid > 0 and yes_ask < 1,
    mid strictly inside (0,1)) AND its snapshot falls on the TARGET station-local
    date (mirrors scripts/market_benchmark_contract.py align='local'). Without
    the date constraint the nearest-to-pin lookup silently attaches stale or
    placeholder quotes from other days (observed: mid=0.500 rows stamped
    2026-09-18 attached to 2026-09-11..17).
    """
    c = ro(SNAPS)
    city = {}
    for st, pref in c.execute(
            "SELECT DISTINCT station, substr(market_ticker,1,instr(market_ticker,'-')-1) "
            "FROM market_snapshots WHERE market_ticker LIKE 'KXHIGH%-T%'"):
        city[pref] = st
    rows = c.execute(
        "SELECT market_ticker, mid_price, snapshot_time, station FROM market_snapshots "
        "WHERE market_ticker LIKE 'KXHIGH%-T%' AND market_type='binary' "
        "AND mid_price IS NOT NULL AND mid_price > 0.0 AND mid_price < 1.0 "
        "AND yes_bid > 0 AND yes_ask < 1").fetchall()
    c.close()

    MON = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
           "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
    by_ticker = defaultdict(list)
    for tk, mid, stt, st in rows:
        by_ticker[tk].append((stt, float(mid), st))

    pins = {}

    def pin_epoch(st, ds):
        key = (st, ds)
        if key not in pins:
            try:
                tz = ZoneInfo(station_timezone_name(st))
                loc = datetime.fromisoformat(f"{ds}T{DECISION_PIN_LOCAL_HHMM}").replace(tzinfo=tz)
                pins[key] = loc.timestamp()
            except Exception:
                pins[key] = None
        return pins[key]

    ladders = defaultdict(dict)   # (station, date) -> {strike: (mid, snap_time)}
    for tk, pts in by_ticker.items():
        parts = tk.split("-")
        if len(parts) < 3:
            continue
        st = city.get(parts[0])
        if st is None:
            continue
        code = parts[1]        # 26SEP09
        tpart = parts[2]       # T91
        if not tpart.startswith("T"):
            continue
        try:
            yy = 2000 + int(code[0:2])
            mon = MON[code[2:5].upper()]
            dd = int(code[5:7])
            strike = float(tpart[1:])
        except (ValueError, KeyError):
            continue
        ds = f"{yy:04d}-{mon:02d}-{dd:02d}"
        pe = pin_epoch(st, ds)
        if pe is None:
            continue
        # keep only quotes whose station-local date equals the target date
        on_date = [(stt, mid) for stt, mid, _ in pts if _local_date(stt, st) == ds]
        if not on_date:
            continue
        best = min(on_date, key=lambda sp: abs(_epoch(sp[0]) - pe))
        ladders[(st, ds)][strike] = (best[1], best[0])
    return ladders


def _local_date(snap_time: str, st: str):
    try:
        dt = datetime.fromisoformat(snap_time)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo(station_timezone_name(st))).date().isoformat()
    except Exception:
        return None


def _epoch(s: str) -> float:
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return 0.0


def market_p_at(ladder, strike):
    """Mid implied P(cross) at `strike`, interpolating the ladder if needed."""
    if not ladder or strike is None:
        return None
    if strike in ladder:
        return ladder[strike][0]
    lo = max((t for t in ladder if t < strike), default=None)
    hi = min((t for t in ladder if t > strike), default=None)
    if lo is not None and hi is not None and hi > lo:
        plo, phi = ladder[lo][0], ladder[hi][0]
        return plo + (phi - plo) * (strike - lo) / (hi - lo)
    if lo is not None:
        return ladder[lo][0]
    if hi is not None:
        return ladder[hi][0]
    return None


# ---------------------------------------------------------------------------
# Build the aligned grid
# ---------------------------------------------------------------------------
def build_grid():
    clim = load_clim()
    truth = load_truth()
    asos = load_asos()
    det, gefs = load_nwp()
    ladders = load_market()

    d0 = date.fromisoformat(MIGRATION_START)
    d1 = date.fromisoformat(MIGRATION_END)
    days = []
    d = d0
    while d <= d1:
        days.append(d.isoformat())
        d += timedelta(days=1)

    grid = {}   # (st, ds) -> dict
    for st in STATIONS:
        for ds in days:
            rec = asos.get((st, ds))
            hsf = hsf_at_pin(rec)
            if hsf is not None:
                # strike-crossing axis: next integer strictly above the 13:30
                # station-local running high (matches the rate_1h sweep).
                T = float(math.floor(hsf) + 1.0)
            else:
                T = strike_for(clim, st, ds)
            if T is None:
                continue
            settled = truth.get((st, ds))   # canonical truth, NOT raw ASOS max
            row = {"station": st, "date": ds, "strike": T,
                   "settled_max": settled,
                   "hsf_pin": hsf,
                   "asos_max": rec["dmax"] if rec else None,
                   "y": (None if settled is None else (1 if settled > T else 0))}
            for model in ("ecmwf", "gfs", "icon", "gem"):
                mu = det[model].get((st, ds))
                row[model] = mu
                row[model + "_p"] = p_ge(mu, SIGMA_DET_F, T) if mu is not None else None
            g = gefs.get((st, ds))
            if g is not None:
                row["gefs"] = g["mu"]
                row["gefs_sd"] = g["sd"]
                row["gefs_n"] = g["n"]
                row["gefs_p"] = p_ge(g["mu"], max(SIGMA_GEFS_MIN, g["sd"]), T)
            else:
                row["gefs"] = row["gefs_sd"] = row["gefs_n"] = row["gefs_p"] = None
            rn = rate_nowcast(rec, T)
            row["rate_1h"] = rn["mu"] if rn else None
            row["rate_1h_p"] = rn["p"] if rn else None
            row["rate_1h_first_p"] = rn["p_first"] if rn else None
            mp = market_p_at(ladders.get((st, ds)), T)
            row["market"] = mp
            row["market_p"] = mp
            grid[(st, ds)] = row
    return grid


# ---------------------------------------------------------------------------
# Tasks 1-3
# ---------------------------------------------------------------------------
SOURCES = ["ecmwf", "gefs", "gfs", "icon", "gem", "rate_1h", "market"]


def task1(grid):
    out = {}
    for s in SOURCES:
        pk = s + "_p"
        rows = [r for r in grid.values() if r.get(pk) is not None and r.get("y") is not None]
        if not rows:
            out[s] = {"n": 0}
            continue
        ps = [r[pk] for r in rows]
        ys = [r["y"] for r in rows]
        dates = sorted(r["date"] for r in rows)
        cal = calibration_metrics(ps, ys)
        out[s] = {
            "n": len(rows),
            "brier": round(brier(ps, ys), 4),
            "dir_acc": round(dir_acc(ps, ys), 4),
            "base_rate": round(sum(ys) / len(ys), 4),
            "date_min": dates[0], "date_max": dates[-1],
            "mean_p": round(sum(ps) / len(ps), 4),
            "reliability": cal["reliability"],
            "resolution": cal["resolution"],
            "uncertainty": cal["uncertainty"],
            "ece": cal["ece"],
            "mce": cal["mce"],
            "calibration_table": cal["calibration_table"],
        }
    return out


def corr_matrix(grid, keyfmt):
    """Pearson + Spearman over the union of rows where BOTH sources have a value."""
    mat = {}
    for a in SOURCES:
        for b in SOURCES:
            if a >= b:
                continue
            rows = [r for r in grid.values()
                    if r.get(keyfmt.format(a)) is not None and r.get(keyfmt.format(b)) is not None]
            if len(rows) < 3:
                mat[f"{a}|{b}"] = {"n": len(rows), "pearson": None, "spearman": None,
                                   "flag": False}
                continue
            xs = [r[keyfmt.format(a)] for r in rows]
            ys = [r[keyfmt.format(b)] for r in rows]
            rp = pearson(xs, ys)
            rs = spearman(xs, ys)
            flag = (abs(rp) > CORR_FLAG) if rp == rp else False
            mat[f"{a}|{b}"] = {"n": len(rows),
                               "pearson": None if rp != rp else round(rp, 4),
                               "spearman": None if rs != rs else round(rs, 4),
                               "flag": flag}
    return mat


def task3(grid, t1, pmat):
    """Greedy independent set by Brier; equal-weight blend vs best single."""
    order = sorted([s for s in SOURCES if t1[s].get("brier") is not None],
                   key=lambda s: t1[s]["brier"])
    chosen = []
    for s in order:
        ok = True
        for t in chosen:
            key = f"{s}|{t}" if s < t else f"{t}|{s}"
            r = pmat.get(key, {}).get("pearson")
            if r is not None and abs(r) > CORR_FLAG:
                ok = False
                break
        if ok:
            chosen.append(s)
    # overlap rows where ALL chosen have a value and truth exists
    rows = [r for r in grid.values()
            if r.get("y") is not None and all(r.get(s + "_p") is not None for s in chosen)]
    res = {"chosen": chosen, "n_overlap": len(rows), "best_single": order[0] if order else None}
    if not rows:
        return res
    ys = [r["y"] for r in rows]
    for s in chosen:
        res[f"single_brier_{s}"] = round(brier([r[s + "_p"] for r in rows], ys), 4)
    blend = [sum(r[s + "_p"] for s in chosen) / len(chosen) for r in rows]
    res["blend_brier"] = round(brier(blend, ys), 4)
    res["blend_dir_acc"] = round(dir_acc(blend, ys), 4)
    if order:
        b = order[0]
        res["best_single_brier"] = round(brier([r[b + "_p"] for r in rows], ys), 4)
        res["best_single_dir_acc"] = round(dir_acc([r[b + "_p"] for r in rows], ys), 4)
        res["blend_minus_best_single"] = round(res["blend_brier"] - res["best_single_brier"], 4)
    # targeted blends over the genuinely-independent survivors and the best NWP
    nwp_rank = sorted([s for s in ("gfs", "icon", "gem", "ecmwf", "gefs")
                       if t1[s].get("brier") is not None], key=lambda s: t1[s]["brier"])
    best_nwp = nwp_rank[0] if nwp_rank else "gfs"
    subsets = {
        "fixed3": ["gefs", "rate_1h", "market"],
        "nwp_best+rate+market": [best_nwp, "rate_1h", "market"],
        "rate+market": ["rate_1h", "market"],
        "nwp_best+rate": [best_nwp, "rate_1h"],
    }
    for name, members in subsets.items():
        frows = [r for r in grid.values()
                 if r.get("y") is not None and all(r.get(s + "_p") is not None for s in members)]
        if len(frows) < 5:
            continue
        fys = [r["y"] for r in frows]
        fblend = [sum(r[s + "_p"] for s in members) / len(members) for r in frows]
        bs = []
        for s in members:
            bs.append((s, brier([r[s + "_p"] for r in frows], fys)))
        bs.sort(key=lambda t: t[1])
        res[name] = {"members": members, "n": len(frows),
                     "blend_brier": round(brier(fblend, fys), 4),
                     "blend_dir_acc": round(dir_acc(fblend, fys), 4),
                     "best_member": bs[0][0],
                     "best_member_brier": round(bs[0][1], 4),
                     "blend_minus_best_member": round(brier(fblend, fys) - bs[0][1], 4)}
    return res


# ---------------------------------------------------------------------------
# Task 4 — p_market
# ---------------------------------------------------------------------------
def resolve_p_market():
    from scripts.sbox_intraday_ev_bridge import pinned_decision_utc, market_mid_prob
    c = ro(SBOX)
    rows = c.execute(
        "SELECT trade_uuid, station, trade_date_utc, ticker FROM trade_calibration").fetchall()
    c.close()
    out = {}
    for uuid, st, day, tk in rows:
        pin = pinned_decision_utc(st, day)
        out[uuid] = market_mid_prob(tk, pin) if tk else None
    return out


def write_p_market():
    vals = resolve_p_market()
    c = sqlite3.connect(SBOX)
    try:
        n = 0
        for uuid, pm in vals.items():
            c.execute("UPDATE trade_calibration SET p_market=? WHERE trade_uuid=?", (pm, uuid))
            if pm is not None:
                n += 1
        c.commit()
        total = c.execute("SELECT COUNT(*) FROM trade_calibration").fetchone()[0]
        filled = c.execute(
            "SELECT COUNT(*) FROM trade_calibration WHERE p_market IS NOT NULL").fetchone()[0]
    finally:
        c.close()
    return {"rows": total, "p_market_filled": filled, "resolved_nonnull": n}


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write-p-market", action="store_true",
                    help="fill trade_calibration.p_market (additive column)")
    args = ap.parse_args()

    if args.write_p_market:
        print(json.dumps(write_p_market(), indent=2))
        return

    sys.stderr.write("building grid (ASOS scan ~60-90s)...\n")
    grid = build_grid()
    sys.stderr.write(f"grid station-days: {len(grid)}\n")

    t1 = task1(grid)
    pmat = corr_matrix(grid, "{0}_p")
    tmat = corr_matrix(grid, "{0}")
    t3 = task3(grid, t1, pmat)

    pm = resolve_p_market()
    n_pm = sum(1 for v in pm.values() if v is not None)

    out = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "window": [MIGRATION_START, MIGRATION_END],
        "strike_rule": "T = floor(high-so-far at 13:30 station-local pin) + 1 (climatology fallback if ASOS absent)",
        "label_rule": "crossed = settled_max > T (strict; == is a LOSS)",
        "primary_metric": "Brier + calibration of P(cross) vs settled truth",
        "sigma_det_F": SIGMA_DET_F, "sigma_gefs_min": SIGMA_GEFS_MIN,
        "sigma_rate": SIGMA_RATE,
        "task1_per_source": t1,
        "task2_corr_pcross": pmat,
        "task2_corr_temp": tmat,
        "task3_blend": t3,
        "task4_p_market": {"rows": len(pm), "resolved_nonnull": n_pm},
        "grid_rows": len(grid),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2))

    # ---- console report ----
    print("=" * 92)
    print("TASK 1 (PRIMARY) — P(cross) Brier + calibration vs settled truth")
    print("=" * 92)
    print(f"{'source':10} {'n':>6} {'Brier':>8} {'ECE':>7} {'MCE':>7} "
          f"{'reliab':>7} {'resol':>7} {'base':>6} {'mean_p':>7}  range")
    for s in SOURCES:
        r = t1[s]
        if r.get("n", 0) == 0:
            print(f"{s:10} {'0':>6}")
            continue
        print(f"{s:10} {r['n']:>6} {r['brier']:>8.4f} {r['ece']:>7.4f} {r['mce']:>7.4f} "
              f"{r['reliability']:>7.4f} {r['resolution']:>7.4f} {r['base_rate']:>6.3f} "
              f"{r['mean_p']:>7.3f}  {r['date_min']}..{r['date_max']}")
    print("  (Brier = reliability - resolution + uncertainty; LOWER Brier/ECE/reliab better,")
    print("   HIGHER resolution better. dir. acc is secondary: see results JSON.)")
    print()
    print("=" * 92)
    print("TASK 1b (SECONDARY) — directional accuracy on the strict axis")
    print("=" * 92)
    print(f"{'source':10} {'n':>6} {'dir_acc':>8} {'base_rate':>10}")
    for s in SOURCES:
        r = t1[s]
        if r.get("n", 0) == 0:
            continue
        print(f"{s:10} {r['n']:>6} {r['dir_acc']:>8.4f} {r['base_rate']:>10.3f}")
    print()
    print("=" * 92)
    print("TASK 1C — reliability tables (P(cross) decile -> observed rate)")
    print("=" * 92)
    for s in SOURCES:
        r = t1[s]
        if r.get("n", 0) == 0:
            continue
        print(f"{s}  (n={r['n']}, ECE={r['ece']}, MCE={r['mce']})")
        print(f"  {'bin':>10} {'n':>6} {'mean_p':>8} {'obs':>8} {'gap':>8}")
        for row in r["calibration_table"]:
            if row["n"] == 0:
                print(f"  {row['bin']:>10} {0:>6}      -        -        -")
                continue
            print(f"  {row['bin']:>10} {row['n']:>6} {row['mean_p']:>8.4f} "
                  f"{row['emp_rate']:>8.4f} {row['gap']:>+8.4f}")
        print()
    print("=" * 78)
    print("TASK 2 — pairwise correlation of per-station-day forecasts (P(cross))")
    print("=" * 78)
    print(f"{'pair':22} {'n':>6} {'pearson':>9} {'spearman':>9}  flag")
    for k in sorted(pmat):
        r = pmat[k]
        pr = "  " if r["pearson"] is None else f"{r['pearson']:>9.4f}"
        sr = "  " if r["spearman"] is None else f"{r['spearman']:>9.4f}"
        fl = "  <== EFFECTIVELY ONE" if r["flag"] else ""
        print(f"{k:22} {r['n']:>6} {pr} {sr}{fl}")
    print()
    print("TASK 2b — NWP temperature correlation (°F, raw mu)")
    print(f"{'pair':22} {'n':>6} {'pearson':>9}  flag")
    for k in sorted(tmat):
        r = tmat[k]
        if r["pearson"] is None:
            continue
        fl = "  <== EFFECTIVELY ONE" if r["flag"] else ""
        print(f"{k:22} {r['n']:>6} {r['pearson']:>9.4f}{fl}")
    print()
    print("=" * 78)
    print("TASK 3 — blend vs best single")
    print("=" * 78)
    print(json.dumps(t3, indent=2))
    print()
    print(f"TASK 4 — p_market resolvable: {n_pm}/{len(pm)}")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
