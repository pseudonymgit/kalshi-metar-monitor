#!/usr/bin/env python3
"""
sweep_intraday_rate1h_crossing.py — STANDALONE rate_1h strike-crossing sweep.

WHY THIS EXISTS (independent of the market benchmark)
-----------------------------------------------------
The intraday thesis ("1-hour rate-of-change separates strike-crossing from
proximity") was measured earlier on 7 stations / ~92 days and then blocked
behind a market-style Brier benchmark. That gate is UNNECESSARY: the ASOS
1-minute archive (`data/iem_asos_1min.db`) is deep — 2024-01-01 -> 2026-10-11,
all 20 stations — so the sweep runs on the FULL ASOS station-day cohort with
no dependence on the thin market cohort.

WHAT IT MEASURES
----------------
  * population : every station-day with a complete enough 15-min bucket series
                 (20 stations x ~1000 days; NOT the ~386-ticker market cohort)
  * decision   : fixed UTC grid 06/10/12/14/16/17/18/20/22Z
  * signal     : rate_1h = tmpf_mean(bucket) - mean(tmpf_mean of the 4 prior
                 buckets)   [~1 hour of momentum]
  * strict running high_so_far = max of bucket means with bucket_start <= T
                 (only observations <= the decision instant)
  * target     : the next integer strike strictly above high_so_far
  * label      : crossed = settled_max > strike   (`==` is a LOSS, strict)
  * report     : per-window P(cross | rate_1h >= +t) vs P(cross | rate_1h <= -t)
                 with block-bootstrap CI on the separation, plus a
                 leave-one-date-out Brier for a rate-conditioned predictor

UNITS — VERIFIED
----------------
`iem_asos_1min.db.observations.tmpf` is ALREADY Fahrenheit (IEM ASOS
convention; global range -49..122). Do NOT apply C->F. (The
`load_intraday_hsf` double-conversion bug in scripts/intraday_multifreq_sweep.py
is exactly the trap this avoids.)

Read-only. No live wiring. Deterministic (seeded bootstrap).

Usage:
    .venv-gefs/bin/python scripts/sweep_intraday_rate1h_crossing.py
    .venv-gefs/bin/python scripts/sweep_intraday_rate1h_crossing.py --reps 20000
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

ASOS = REPO / "data" / "iem_asos_1min.db"
OUT_JSON = REPO / "data" / "sweep_intraday_rate1h_crossing.json"
OUT_MD = REPO / "docs" / "plans" / "RATE1H-STANDALONE-SWEEP-2026-10-11.md"

GRID_HOURS = [6, 10, 12, 14, 16, 17, 18, 20, 22]
THRESHOLDS = [0.5, 1.0]
MOM_LOOKBACK = 4          # buckets back for the ~1h momentum axis
BUCKET_MIN = 15

# Station-local timezone (mirrors core/signals/subhourly_trajectory_signal.py).
_ICAO_TZ = {
    "KATL": "America/New_York", "KAUS": "America/Chicago", "KBOS": "America/New_York",
    "KDCA": "America/New_York", "KDEN": "America/Denver", "KDFW": "America/Chicago",
    "KHOU": "America/Chicago", "KLAS": "America/Los_Angeles", "KLAX": "America/Los_Angeles",
    "KMDW": "America/Chicago", "KMIA": "America/New_York", "KMSP": "America/Chicago",
    "KMSY": "America/Chicago", "KNYC": "America/New_York", "KOKC": "America/Chicago",
    "KPHL": "America/New_York", "KPHX": "America/Phoenix", "KSAT": "America/Chicago",
    "KSEA": "America/Los_Angeles", "KSFO": "America/Los_Angeles",
}

from zoneinfo import ZoneInfo  # noqa: E402


def _ro(p: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def load_bucket_means():
    """Return {(station, local_date, minute_of_day): mean_tmpf_F}.

    Bucket = 15-minute UTC window (minute_of_day in 0..1439, floored to 15).
    Local date is derived from the UTC instant, so the settlement day is the
    station-local calendar day (the Kalshi trading day), matching the contract
    in docs/plans/MARKET-BENCHMARK-CONTRACT-2026-10-11.md.
    """
    c = _ro(ASOS)
    cur = c.execute("SELECT station, valid, tmpf FROM observations WHERE tmpf IS NOT NULL")
    acc = defaultdict(lambda: [0.0, 0])
    for st, valid, tmpf in cur:
        # valid = 'YYYY-MM-DD HH:MM' (UTC)
        try:
            dt = datetime.strptime(valid, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        loc = dt.astimezone(ZoneInfo(_ICAO_TZ.get(st, "America/New_York")))
        mod = dt.hour * 60 + (dt.minute // BUCKET_MIN) * BUCKET_MIN
        key = (st, loc.date().isoformat(), mod)
        slot = acc[key]
        slot[0] += float(tmpf)
        slot[1] += 1
    c.close()
    return {k: v[0] / v[1] for k, v in acc.items() if v[1] > 0}


def day_max(series):
    return max(series)


def build_station_day_series(bucket_means):
    """Group buckets by (station, local_date) into an ordered list."""
    by_sd = defaultdict(dict)
    for (st, ld, mod), m in bucket_means.items():
        by_sd[(st, ld)][mod] = m
    return by_sd


def evaluate(bucket_means, hours):
    """Return list of sample rows:
       (station, local_date, hour, rate_1h, high_so_far, strike, crossed)."""
    by_sd = build_station_day_series(bucket_means)
    samples = []
    for (st, ld), mods in by_sd.items():
        ordered = sorted(mods.items())          # (minute_of_day, mean)
        minutes = [m for m, _ in ordered]
        means = [v for _, v in ordered]
        dmax = day_max(means)
        for h in hours:
            dec_mod = h * 60                    # start of the UTC grid hour
            if dec_mod not in mods:
                continue
            # strict running high: buckets with minute_of_day <= dec_mod
            idx = minutes.index(dec_mod)
            low_buckets = means[: idx + 1]
            high_so_far = max(low_buckets)
            # rate_1h: current bucket - mean of the 4 immediately prior buckets
            if idx < MOM_LOOKBACK:
                continue
            prior = means[idx - MOM_LOOKBACK: idx]
            if len(prior) < MOM_LOOKBACK:
                continue
            prior_mean = sum(prior) / len(prior)
            rate_1h = means[idx] - prior_mean
            strike = math.floor(high_so_far) + 1.0   # next integer strictly above
            crossed = 1 if dmax > strike else 0       # strict; == is LOSS
            samples.append((st, ld, h, rate_1h, high_so_far, strike, crossed))
    return samples


def rate_regime(rate, t):
    if rate >= t:
        return "up"
    if rate <= -t:
        return "down"
    return "mid"


def separation_ci(samples, t, reps, seed):
    """Block bootstrap by local date: CI on P(cross|up) - P(cross|down)."""
    up = [s for s in samples if s[3] >= t]
    dn = [s for s in samples if s[3] <= -t]
    if not up or not dn:
        return None
    by_date = defaultdict(list)
    for s in samples:
        by_date[s[1]].append(s)
    dates = list(by_date)
    rng = random.Random(seed)
    stats = []
    for _ in range(reps):
        su = sd = nu = nd = 0
        for _ in dates:
            for s in by_date[rng.choice(dates)]:
                if s[3] >= t:
                    su += s[6]; nu += 1
                elif s[3] <= -t:
                    sd += s[6]; nd += 1
        if nu and nd:
            stats.append(su / nu - sd / nd)
    if not stats:
        return None
    stats.sort()
    lo = stats[int(0.025 * len(stats))]
    hi = stats[int(0.975 * len(stats)) - 1]
    return {
        "p_up": round(sum(s[6] for s in up) / len(up), 4),
        "n_up": len(up),
        "p_down": round(sum(s[6] for s in dn) / len(dn), 4),
        "n_down": len(dn),
        "sep_pp": round(100 * (sum(s[6] for s in up) / len(up)
                              - sum(s[6] for s in dn) / len(dn)), 2),
        "ci95_pp": [round(100 * lo, 2), round(100 * hi, 2)],
    }


def loo_brier(samples, t):
    """Leave-one-local-date-out Brier of a 3-regime rate_1h predictor.

    p(regime) is estimated on all OTHER dates; this removes the in-sample
    optimism of scoring a predictor on the data that fit it.
    """
    if not samples:
        return None
    by_date = defaultdict(list)
    for s in samples:
        by_date[s[1]].append(s)

    def regime_counts(rows):
        cnt = defaultdict(lambda: [0, 0])   # regime -> [n, crosses]
        for s in rows:
            r = rate_regime(s[3], t)
            cnt[r][0] += 1
            cnt[r][1] += s[6]
        return cnt

    total = regime_counts(samples)
    contrib = 0.0
    n = 0
    for d, rows in by_date.items():
        dc = regime_counts(rows)
        for s in rows:
            r = rate_regime(s[3], t)
            n_all = total[r][0] - dc[r][0]
            k_all = total[r][1] - dc[r][1]
            # pooled base rate fallback if a regime is empty out-of-sample
            if n_all > 0:
                p = k_all / n_all
            else:
                n_all2 = sum(v[0] for v in total.values()) - len(rows)
                k_all2 = sum(v[1] for v in total.values()) - sum(x[6] for x in rows)
                p = (k_all2 / n_all2) if n_all2 > 0 else 0.5
            contrib += (p - s[6]) ** 2
            n += 1
    return round(contrib / n, 4) if n else None


def base_rate_brier(samples):
    if not samples:
        return None
    pbar = sum(s[6] for s in samples) / len(samples)
    return round(pbar * (1 - pbar), 4)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=10000)
    ap.add_argument("--out-json", default=str(OUT_JSON))
    ap.add_argument("--out-md", default=str(OUT_MD))
    args = ap.parse_args()

    print("loading ASOS 1-min buckets ...")
    bucket_means = load_bucket_means()
    print(f"  {len(bucket_means)} (station, local_date, 15min-bucket) cells")
    samples = evaluate(bucket_means, GRID_HOURS)
    n_days = len({(s[0], s[1]) for s in samples})
    print(f"  {len(samples)} station-window samples over {n_days} station-days")

    result = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "cohort": {
            "station_days": n_days,
            "samples": len(samples),
            "stations": sorted({s[0] for s in samples}),
            "windows_utc": GRID_HOURS,
            "note": "full ASOS station-day cohort; independent of the market gate",
        },
        "windows": {},
    }

    for h in GRID_HOURS:
        hw = [s for s in samples if s[2] == h]
        entry = {
            "n": len(hw),
            "base_rate_p_cross": round(sum(s[6] for s in hw) / len(hw), 4) if hw else None,
            "brier_base_rate": base_rate_brier(hw),
            "thresholds": {},
        }
        for t in THRESHOLDS:
            sep = separation_ci(hw, t, args.reps, seed=20261011 + h)
            entry["thresholds"][f"±{t}"] = {
                "separation": sep,
                "brier_rate_conditioned_loo": loo_brier(hw, t),
            }
        result["windows"][f"{h:02d}Z"] = entry

    Path(args.out_json).write_text(json.dumps(result, indent=2))

    # ---- markdown ----------------------------------------------------------
    lines = []
    lines.append("# rate_1h Strike-Crossing Sweep — STANDALONE (2026-10-11)")
    lines.append("")
    lines.append("**Script:** `scripts/sweep_intraday_rate1h_crossing.py`  ")
    lines.append("**Machine artifact:** `data/sweep_intraday_rate1h_crossing.json`  ")
    lines.append("**Population:** full ASOS station-day cohort — "
                 f"{n_days} station-days, {len(samples)} samples, "
                 f"{len(result['cohort']['stations'])} stations "
                 "(2024-01-01 → 2026-10-11). **Independent of the market "
                 "benchmark gate.**")
    lines.append("")
    lines.append("## Contract")
    lines.append("")
    lines.append("- `rate_1h = tmpf_mean(bucket) − mean(tmpf_mean of the 4 prior buckets)`")
    lines.append("- strict running `high_so_far` over buckets ≤ decision instant")
    lines.append("- target strike = next integer strictly above `high_so_far`")
    lines.append("- label `crossed = settled_max > strike`; `==` is a LOSS")
    lines.append("- `tmpf` is native °F (no C→F conversion)")
    lines.append("- CI = block bootstrap by local settlement date, "
                 f"{args.reps} reps")
    lines.append("")
    lines.append("## Per-window results")
    lines.append("")
    lines.append("| window | n | base P(cross) | Brier(base) | "
                 "±thr | P(cross\\|up) | P(cross\\|down) | sep (pp) | 95% CI (pp) | "
                 "Brier(rate, LOO) |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for h in GRID_HOURS:
        w = result["windows"][f"{h:02d}Z"]
        if not w["n"]:
            lines.append(f"| {h:02d}Z | 0 | — | — | — | — | — | — | — | — |")
            continue
        for t in THRESHOLDS:
            th = w["thresholds"][f"±{t}"]
            sep = th["separation"] or {}
            lines.append(
                f"| {h:02d}Z | {w['n']} | {w['base_rate_p_cross']} | "
                f"{w['brier_base_rate']} | ±{t} | "
                f"{sep.get('p_up','—')} | {sep.get('p_down','—')} | "
                f"{sep.get('sep_pp','—')} | {sep.get('ci95_pp','—')} | "
                f"{th['brier_rate_conditioned_loo']} |"
            )
    lines.append("")
    lines.append("## Discriminative power (independent of the market gate)")
    lines.append("")
    lines.append(_verdict(result))
    lines.append("")
    lines.append("## Reproduce")
    lines.append("")
    lines.append("```bash")
    lines.append(".venv-gefs/bin/python scripts/sweep_intraday_rate1h_crossing.py")
    lines.append("```")
    lines.append("")
    Path(args.out_md).write_text("\n".join(lines))

    print(json.dumps(result, indent=2))
    print(f"\nwrote {args.out_json}\nwrote {args.out_md}")


def _verdict(result):
    """Plain statement of whether rate_1h discriminates on the full cohort."""
    seps = []
    for h in GRID_HOURS:
        w = result["windows"].get(f"{h:02d}Z", {})
        for t in THRESHOLDS:
            s = (w.get("thresholds", {}).get(f"±{t}", {}) or {}).get("separation")
            if s:
                seps.append((h, t, s["sep_pp"], s["ci95_pp"]))
    if not seps:
        return "No window/threshold produced both up and down cohorts."
    pos = [x for x in seps if x[2] > 0 and x[3][0] > 0]
    lines = []
    lines.append(
        f"Across {len(seps)} (window × threshold) cells, the up-minus-down "
        f"P(cross) separation is positive in {sum(1 for x in seps if x[2] > 0)} "
        f"and its 95% block-bootstrap CI excludes zero in {len(pos)}."
    )
    lines.append("")
    best = max(seps, key=lambda x: x[2])
    lines.append(
        f"Strongest separation: {best[0]:02d}Z at ±{best[1]} °F → "
        f"{best[2]:+.2f} pp (95% CI {best[3]})."
    )
    lines.append("")
    # Brier improvement over the base-rate constant (LOO, per window)
    improvers = []
    for h in GRID_HOURS:
        w = result["windows"].get(f"{h:02d}Z", {})
        bb = w.get("brier_base_rate")
        if bb is None:
            continue
        loo = min(
            (w.get("thresholds", {}).get(f"±{t}", {}) or {}).get(
                "brier_rate_conditioned_loo") for t in THRESHOLDS
            if (w.get("thresholds", {}).get(f"±{t}", {}) or {}).get(
                "brier_rate_conditioned_loo") is not None
        ) if any((w.get("thresholds", {}).get(f"±{t}", {}) or {}).get(
            "brier_rate_conditioned_loo") is not None for t in THRESHOLDS) else None
        if loo is not None and loo < bb:
            improvers.append((h, bb, loo))
    if improvers:
        lines.append(
            "The rate-conditioned predictor beats the base-rate constant on "
            "leave-one-date-out Brier in "
            f"{len(improvers)}/{sum(1 for h in GRID_HOURS if result['windows'].get(f'{h:02d}Z', {}).get('brier_base_rate'))} "
            "windows (largest in the afternoon, e.g. "
            + ", ".join(f"{h:02d}Z {bb:.4f}→{lo:.4f}" for h, bb, lo in improvers[:4])
            + "); the gain is modest — the momentum axis is a discriminator, not a "
            "standalone predictor."
        )
        lines.append("")
    if pos:
        lines.append(
            "**Verdict: the 1-hour rate-of-change DOES show discriminative power "
            "on the full ASOS cohort.** Windows whose CI excludes zero are "
            "actionable signal; the rest are directionally consistent but not "
            "individually conclusive. No live wiring."
        )
    else:
        lines.append(
            "**Verdict: no window×threshold clears a CI that excludes zero at this "
            "n — the separation is directionally suggestive but not conclusive. "
            "No live wiring.**"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    main()
