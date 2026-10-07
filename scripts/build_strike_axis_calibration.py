#!/usr/bin/env python3
"""
build_strike_axis_calibration.py — strike-crossing-axis reliability curve.

H2 blocker fix (Gilfoyle, H4 review 2026-10-07).

`data/calibration_curves.json` is built on the DAY-OVER-DAY warming axis
(`build_calibration_curves.py::compute_ensemble_fraction` ->
`predicted_warming = ensemble_mean > prev_temp`), which is NOT the axis the B2
sweep trades (strict strike-crossing `P(high >= T)`). Feeding one into the other
silently fabricates the trade set.

This builder produces a curve on the CORRECT axis, from the SAME data the sweep
consumes, and — critically — trained OUT-OF-SAMPLE: only ladder station-days
BEFORE the evaluation window start, so the Aug–Oct 2026 sweep is scored against a
curve fit on prior history.

The predictor is the SAME intraday-constrained probability the sweep selects on
(Gaussian constrained by the observed high-so-far at 13:30Z local), so the curve's
domain matches the quantity it calibrates.

Input pairs, per (station, settlement date, threshold strike):
    p_pred = P(high >= T) with the 13:30Z-local intraday constraint applied
             (1.0 for T <= high_so_far, else Gaussian with remaining-day sigma)
    y      = 1 if the threshold settled 'yes' (strict: high > T) else 0
Bin p_pred into 10 equal bins, take the empirical rate per bin, enforce monotone
non-decreasing (PAVA), and shrink under-populated bins toward the pooled rate.

Output: data/strike_axis_calibration.json
Usage:  .venv-gefs/bin/python scripts/build_strike_axis_calibration.py
"""
import bisect
import json
import math
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from core.station_time import station_timezone_name  # noqa: E402

GEFS = REPO / "data" / "gefs_archive.db"
LADDER = REPO / "data" / "kalshi_strike_ladder.db"
METAR = REPO / "data" / "metar_backfill.db"
OUT = REPO / "data" / "strike_axis_calibration.json"

TRAIN_END = "2026-08-01"      # exclusive; the B2 eval window starts here
ENTRY_UTC = "13:30:00"       # must match the sweep's pinned default
MIN_SAMPLES = 30              # below this a bin shrinks toward the pooled rate
N_BINS = 10
USE_INTRADAY = True           # train on the intraday-constrained predictor


def _norm_cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def p_high_ge(mu, sigma, x):
    if sigma <= 0:
        return 1.0 if mu >= x else 0.0
    return 1.0 - _norm_cdf((x - mu) / sigma)


def ro(p):
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def load_gefs(train_end):
    """{station: {date: (mu_f, sigma_f)}} from step=24 members, dates < train_end."""
    c = ro(GEFS)
    out = {}
    q = """SELECT station, target_date, ensemble_mean, ensemble_min, ensemble_max
           FROM gefs_archive WHERE step=24 AND blob_ok=1 AND target_date < ?"""
    for st, d, mean, mn, mx in c.execute(q, (train_end,)):
        if mean is None or mn is None or mx is None:
            continue
        mu = float(mean) * 9.0 / 5.0 + 32.0
        sigma = max(0.8, (float(mx) - float(mn)) * 9.0 / 5.0 / 4.0)
        out.setdefault(st, {})[d] = (mu, sigma)
    c.close()
    return out


def load_ladder(train_end):
    """[(station, date, strike, result)] HIGH/greater with a realized result, dates < train_end."""
    c = ro(LADDER)
    q = """SELECT station, local_trading_date, strike_value, result
           FROM strike_ladder
           WHERE market_type='HIGH' AND strike_type='greater'
             AND result IN ('yes','no') AND local_trading_date < ?"""
    rows = [(st, d, float(sv), res) for st, d, sv, res in c.execute(q, (train_end,))]
    c.close()
    return rows


def pava(y):
    """Monotone non-decreasing fit of a sequence (pool adjacent violators)."""
    vals = [float(v) for v in y]
    n = [1] * len(vals)
    i = 0
    while i < len(vals) - 1:
        if vals[i] > vals[i + 1]:
            tot = vals[i] * n[i] + vals[i + 1] * n[i + 1]
            cnt = n[i] + n[i + 1]
            vals[i] = tot / cnt
            n[i] = cnt
            vals.pop(i + 1)
            n.pop(i + 1)
            if i > 0:
                i -= 1
        else:
            i += 1
    out = []
    for v, cnt in zip(vals, n):
        out.extend([v] * cnt)
    return out


def local_day_bounds(station, date_str):
    tz = ZoneInfo(station_timezone_name(station))
    d = datetime.fromisoformat(date_str).date()
    start = datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(timezone.utc)
    return start, start + timedelta(days=1)


def load_metar_by_station(station):
    """Sorted [(ts_iso, temp_f)] for one station (whole archive)."""
    c = ro(METAR)
    rows = [(ts, float(tf)) for ts, tf in c.execute(
        "SELECT timestamp_utc, temp_f FROM metar_observations "
        "WHERE station=? AND temp_f IS NOT NULL ORDER BY timestamp_utc", (station,))]
    c.close()
    return rows


def p_pred(mu, sigma, hsf, df, x):
    """The sweep's predictor: intraday-constrained P(high>=x)."""
    if USE_INTRADAY:
        if hsf is not None and x <= hsf:
            return 1.0
        remaining = max(0.05, 1.0 - max(0.0, min(1.0, df)))
        sigma_t = max(0.05, max(0.8, sigma) * math.sqrt(remaining))
        mu_t = max(mu, hsf) if hsf is not None else mu
        return p_high_ge(mu_t, sigma_t, x)
    return p_high_ge(mu, sigma, x)


def main():
    gefs = load_gefs(TRAIN_END)
    ladder = load_ladder(TRAIN_END)
    edges = [i / N_BINS for i in range(N_BINS + 1)]
    bins = [[] for _ in range(N_BINS)]
    n_used = 0
    n_locked = 0
    metar_cache = {}
    for st, d, sv, res in ladder:
        g = gefs.get(st, {}).get(d)
        if not g:
            continue
        mu, sigma = g
        hsf = None
        df = 0.0
        if USE_INTRADAY:
            if st not in metar_cache:
                metar_cache[st] = load_metar_by_station(st)
            rows = metar_cache[st]
            day_start, day_end = local_day_bounds(st, d)
            cutoff = datetime.fromisoformat(f"{d}T{ENTRY_UTC}+00:00")
            lo_iso = day_start.isoformat()
            hi_iso = cutoff.isoformat()
            # binary search the sorted timestamps for the [day_start, cutoff) window
            keys = [r[0] for r in rows]
            i0 = bisect.bisect_left(keys, lo_iso)
            i1 = bisect.bisect_left(keys, hi_iso)
            window = rows[i0:i1]
            if window:
                hsf = max(tf for _, tf in window)
            df = max(0.0, min(1.0, (cutoff - day_start).total_seconds()
                              / (day_end - day_start).total_seconds()))
            # Locked strikes (T <= observed high-so-far) are a mathematical
            # CERTAINTY (P=1), not a model probability — calibrating them would
            # be wrong. Exclude from the fit; the sweep passes them through.
            if hsf is not None and sv <= hsf:
                n_locked += 1
                continue
        p = p_pred(mu, sigma, hsf, df, sv)
        idx = min(N_BINS - 1, int(p * N_BINS))
        bins[idx].append(1.0 if res == "yes" else 0.0)
        n_used += 1

    pooled = (sum(sum(b) for b in bins) / n_used) if n_used else 0.5
    raw_rates = []
    counts = []
    for b in bins:
        counts.append(len(b))
        if len(b) >= MIN_SAMPLES:
            raw_rates.append(sum(b) / len(b))
        else:
            # shrink toward pooled by sample weight
            raw_rates.append((sum(b) + pooled * MIN_SAMPLES) / (len(b) + MIN_SAMPLES))
    mono = pava(raw_rates)

    artifact = {
        "axis": "strike_crossing",
        "predictor": ("intraday-constrained P(high>=T) @13:30Z local" if USE_INTRADAY
                      else "bare Gaussian P(high>=T)"),
        "method": ("GEFS step-24 Gaussian p (intraday-constrained) vs strict "
                   "strike-crossing outcome; empirical rate per p-bin, PAVA "
                   "monotone, low-n shrunk to pooled"),
        "train_window": ["2021-01-02", TRAIN_END],
        "eval_window_start": TRAIN_END,
        "n_training_pairs": n_used,
        "n_locked_excluded": n_locked,
        "n_bins": N_BINS,
        "min_samples_per_bin": MIN_SAMPLES,
        "bin_edges": edges,
        "pooled_rate": round(pooled, 6),
        "empirical_rate": [round(r, 6) for r in raw_rates],
        "calibrated_rate": [round(r, 6) for r in mono],
        "sample_counts": counts,
    }
    OUT.write_text(json.dumps(artifact, indent=2))
    print(json.dumps({k: artifact[k] for k in
                      ["train_window", "n_training_pairs", "pooled_rate",
                       "empirical_rate", "calibrated_rate", "sample_counts"]}, indent=2))
    print("wrote", OUT)


if __name__ == "__main__":
    main()
