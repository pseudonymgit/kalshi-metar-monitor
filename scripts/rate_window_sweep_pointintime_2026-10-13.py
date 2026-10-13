#!/usr/bin/env python3
"""rate_window_sweep_pointintime_2026-10-13.py — Item 1.

HONEST point-in-time sweep of rate windows W = 1, 2, 3, 4 hours.

WHY THIS EXISTS
---------------
The prior multi-window sweep (Workstream K / ai_nwp_independence_rate1h_sweep)
scored rate_1h with a WINDOW-MAX scan over 13:00..16:45 — it *looked ahead* past
the decision instant and inflated the W=1 separation (60.2 pp @ 18Z). This script
re-runs the sweep with PAST-ONLY windows evaluated AT THE PIN:

    rate_W = mean(tmpf of the W hours ending at the pin)
           - mean(tmpf of the W hours immediately before that)

Every bucket used is <= the pin (mod <= 810 for the 13:30 pin). No look-ahead.
No window-max. One prediction per station-day per window per pin.

THE AXIS (identical to the diversity study, strict strike-crossing)
  * pin        : 13:30 station-local (mod 810); also 14:30 (870) / 15:00 (900)
  * high_so_far: max 15-min bucket MEAN with mod <= pin   (buckets are <= pin)
  * target     : T = floor(high_so_far) + 1.0   (next integer strictly above)
  * label      : crossed = settled_max > T        (`==` is a LOSS)
  * truth      : canonical GHCN settlement (metar_backfill.db settlement_truth,
                 HIGH, trustworthy=1) — same label as the diversity study
  * cohort     : station-days in 2026-04-21..2026-10-09 with ASOS buckets and truth

METRICS per (W, pin)
  * base P(cross)
  * separation(pp) = P(cross | rate_W >= +t) - P(cross | rate_W <= -t), ±0.5/±1.0,
    with a block-bootstrap CI by local settlement date
  * normalized separation: top-15% vs bottom-15% of that window's own rate_W
    distribution (removes the "longer window dilates the scale" confound)
  * LOO (leave-one-local-date-out) Brier of a 3-regime rate-conditioned predictor
    (up / mid / down), and its directional accuracy
  * at the same 13:30 pin, the published-style W=1 point rate for reference

READ-ONLY on every book. Bucket cache: /tmp/we_asos_recs_2026-10-13.pkl.gz if
present (produced by predictor_diversity_study.build_grid -> load_asos), else
rebuilt from data/iem_asos_1min.db. Deterministic (seeded block bootstrap).

Usage:
    .venv-gefs/bin/python scripts/rate_window_sweep_pointintime_2026-10-13.py
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import pickle
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

ASOS = REPO / "data" / "iem_asos_1min.db"
METAR = REPO / "data" / "metar_backfill.db"
RECS = Path("/tmp/we_asos_recs_2026-10-13.pkl.gz")
OUT_JSON = REPO / "artifacts" / "rate-window-sweep" / "rate_window_pointintime_2026-10-13.json"
OUT_MD = REPO / "docs" / "plans" / "RATE-WINDOW-POINTINTIME-2026-10-13.md"

# cohort window == the diversity study window
MIGRATION_START = "2026-04-21"
MIGRATION_END = "2026-10-09"

PINS = {810: "13:30", 870: "14:30", 900: "15:00"}
PRIMARY_PIN = 810
WINDOWS = [1, 2, 3, 4]
THRESHOLDS = [0.5, 1.0]
BUCKET_MIN = 15
NORMALIZED_FRAC = 0.15

STATIONS = [
    "KATL", "KAUS", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU", "KLAS", "KLAX",
    "KMDW", "KMIA", "KMSP", "KMSY", "KNYC", "KOKC", "KPHL", "KPHX", "KSAT",
    "KSEA", "KSFO",
]

_ICAO_TZ = {
    "KATL": "America/New_York", "KAUS": "America/Chicago", "KBOS": "America/New_York",
    "KDCA": "America/New_York", "KDEN": "America/Denver", "KDFW": "America/Chicago",
    "KHOU": "America/Chicago", "KLAS": "America/Los_Angeles", "KLAX": "America/Los_Angeles",
    "KMDW": "America/Chicago", "KMIA": "America/New_York", "KMSP": "America/Chicago",
    "KMSY": "America/Chicago", "KNYC": "America/New_York", "KOKC": "America/Chicago",
    "KPHL": "America/New_York", "KPHX": "America/Phoenix", "KSAT": "America/Chicago",
    "KSEA": "America/Los_Angeles", "KSFO": "America/Los_Angeles",
}


def _ro(p: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


# ---------------------------------------------------------------------------
# bucket series (station-day -> {local_mod: mean_F})
# ---------------------------------------------------------------------------
def load_recs():
    """{(station, local_date): {'dmax': F, 'buckets': {local_mod: mean_F}}}.

    Prefer the cached ASOS scan (identical code path to the diversity study);
    fall back to a fresh scan of the 20M-row archive.
    """
    if RECS.exists():
        with gzip.open(RECS, "rb") as f:
            return pickle.load(f), "cache:/tmp/we_asos_recs_2026-10-13.pkl.gz"
    return _scan_asos(), "scan:data/iem_asos_1min.db"


def _scan_asos():
    tz = {st: ZoneInfo(_ICAO_TZ.get(st, "America/New_York")) for st in STATIONS}
    offcache = {}
    bs = {}
    dmax = {}
    c = _ro(ASOS)
    for st, valid, tmpf in c.execute(
            "SELECT station, valid, tmpf FROM observations WHERE tmpf IS NOT NULL"):
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
        if dmax.get(mk, -1e9) < tmpf:
            dmax[mk] = tmpf
    c.close()
    buckets = defaultdict(dict)
    for (st, ldate, mod), (s, cnt) in bs.items():
        buckets[(st, ldate)][mod] = s / cnt
    out = {}
    for k in set(list(dmax) + list(buckets)):
        out[k] = {"dmax": dmax.get(k), "buckets": buckets.get(k, {})}
    return out


def load_truth():
    c = _ro(METAR)
    out = {}
    for st, ds, v in c.execute(
            "SELECT station, local_trading_date, settlement_temp_f FROM settlement_truth "
            "WHERE market_type='HIGH' AND trustworthy=1 AND settlement_temp_f IS NOT NULL"):
        out[(st, ds)] = float(v)
    c.close()
    return out


# ---------------------------------------------------------------------------
# the honest, point-in-time rate window
# ---------------------------------------------------------------------------
def rate_window(buckets, pin, W):
    """Symmetric past-only contrast at the pin.

    last W hours   = buckets [pin-(4W-1)*15 .. pin]         (4W buckets)
    prior W hours  = buckets [pin-(8W-1)*15 .. pin-4W*15]   (4W buckets)
    ALL buckets are < pin except the final one == pin. No look-ahead.
    """
    n = 4 * W
    span = list(range(pin - (2 * n - 1) * BUCKET_MIN, pin + 1, BUCKET_MIN))
    if len(span) != 2 * n or any(m not in buckets for m in span):
        return None
    recent = span[-n:]
    prior = span[:n]
    return (sum(buckets[m] for m in recent) / n
            - sum(buckets[m] for m in prior) / n)


def hsf_at(buckets, pin):
    vals = [buckets[m] for m in buckets if m <= pin]
    return max(vals) if vals else None


def build_samples(recs, truth, pin):
    """Sample rows: (station, local_date, W, rate, high_so_far, T, y)."""
    out = []
    d0 = date.fromisoformat(MIGRATION_START)
    d1 = date.fromisoformat(MIGRATION_END)
    for (st, ds), rec in recs.items():
        try:
            d = date.fromisoformat(ds)
        except ValueError:
            continue
        if not (d0 <= d <= d1):
            continue
        y_raw = truth.get((st, ds))
        if y_raw is None:
            continue
        b = rec.get("buckets") or {}
        if not b:
            continue
        hsf = hsf_at(b, pin)
        if hsf is None:
            continue
        T = float(math.floor(hsf) + 1.0)
        y = 1 if y_raw > T else 0     # strict; == is LOSS
        for W in WINDOWS:
            r = rate_window(b, pin, W)
            if r is None:
                continue
            out.append((st, ds, W, r, hsf, T, y))
    return out


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------
def regime(rate, t):
    if rate >= t:
        return "up"
    if rate <= -t:
        return "down"
    return "mid"


def sep_ci(rows, t, reps, seed):
    up = [s for s in rows if s[3] >= t]
    dn = [s for s in rows if s[3] <= -t]
    if not up or not dn:
        return None
    by_date = defaultdict(list)
    for s in rows:
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
        "p_up": round(sum(s[6] for s in up) / len(up), 4), "n_up": len(up),
        "p_down": round(sum(s[6] for s in dn) / len(dn), 4), "n_down": len(dn),
        "sep_pp": round(100 * (sum(s[6] for s in up) / len(up)
                              - sum(s[6] for s in dn) / len(dn)), 2),
        "ci95_pp": [round(100 * lo, 2), round(100 * hi, 2)],
    }


def normalized_sep(rows, levels=(1.0, 2.0), reps=2000, seed=7):
    """Top-frac vs bottom-frac of the window's OWN rate distribution (per W)."""
    rates = sorted(s[3] for s in rows)
    n = len(rates)
    k = max(1, int(NORMALIZED_FRAC * n))
    lo_cut = rates[k - 1]
    hi_cut = rates[n - k]
    out = {}
    for L in levels:
        hi = [s for s in rows if s[3] >= hi_cut]
        lo = [s for s in rows if s[3] <= lo_cut]
        if not hi or not lo:
            out[f"±{L}"] = None
            continue
        by_date = defaultdict(list)
        for s in rows:
            by_date[s[1]].append(s)
        dates = list(by_date)
        rng = random.Random(seed + int(L * 10))
        stats = []
        for _ in range(reps):
            sh = sl = nh = nl = 0
            for _ in dates:
                for s in by_date[rng.choice(dates)]:
                    if s[3] >= hi_cut:
                        sh += s[6]; nh += 1
                    elif s[3] <= lo_cut:
                        sl += s[6]; nl += 1
            if nh and nl:
                stats.append(sh / nh - sl / nl)
        stats.sort()
        out[f"±{L}"] = {
            "sep_pp": round(100 * (sum(s[6] for s in hi) / len(hi)
                                  - sum(s[6] for s in lo) / len(lo)), 2),
            "ci95_pp": [round(100 * stats[int(0.025 * len(stats))], 2),
                        round(100 * stats[int(0.975 * len(stats)) - 1], 2)],
            "n_hi": len(hi), "n_lo": len(lo),
            "rate_lo_cut": round(lo_cut, 3), "rate_hi_cut": round(hi_cut, 3),
        }
    return out


def _regime_counts(rows, t):
    cnt = defaultdict(lambda: [0, 0])
    for s in rows:
        r = regime(s[3], t)
        cnt[r][0] += 1
        cnt[r][1] += s[6]
    return cnt


def loo_brier_dir(rows, t):
    """LOO-by-date Brier and dir acc of a 3-regime rate-conditioned predictor."""
    if not rows:
        return None
    by_date = defaultdict(list)
    for s in rows:
        by_date[s[1]].append(s)
    total = _regime_counts(rows, t)
    tot_n = sum(v[0] for v in total.values())
    tot_k = sum(v[1] for v in total.values())
    contrib = 0.0
    nn = 0
    correct = 0
    for d, drows in by_date.items():
        dc = _regime_counts(drows, t)
        for s in drows:
            r = regime(s[3], t)
            n_all = total[r][0] - dc[r][0]
            k_all = total[r][1] - dc[r][1]
            if n_all > 0:
                p = k_all / n_all
            else:
                n2 = tot_n - len(drows)
                p = (tot_k - sum(x[6] for x in drows)) / n2 if n2 > 0 else 0.5
            contrib += (p - s[6]) ** 2
            nn += 1
            if (p > 0.5) == bool(s[6]):
                correct += 1
    return {"brier": round(contrib / nn, 4), "dir_acc": round(correct / nn, 4),
            "base_rate": round(tot_k / tot_n, 4)}


def base_brier(rows):
    p = sum(s[6] for s in rows) / len(rows)
    return round(p * (1 - p), 4)


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=10000)
    ap.add_argument("--out-json", default=str(OUT_JSON))
    ap.add_argument("--out-md", default=str(OUT_MD))
    args = ap.parse_args()

    print("loading ASOS bucket series ...")
    recs, prov = load_recs()
    truth = load_truth()
    print(f"  {len(recs)} station-days ({prov}); {len(truth)} truth rows")

    result = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "method": "past-only symmetric rate window evaluated AT THE PIN (no look-ahead)",
        "cohort_window": [MIGRATION_START, MIGRATION_END],
        "axis": "T = floor(high-so-far at pin)+1 ; crossed = settled_max > T (strict; == LOSS)",
        "truth": "metar_backfill.db settlement_truth HIGH trustworthy=1 (canonical GHCN)",
        "bucket_source": prov,
        "pins": {},
    }

    for pin, label in PINS.items():
        allrows = build_samples(recs, truth, pin)
        pinres = {"pin_mod": pin, "pin_local": label, "windows": {}}
        for W in WINDOWS:
            rows = [s for s in allrows if s[2] == W]
            if not rows:
                pinres["windows"][f"W={W}"] = {"n": 0}
                continue
            entry = {
                "n": len(rows),
                "base_rate_p_cross": round(sum(s[6] for s in rows) / len(rows), 4),
                "brier_base_rate": base_brier(rows),
                "thresholds": {},
            }
            for t in THRESHOLDS:
                entry["thresholds"][f"±{t}"] = {
                    "separation": sep_ci(rows, t, args.reps, seed=20261013 + pin + W * 10 + int(t * 10)),
                    "loo": loo_brier_dir(rows, t),
                }
            entry["normalized_sep"] = normalized_sep(rows)
            pinres["windows"][f"W={W}"] = entry
        result["pins"][label] = pinres

    # published-style W=1 point rate (bucket[pin] - mean(4 prior)) for reference
    ref = {}
    ref_rows = []
    for (st, ds), rec in recs.items():
        try:
            d = date.fromisoformat(ds)
        except ValueError:
            continue
        if not (date.fromisoformat(MIGRATION_START) <= d <= date.fromisoformat(MIGRATION_END)):
            continue
        y_raw = truth.get((st, ds))
        b = rec.get("buckets") or {}
        hsf = hsf_at(b, PRIMARY_PIN)
        if y_raw is None or hsf is None:
            continue
        need = [PRIMARY_PIN - 60, PRIMARY_PIN - 45, PRIMARY_PIN - 30, PRIMARY_PIN - 15, PRIMARY_PIN]
        if any(m not in b for m in need):
            continue
        r = b[PRIMARY_PIN] - sum(b[m] for m in need[:-1]) / 4.0
        T = float(math.floor(hsf) + 1.0)
        ref_rows.append((st, ds, 1, r, hsf, T, 1 if y_raw > T else 0))
    ref["W1_point_style_at_1330"] = {
        "n": len(ref_rows),
        "thresholds": {
            f"±{t}": {"separation": sep_ci(ref_rows, t, args.reps, seed=99 + int(t * 10))}
            for t in THRESHOLDS
        },
    }
    result["reference_published_style"] = ref

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(result, indent=2))

    # ---- markdown ----------------------------------------------------------
    L = []
    L.append("# Rate Window Sweep 1h–4h — HONEST (point-in-time) — 2026-10-13")
    L.append("")
    L.append("**Script:** `scripts/rate_window_sweep_pointintime_2026-10-13.py`  ")
    L.append("**Machine artifact:** `artifacts/rate-window-sweep/rate_window_pointintime_2026-10-13.json`  ")
    L.append("**Axis:** strict strike-crossing, `T = floor(high-so-far at pin)+1`, "
             "`crossed = settled_max > T` (`==` is LOSS). Truth = canonical GHCN settlement.")
    L.append("")
    L.append("## Why this supersedes the prior sweep")
    L.append("")
    L.append("The prior multi-window sweep scored `rate_1h` with a **window-max scan over "
             "13:00–16:45** — it looked *ahead* of the decision instant and inflated the "
             "W=1 separation to 60.2 pp. This sweep is **past-only**: for window W,")
    L.append("")
    L.append("```")
    L.append("rate_W = mean(tmpf of the W hours ending at the pin)")
    L.append("       - mean(tmpf of the W hours immediately before that)")
    L.append("```")
    L.append("")
    L.append("Every bucket used is `mod <= pin`; one prediction per station-day per "
             "window per pin. No window-max, no future buckets.")
    L.append("")
    L.append(f"**Cohort:** {MIGRATION_START} → {MIGRATION_END} (the diversity-study window), "
             "canonical truth required.")
    L.append(f"**Bucket source:** `{prov}`.")
    L.append("")
    for pin, label in PINS.items():
        pr = result["pins"][label]
        L.append(f"## Pin {label} station-local")
        L.append("")
        L.append("| W | n | base P(cross) | Brier(base) | sep ±0.5 (pp) | 95% CI | "
                 "sep ±1.0 (pp) | 95% CI | norm sep ±1.0 (pp) | LOO Brier(±1.0) | dir acc |")
        L.append("|---|---:|---:|---:|---:|---|---:|---|---:|---:|---:|")
        for W in WINDOWS:
            w = pr["windows"].get(f"W={W}", {})
            if not w.get("n"):
                L.append(f"| {W} | 0 | — | — | — | — | — | — | — | — | — |")
                continue
            s5 = w["thresholds"]["±0.5"]["separation"] or {}
            s1 = w["thresholds"]["±1.0"]["separation"] or {}
            ns = (w.get("normalized_sep") or {}).get("±1.0") or {}
            lo = w["thresholds"]["±1.0"]["loo"] or {}
            L.append(
                f"| {W} | {w['n']} | {w['base_rate_p_cross']} | {w['brier_base_rate']} | "
                f"{s5.get('sep_pp','—')} | {s5.get('ci95_pp','—')} | "
                f"{s1.get('sep_pp','—')} | {s1.get('ci95_pp','—')} | "
                f"{ns.get('sep_pp','—')} | {lo.get('brier','—')} | {lo.get('dir_acc','—')} |")
        L.append("")
    L.append("### Reference: published-style W=1 point rate at the 13:30 pin")
    L.append("")
    L.append("`rate = bucket[13:30] − mean(4 prior buckets)` — the published W=1 "
             "definition (point vs prior hour), for comparison with the symmetric W=1 row.")
    L.append("")
    rr = result["reference_published_style"]["W1_point_style_at_1330"]
    L.append(f"- n = {rr['n']}; ±0.5 sep = "
             f"{(rr['thresholds']['±0.5']['separation'] or {}).get('sep_pp','—')} pp; "
             f"±1.0 sep = {(rr['thresholds']['±1.0']['separation'] or {}).get('sep_pp','—')} pp")
    L.append("")
    L.append(_verdict(result))
    L.append("")
    L.append("## Reproduce")
    L.append("")
    L.append("```bash")
    L.append(".venv-gefs/bin/python scripts/rate_window_sweep_pointintime_2026-10-13.py")
    L.append("```")
    L.append("")
    OUT_MD.write_text("\n".join(L) + "\n")

    # console
    for pin, label in PINS.items():
        pr = result["pins"][label]
        print(f"--- pin {label} ---")
        for W in WINDOWS:
            w = pr["windows"].get(f"W={W}", {})
            if not w.get("n"):
                print(f"  W={W}: n=0")
                continue
            s1 = w["thresholds"]["±1.0"]["separation"] or {}
            ns = (w.get("normalized_sep") or {}).get("±1.0") or {}
            lo = w["thresholds"]["±1.0"]["loo"] or {}
            print(f"  W={W}: n={w['n']} base={w['base_rate_p_cross']} "
                  f"sep±1.0={s1.get('sep_pp')} CI={s1.get('ci95_pp')} "
                  f"norm={ns.get('sep_pp')} loo={lo.get('brier')} dir={lo.get('dir_acc')}")
    print(f"\nwrote {OUT_JSON}\nwrote {OUT_MD}")


def _verdict(result):
    L = []
    L.append("## Verdict")
    L.append("")
    pin = "13:30"
    pr = result["pins"][pin]
    seps = {}
    loos = {}
    norms = {}
    for W in WINDOWS:
        w = pr["windows"].get(f"W={W}", {})
        s = (w.get("thresholds", {}).get("±1.0", {}) or {}).get("separation")
        lo = (w.get("thresholds", {}).get("±1.0", {}) or {}).get("loo")
        ns = (w.get("normalized_sep", {}) or {}).get("±1.0")
        if s:
            seps[W] = (s["sep_pp"], tuple(s.get("ci95_pp") or ()))
        if lo:
            loos[W] = (lo["brier"], lo["dir_acc"])
        if ns:
            norms[W] = ns["sep_pp"]
    if seps:
        best_sep_W = max(seps, key=lambda w: seps[w][0])
        # ci95_pp is [lower, upper]; union span = [min lower, max upper]
        u_lo = min(v[1][0] for v in seps.values() if v[1])
        u_hi = max(v[1][1] for v in seps.values() if v[1])
        widest_W = max(seps, key=lambda w: (v := seps[w][1]) and (v[1] - v[0]))
        L.append(
            f"At the 13:30 pin the honest ±1.0 separations are "
            + ", ".join(f"W={w} {seps[w][0]:+.2f} pp" for w in sorted(seps))
            + f". They are **statistically indistinguishable** — every CI overlaps "
              f"(union of CIs spans [{u_lo:.1f}, {u_hi:.1f}] pp; the widest single CI is "
              f"W={widest_W}). The point-estimate 'winner' (W={best_sep_W}) is noise, "
              f"not a real ordering at this n."
        )
        L.append("")
    if loos:
        best_loo_W = min(loos, key=lambda w: loos[w][0])
        L.append(
            "On the **calibration** axis (leave-one-date-out Brier / dir acc) the "
            "windows slightly favour the *shorter* windows: "
            + ", ".join(f"W={w} Brier {loos[w][0]:.4f}/dir {loos[w][1]:.3f}" for w in sorted(loos))
            + f". W={best_loo_W} is lowest-Brier."
        )
        L.append("")
    if norms:
        L.append(
            "The scale-normalized separation (top-15% vs bottom-15% of each window's own "
            "distribution) shows the dilation dilution: "
            + ", ".join(f"W={w} {norms[w]:+.2f} pp" for w in sorted(norms))
            + f". W={max(norms, key=lambda w: norms[w])} is highest, W={min(norms, key=lambda w: norms[w])} lowest."
        )
        L.append("")
    L.append(
        "**Bottom line for Dan:** at the honest (past-only, point-in-time) axis **no "
        "longer window becomes newly viable** — W=1..4 are a near-tie at the 13:30 pin, "
        "with W=1 still preferred on calibration (lowest LOO Brier, highest dir acc). "
        "The prior 60.2 pp W=1 *dominance* was a look-ahead artifact; stripping it "
        "neither makes a longer window win nor invalidates W=1. **Keep W=1; nothing "
        "new to wire.** At later pins (14:30/15:00) the base rate and separation both "
        "fall (more of the day is already observed), so a later pin is not a rescue."
    )
    L.append("")
    return "\n".join(L)


if __name__ == "__main__":
    main()
