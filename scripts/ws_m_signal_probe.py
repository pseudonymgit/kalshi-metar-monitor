#!/usr/bin/env python3
"""
WS-M — Honest improvement probe for G2/G4/G5.

Tests, on a TRAIN/HOLDOUT split, whether feature augmentation or logic changes can
push G2/G4/G5 above 0.55 directional accuracy. No leakage: all predictors use only
days[..idx-1]; truth = day-over-day direction of Kalshi settlement temps.

Feature families probed (all honest, prior-day only):
  - persistence      : predict tomorrow continues today's day-over-day move
  - pressure trend   : sign of pressure change over k days
  - dewpoint-dep     : dry/moist regime + trend continuation
  - multi-feature logistic: combined features fitted on TRAIN, tested on HOLDOUT

Usage: .venv-gefs/bin/python scripts/ws_m_signal_probe.py
"""
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
METAR = str(ROOT / "data" / "metar_backfill.db")
KALSHI = str(ROOT / "data" / "kalshi_settlements.db")
OUT = str(ROOT / "data" / "ws_m_signal_probe.json")

TRAIN_END = "2026-06-30"
HOLDOUT_START = "2026-07-01"


def load():
    m = sqlite3.connect(METAR)
    k = sqlite3.connect(KALSHI)
    stations = [s for s, in m.execute(
        "SELECT DISTINCT station FROM metar_observations").fetchall() if s]
    kd = defaultdict(list)
    for st, dt, t in k.execute(
        "SELECT station, target_date, kalshi_temp FROM kalshi_settlements "
        "WHERE kalshi_temp IS NOT NULL ORDER BY station, target_date"):
        kd[st].append((dt[:10], t))
    truth = {}
    for st, rows in kd.items():
        for i in range(1, len(rows)):
            a, b = rows[i][1], rows[i - 1][1]
            truth[(st, rows[i][0])] = "up" if a > b else ("down" if a < b else None)

    series = {}
    for st in stations:
        rows = m.execute(
            "SELECT date_utc, AVG(temp_f), AVG(dewpoint_f), AVG(pressure_mb) "
            "FROM metar_observations WHERE station=? AND date_utc>='2026-04-01' "
            "AND temp_f IS NOT NULL GROUP BY date_utc ORDER BY date_utc", (st,)).fetchall()
        series[st] = [(d[:10], t, dw, p) for d, t, dw, p in rows]
    return truth, series


def daterange(d):
    return d <= TRAIN_END, d >= HOLDOUT_START


def eval_rule(predict_fn, truth, series):
    """predict_fn(st, days, i) -> 'up'/'down'/None; days = list of tuples."""
    res = {"train": [0, 0], "holdout": [0, 0]}
    for st, rows in series.items():
        dates = [r[0] for r in rows]
        for i in range(3, len(rows)):
            d = dates[i]
            t = truth.get((st, d))
            if t is None:
                continue
            pred = predict_fn(st, rows, i)
            if pred is None:
                continue
            is_train, is_hold = daterange(d)
            for split, flag in (("train", is_train), ("holdout", is_hold)):
                if flag:
                    res[split][0] += 1
                    res[split][1] += pred == t
    out = {}
    for split in ("train", "holdout"):
        n, c = res[split]
        out[split] = {"n": n, "acc": round(c / n, 4) if n else None}
    return out


def main():
    truth, series = load()
    results = {}

    # 1. persistence: tomorrow follows last completed day-over-day move
    def r_persist(st, rows, i):
        t0, t1 = rows[i - 1][1], rows[i - 2][1]
        if t0 is None or t1 is None or t0 == t1:
            return None
        return "up" if t0 > t1 else "down"
    results["persistence"] = eval_rule(r_persist, truth, series)

    # 2. pressure trend (k=2): sign of pressure change predicts temp direction
    def r_press(st, rows, i):
        p0, p1 = rows[i - 1][3], rows[i - 2][3]
        if p0 is None or p1 is None or p0 == p1:
            return None
        # rising pressure -> cooling (down), falling -> warming (up)
        return "down" if p0 > p1 else "up"
    results["pressure_trend"] = eval_rule(r_press, truth, series)

    def r_press_cont(st, rows, i):
        p0, p1 = rows[i - 1][3], rows[i - 2][3]
        if p0 is None or p1 is None or p0 == p1:
            return None
        return "up" if p0 > p1 else "down"
    results["pressure_trend_cont"] = eval_rule(r_press_cont, truth, series)

    # 3. dewpoint-dep + trend continuation (G5-style but prior-day)
    def r_dew(st, rows, i):
        t0, dw0 = rows[i - 1][1], rows[i - 1][2]
        t1 = rows[i - 2][1]
        if None in (t0, dw0, t1):
            return None
        dep = t0 - dw0
        if dep <= 10.0:
            return None
        if t0 == t1:
            return None
        return "up" if t0 > t1 else "down"
    results["dewpoint_dep"] = eval_rule(r_dew, truth, series)

    # 4. multi-feature logistic on prior-day features (fit train, test holdout)
    feats, labels, splits = [], [], []
    for st, rows in series.items():
        dates = [r[0] for r in rows]
        for i in range(3, len(rows)):
            d = dates[i]
            t = truth.get((st, d))
            if t is None:
                continue
            t0, dw0, p0 = rows[i - 1][1], rows[i - 1][2], rows[i - 1][3]
            t1, p1 = rows[i - 2][1], rows[i - 2][3]
            if None in (t0, dw0, p0, t1, p1):
                continue
            x = [t0 - t1,                      # temp momentum
                 (t0 - t1) - (rows[i-2][1] - rows[i-3][1]) if rows[i-3][1] is not None else 0,
                 p0 - p1,                      # pressure momentum
                 t0 - dw0,                     # dewpoint depression
                 (t0 - dw0) * np.sign(t0 - t1)] # interaction
            feats.append(x)
            labels.append(1.0 if t == "up" else 0.0)
            splits.append(daterange(d)[0])  # True=train
    X = np.array(feats)
    y = np.array(labels)
    tr = np.array(splits)
    mu, sd = X[tr].mean(0), X[tr].std(0) + 1e-9
    Xs = (X - mu) / sd
    w = np.zeros(X.shape[1]); b = 0.0
    for _ in range(800):
        z = np.clip(Xs[tr] @ w + b, -30, 30)
        p = 1 / (1 + np.exp(-z))
        g = Xs[tr].T @ (p - y[tr]) / tr.sum()
        gw = 0.05 * w
        w -= 0.5 * (g + gw); b -= 0.5 * np.mean(p - y[tr])
    def logit_out(sub):
        z = np.clip(Xs[sub] @ w + b, -30, 30)
        return 1 / (1 + np.exp(-z))
    out = {}
    for split, sub in (("train", tr), ("holdout", ~tr)):
        pp = logit_out(sub)
        pred = (pp >= 0.5).astype(int)
        acc = float((pred == y[sub]).mean()) if sub.sum() else None
        out[split] = {"n": int(sub.sum()), "acc": round(acc, 4) if acc is not None else None,
                      "base_rate_up": round(float(y[sub].mean()), 4) if sub.sum() else None}
    results["logistic_multifeature"] = out

    Path(OUT).write_text(json.dumps(results, indent=2))
    print("WS-M honest improvement probe (train<= %s, holdout>= %s):" % (TRAIN_END, HOLDOUT_START))
    for k, v in results.items():
        print(f"  {k:26} train={v['train']['acc']} (n={v['train']['n']}) | holdout={v['holdout']['acc']} (n={v['holdout']['n']})")
    print(f"Saved: {OUT}")


if __name__ == "__main__":
    main()
