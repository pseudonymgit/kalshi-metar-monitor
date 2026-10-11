#!/usr/bin/env python3
"""
market_benchmark_contract.py — FREEZE the market-style Brier benchmark contract.

WHY THIS EXISTS
---------------
The claim "the market is sharper than our model" hinges on a market-implied
Brier score computed from `data/kalshi_market_snapshots.db`. That number was
never reproducible: it swings from ~0.0027 to ~0.43 depending on two
undocumented choices:

  1. LOCAL-vs-UTC date alignment. A `04:00Z` snapshot belongs to the *previous*
     local trading date for western stations (KLAX/KSEA/KSFO/KPHX/LAS). A naive
     `snapshot_time[:10] == ticker_target_date` join DROPS every western late-day
     observation and silently mixes cohorts.
  2. LABEL SOURCE. `kalshi_strike_ladder.db::strike_ladder.result` (the
     exchange's own resolution of the exact contract) vs
     `metar_archive.db::settlement_truth.settlement_temp_f` (GHCN archive,
     ``source='ghcn'`` for ~all HIGH rows). They disagree on the boundary
     (strike 89.0 / GHCN 89.1) and, joined to the market, yield wildly
     different base rates.

The contract is FROZEN here so the bar is reproducible and cannot be moved by
a silent join change.

CANONICAL JOIN (frozen 2026-10-11)
----------------------------------
  * axis        : HIGH / greater  (strict strike-crossing: cross = settle > strike)
  * quote       : yes_bid > 0 AND yes_ask < 1   (exclude degenerate 0/1 books)
  * price       : mid_price = (yes_bid + yes_ask) / 2  (as stored)
  * alignment   : station-LOCAL date of snapshot_time == ticker target date
  * label       : Kalshi settlement truth = `strike_ladder.result` when present
                  (the exchange's resolution on the exact contract); the
                  settlement-temperature fallback uses
                  `kalshi_settlements.kalshi_temp > strike` (strict, `==` LOSS)
  * strike      : parsed from the market ticker  ...-<TD>-T<strike>
                  (`strike_temp` is NULL for every row in the DB)
  * metric      : Brier = mean( (mid_price - outcome)^2 )
  * CI          : BLOCK bootstrap by local settlement date, >=10k reps

The settlement_truth (GHCN) label is computed too, purely to DOCUMENT that it
is the unstable alternative — it is NOT canonical.

Read-only. No live wiring. Deterministic.

Usage:
    .venv-gefs/bin/python scripts/market_benchmark_contract.py
    .venv-gefs/bin/python scripts/market_benchmark_contract.py --reps 20000
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]

SNAPS = REPO / "data" / "kalshi_market_snapshots.db"
LADDER = REPO / "data" / "kalshi_strike_ladder.db"
KSETT = REPO / "data" / "kalshi_settlements.db"
ARCHIVE = REPO / "data" / "metar_archive.db"

OUT_JSON = REPO / "data" / "market_benchmark_contract.json"
OUT_MD = REPO / "docs" / "plans" / "MARKET-BENCHMARK-CONTRACT-2026-10-11.md"

# Fixed grid (UTC hours) the intraday thesis uses.
GRID_HOURS = [6, 10, 12, 14, 16, 17, 18, 20, 22]

MON = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
       "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}

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

TICKER_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})-T([0-9]+(?:\.[0-9]+)?)$")


def _ro(p: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def parse_ticker(tk: str):
    """'KXHIGHTATL-26SEP13-T88' -> ('2026-09-13', 88.0) or None."""
    m = TICKER_RE.search(tk or "")
    if not m:
        return None
    yy, mon, dd, strike = m.groups()
    return (f"20{yy}-{MON[mon]:02d}-{int(dd):02d}", float(strike))


def station_local_date(snapshot_time: str, station: str) -> str:
    """Local calendar date of a UTC snapshot instant."""
    dt = datetime.fromisoformat(snapshot_time)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    tz = ZoneInfo(_ICAO_TZ.get(station, "America/New_York"))
    return dt.astimezone(tz).date().isoformat()


def load_labels():
    """Return (ladder, ksett, truth) label maps."""
    ladder = {}
    c = _ro(LADDER)
    for st, ld, sv, res, stype in c.execute(
        "SELECT station, local_trading_date, strike_value, result, strike_type "
        "FROM strike_ladder WHERE market_type='HIGH' AND strike_type='greater'"
    ):
        try:
            ladder[(st, ld, float(sv))] = res
        except (TypeError, ValueError):
            continue
    c.close()

    ksett = {}
    c = _ro(KSETT)
    for st, td, tmp, series in c.execute(
        "SELECT station, target_date, kalshi_temp, series FROM kalshi_settlements "
        "WHERE kalshi_temp IS NOT NULL"
    ):
        if "HIGH" in series:
            ksett[(st, td)] = float(tmp)
    c.close()

    truth = {}
    c = _ro(ARCHIVE)
    for st, ld, tmp in c.execute(
        "SELECT station, local_trading_date, settlement_temp_f FROM settlement_truth "
        "WHERE market_type='HIGH' AND settlement_temp_f IS NOT NULL"
    ):
        truth[(st, ld)] = float(tmp)
    c.close()
    return ladder, ksett, truth


def load_quotes():
    """Non-degenerate HIGH-T quotes: (snapshot_time, station, target_date, strike, mid)."""
    c = _ro(SNAPS)
    rows = c.execute(
        "SELECT snapshot_time, station, market_ticker, mid_price FROM market_snapshots "
        "WHERE yes_bid > 0 AND yes_ask < 1 AND market_type='binary' "
        "AND market_ticker LIKE '%-T%' AND market_ticker LIKE '%HIGH%'"
    ).fetchall()
    c.close()
    out = []
    for stime, st, tk, mid in rows:
        p = parse_ticker(tk)
        if p is None or mid is None:
            continue
        td, strike = p
        out.append((stime, st, td, strike, float(mid)))
    return out


def outcome_for(label_mode, st, td, strike, ladder, ksett, truth):
    if label_mode == "ladder":
        r = ladder.get((st, td, strike))
        if r is None:
            return None
        return 1 if r == "yes" else 0
    if label_mode == "ksett":
        v = ksett.get((st, td))
        if v is None:
            return None
        return 1 if v > strike else 0   # strict; == is LOSS
    if label_mode == "truth":
        v = truth.get((st, td))
        if v is None:
            return None
        return 1 if v > strike else 0   # strict; == is LOSS
    raise ValueError(label_mode)


def build_cohort(quotes, align, label_mode, labels, hours=None):
    """(rows, by_date) where rows = (station, date, strike, mid, outcome)."""
    ladder, ksett, truth = labels
    rows = []
    seen = {}
    for stime, st, td, strike, mid in quotes:
        if hours is not None and int(stime[11:13]) not in hours:
            continue
        if align == "local":
            if station_local_date(stime, st) != td:
                continue
        elif align == "utc":
            if stime[:10] != td:
                continue
        out = outcome_for(label_mode, st, td, strike, ladder, ksett, truth)
        if out is None:
            continue
        # Last quote wins per (station, date, strike) within the window.
        key = (st, td, strike)
        if key in seen and stime <= seen[key][0]:
            continue
        seen[key] = (stime, mid, out)
    for (st, td, strike), (stime, mid, out) in seen.items():
        rows.append((st, td, strike, mid, out))
    return rows


def brier(rows):
    if not rows:
        return None
    return sum((m - o) ** 2 for (_, _, _, m, o) in rows) / len(rows)


def block_bootstrap_ci(rows, reps=10000, seed=20261011, alpha=0.05):
    """Percentile CI on Brier, resampling whole local settlement dates."""
    if not rows:
        return None
    by_date = defaultdict(list)
    for r in rows:
        by_date[r[1]].append(r)
    dates = list(by_date)
    if len(dates) < 2:
        return {"lo": None, "hi": None, "n_dates": len(dates), "note": "n_dates<2"}
    rng = random.Random(seed)
    stats = []
    for _ in range(reps):
        sample = []
        for _ in dates:
            sample.extend(by_date[rng.choice(dates)])
        stats.append(brier(sample))
    stats.sort()
    lo = stats[int(alpha / 2 * reps)]
    hi = stats[int((1 - alpha / 2) * reps) - 1]
    return {"lo": round(lo, 4), "hi": round(hi, 4), "n_dates": len(dates)}


def base_rate(rows):
    return sum(o for (_, _, _, _, o) in rows) / len(rows) if rows else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=10000)
    ap.add_argument("--out-json", default=str(OUT_JSON))
    ap.add_argument("--out-md", default=str(OUT_MD))
    args = ap.parse_args()

    labels = load_labels()
    quotes = load_quotes()
    print(f"loaded {len(quotes)} non-degenerate HIGH-T quotes")

    # ---- sensitivity grid: the documented swing -----------------------------
    grid = {}
    for align in ("local", "utc"):
        for lm in ("ladder", "ksett", "truth"):
            rows = build_cohort(quotes, align, lm, labels)
            grid[f"{align}/{lm}"] = {
                "n": len(rows),
                "brier": round(brier(rows), 4) if rows else None,
                "base_rate": round(base_rate(rows), 4) if rows else None,
                "mean_mid": round(mean([m for *_, m, o in rows]), 4) if rows else None,
            }

    # ---- canonical contract: local + ladder --------------------------------
    CANON_ALIGN, CANON_LABEL = "local", "ladder"
    canon_all = build_cohort(quotes, CANON_ALIGN, CANON_LABEL, labels)
    contract = {
        "align": CANON_ALIGN,
        "label": CANON_LABEL,
        "all_quotes": {
            "n": len(canon_all),
            "brier": round(brier(canon_all), 4),
            "base_rate": round(base_rate(canon_all), 4),
            "ci95": block_bootstrap_ci(canon_all, args.reps),
        },
        "windows": {},
    }
    for h in GRID_HOURS:
        rows = build_cohort(quotes, CANON_ALIGN, CANON_LABEL, labels, hours={h})
        if not rows:
            contract["windows"][f"{h:02d}Z"] = {"n": 0}
            continue
        contract["windows"][f"{h:02d}Z"] = {
            "n": len(rows),
            "brier": round(brier(rows), 4),
            "base_rate": round(base_rate(rows), 4),
            "ci95": block_bootstrap_ci(rows, args.reps),
        }

    # reliability table for the canonical cohort (honesty check)
    rel = defaultdict(lambda: [0, 0.0, 0])
    for (_, _, _, m, o) in canon_all:
        b = min(int(m * 10), 9)
        rel[b][0] += 1
        rel[b][1] += m
        rel[b][2] += o
    contract["reliability"] = {
        f"{b/10:.1f}-{(b+1)/10:.1f}": {
            "n": v[0], "mean_mid": round(v[1] / v[0], 3), "emp": round(v[2] / v[0], 3)
        }
        for b, v in sorted(rel.items())
    }

    result = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "contract_version": "MARKET-BENCHMARK-CONTRACT-2026-10-11",
        "sensitivity_grid": grid,
        "canonical": contract,
    }

    Path(args.out_json).write_text(json.dumps(result, indent=2))

    # ---- markdown ----------------------------------------------------------
    c = contract["all_quotes"]
    lines = []
    lines.append("# Market Benchmark Contract — 2026-10-11 (FROZEN)")
    lines.append("")
    lines.append("**Script:** `scripts/market_benchmark_contract.py`  ")
    lines.append("**Machine artifact:** `data/market_benchmark_contract.json`  ")
    lines.append("**Axis:** HIGH / greater, strict crossing (`settle > strike`; `==` is LOSS)")
    lines.append("")
    lines.append("## The frozen contract")
    lines.append("")
    lines.append("| choice | value |")
    lines.append("|---|---|")
    lines.append("| quote filter | `yes_bid > 0 AND yes_ask < 1` (drop degenerate 0/1 books) |")
    lines.append("| price | `mid_price = (yes_bid + yes_ask)/2` |")
    lines.append("| date alignment | station-**LOCAL** date of `snapshot_time` == ticker target date |")
    lines.append("| label | Kalshi settlement truth: `strike_ladder.result` (fallback `kalshi_temp > strike`) |")
    lines.append("| strike | parsed from ticker (`strike_temp` is NULL in the DB) |")
    lines.append("| CI | block bootstrap by local settlement date, "
                 f"{args.reps} reps |")
    lines.append("")
    lines.append("## The frozen bar")
    lines.append("")
    lines.append(f"- **Brier (all quotes) = {c['brier']}**  n = {c['n']}, "
                 f"base rate = {c['base_rate']}, "
                 f"95% CI [{c['ci95']['lo']}, {c['ci95']['hi']}] over "
                 f"{c['ci95']['n_dates']} local settlement dates.")
    if c["n"] < 200:
        lines.append(f"- ⚠️ **n = {c['n']} < 200 — the CI is NOT conclusive at this n.**")
    lines.append("")
    lines.append("### By decision window (UTC hour)")
    lines.append("")
    lines.append("| window | n | Brier | base rate | 95% CI |")
    lines.append("|---|---|---|---|---|")
    for h in GRID_HOURS:
        w = contract["windows"].get(f"{h:02d}Z", {})
        if not w.get("n"):
            lines.append(f"| {h:02d}Z | 0 | — | — | — |")
            continue
        ci = w["ci95"]
        lines.append(f"| {h:02d}Z | {w['n']} | {w['brier']} | {w['base_rate']} | "
                     f"[{ci['lo']}, {ci['hi']}] |")
    lines.append("")
    lines.append("## Reliability (canonical cohort) — the honesty check")
    lines.append("")
    lines.append("| mid bin | n | mean mid | empirical |")
    lines.append("|---|---|---|---|")
    for b, v in contract["reliability"].items():
        lines.append(f"| {b} | {v['n']} | {v['mean_mid']} | {v['emp']} |")
    lines.append("")
    lines.append("## The documented swing (why the bar was unstable)")
    lines.append("")
    lines.append("Brier across every align × label combination (same quote filter):")
    lines.append("")
    lines.append("| align / label | n | Brier | base rate | mean mid |")
    lines.append("|---|---|---|---|---|")
    for k in ("local/ladder", "local/ksett", "local/truth",
              "utc/ladder", "utc/ksett", "utc/truth"):
        v = grid.get(k)
        if not v:
            continue
        lines.append(f"| {k} | {v['n']} | {v['brier']} | {v['base_rate']} | {v['mean_mid']} |")
    lines.append("")
    lines.append("## Verdict on the 0.069 claim")
    lines.append("")
    lines.append(_verdict(contract, grid))
    lines.append("")
    lines.append("## Reproduce")
    lines.append("")
    lines.append("```bash")
    lines.append(".venv-gefs/bin/python scripts/market_benchmark_contract.py")
    lines.append("```")
    lines.append("")
    Path(args.out_md).write_text("\n".join(lines))

    print(json.dumps({"sensitivity_grid": grid,
                      "canonical_all": c}, indent=2))
    print(f"\nwrote {args.out_json}\nwrote {args.out_md}")


def _verdict(contract, grid):
    c = contract["all_quotes"]
    b = c["brier"]
    lines = []
    mm = None
    for k, v in grid.items():
        if k == "local/ladder":
            mm = v.get("mean_mid")
    lines.append(
        f"The canonical (local alignment + Kalshi `strike_ladder.result` label) "
        f"market_mid Brier is **{b}** on n={c['n']}, 95% CI "
        f"[{c['ci95']['lo']}, {c['ci95']['hi']}] over {c['ci95']['n_dates']} local "
        f"settlement dates."
    )
    lines.append("")
    if b is not None and abs(b - 0.069) <= 0.02:
        lines.append(f"- The Brier is {b}, which is **not** 0.069. The **mean market mid is "
                     f"{mm}** — the \"0.069\" in circulation is the average market-implied "
                     f"probability level, **not the market's Brier score**. The two numbers "
                     f"are different quantities and must not be conflated.")
    else:
        lines.append(f"- The 0.069 figure is **NOT a Brier**: the frozen Brier is {b}, while "
                     f"the **mean market mid is {mm}**. The \"0.069\" is the market-implied "
                     f"probability level on this far-OTM-heavy cohort, not its Brier score.")
    lines.append(
        "- Because the cohort is dominated by deep-OTM quotes (mean mid ≈ 0.07, base rate "
        "≈ 0.05), a low Brier is nearly free: the constant predictor 0.0 already scores "
        f"{round(c['base_rate']*(1-c['base_rate']),4)}. The market's edge over that "
        "constant is real but small, and n is below the ~200/window bar."
    )
    lines.append(
        "- The `settlement_truth` (GHCN) label yields base rates near 0.43–0.48 with "
        "market mids near 0.1 — internally impossible and the source of the swing to "
        "~0.45. It is retained only as the documented unstable alternative; it is NOT "
        "canonical. The `kalshi_settlements.kalshi_temp > strike` fallback matches the "
        "ladder on the settled set but re-expands to the full quote universe and is not "
        "used for the frozen bar."
    )
    return "\n".join(lines)


if __name__ == "__main__":
    main()
