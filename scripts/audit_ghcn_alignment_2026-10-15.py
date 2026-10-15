#!/usr/bin/env python3
"""audit_ghcn_alignment_2026-10-15.py — audit data/ghcn_settlements.db for the
same class of date-alignment defect the Kalshi truth table had.

GHCN is the fallback truth where Kalshi has no backfill. It stores daily
(station, date, high_f, low_f) — no ticker field — so the *ticker-vs-target_date*
defect cannot exist directly. But the same misalignment could exist if the daily
date key were shifted one day at ingestion.

METHOD
------
1. Profile the table (rows, range, stations, nulls, unique key).
2. Cross-check against METAR daily truth (`data/metar_backfill.db::daily_stats`,
   keyed (station, date_local)) for the SAME station-day: on overlapping recent
   days, do max_temp_f / min_temp_f agree with GHCN high_f / low_f within 1F?
3. Test a systematic date offset: for each GHCN row try shift -1/0/+1 days and
   report which alignment minimises |GHCN - METAR|. Also do a temporal
   autocorrelation check (GHCN d-1 vs d correlation vs d vs d) so a +-1 shift
   is detectable even at day-to-day noise levels.

Read-only. Writes artifacts/ghcn-alignment-audit-2026-10-15.json.
"""
from __future__ import annotations

import json
import random
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GHCN = REPO / "data" / "ghcn_settlements.db"
METAR = REPO / "data" / "metar_backfill.db"
OUT = REPO / "artifacts" / "ghcn-alignment-audit-2026-10-15.json"

random.seed(20261015)


def main() -> int:
    g = sqlite3.connect(f"file:{GHCN}?mode=ro", uri=True)
    m = sqlite3.connect(f"file:{METAR}?mode=ro", uri=True)

    prof = {
        "rows": g.execute("SELECT COUNT(*) FROM ghcn_settlements").fetchone()[0],
        "date_min": g.execute("SELECT MIN(date) FROM ghcn_settlements").fetchone()[0],
        "date_max": g.execute("SELECT MAX(date) FROM ghcn_settlements").fetchone()[0],
        "stations": [r[0] for r in g.execute(
            "SELECT DISTINCT station FROM ghcn_settlements ORDER BY 1")],
        "null_high": g.execute("SELECT COUNT(*) FROM ghcn_settlements WHERE high_f IS NULL").fetchone()[0],
        "null_low": g.execute("SELECT COUNT(*) FROM ghcn_settlements WHERE low_f IS NULL").fetchone()[0],
        "dup_station_date": g.execute(
            "SELECT COUNT(*) FROM (SELECT station,date FROM ghcn_settlements "
            "GROUP BY station,date HAVING COUNT(*)>1)").fetchone()[0],
    }
    # daily_stats profile
    ds_prof = {
        "rows": m.execute("SELECT COUNT(*) FROM daily_stats").fetchone()[0],
        "date_min": m.execute("SELECT MIN(date_local) FROM daily_stats").fetchone()[0],
        "date_max": m.execute("SELECT MAX(date_local) FROM daily_stats").fetchone()[0],
        "stations": m.execute("SELECT COUNT(DISTINCT station) FROM daily_stats").fetchone()[0],
    }

    # recent window where both should have coverage
    win_lo, win_hi = "2026-04-01", "2026-10-11"

    # ---- 1) direct same-day agreement ---------------------------------------
    direct = []
    for st, d, hi, lo in g.execute(
            "SELECT station, date, high_f, low_f FROM ghcn_settlements "
            "WHERE date>=? AND date<=?", (win_lo, win_hi)):
        direct.append((st, d, hi, lo))
    ds = {(st, d): (mx, mn) for st, d, mx, mn in m.execute(
        "SELECT station, date_local, max_temp_f, min_temp_f FROM daily_stats "
        "WHERE date_local>=? AND date_local<=?", (win_lo, win_hi))}

    pairs = [(st, d, hi, lo, ds[(st, d)][0], ds[(st, d)][1])
             for (st, d, hi, lo) in direct if (st, d) in ds
             and hi is not None and ds[(st, d)][0] is not None]
    print(f"[ghcn] overlap rows (GHCN∩METAR, {win_lo}..{win_hi}): {len(pairs)}")

    # subsample ~200 for the agreement table (deterministic)
    sample = random.sample(pairs, min(200, len(pairs))) if pairs else []
    dhi = [abs(hi - mx) for _, _, hi, lo, mx, mn in sample]
    agree1 = sum(1 for x in dhi if x <= 1.0)
    agree2 = sum(1 for x in dhi if x <= 2.0)

    def stats(xs):
        if not xs:
            return {}
        xs2 = sorted(xs)
        return {"n": len(xs),
                "mean": round(sum(xs) / len(xs), 3),
                "median": round(xs2[len(xs2) // 2], 3),
                "p90": round(xs2[int(0.9 * (len(xs2) - 1))], 3),
                "max": round(xs2[-1], 3)}

    # ---- 2) systematic offset test on the SAMPLE ----------------------------
    def shift_key(d, days):
        from datetime import date, timedelta
        y, mo, dd = map(int, d.split("-"))
        return (date(y, mo, dd) + timedelta(days=days)).isoformat()

    offs = {}
    for shift in (-1, 0, 1):
        diffs = []
        for st, d, hi, lo, mx, mn in sample:
            k = (st, shift_key(d, shift))
            if k in ds and ds[k][0] is not None:
                diffs.append(abs(hi - ds[k][0]))
        offs[shift] = stats(diffs)

    # ---- 3) full-window offset test (all overlap rows, not just sample) -----
    offs_full = {}
    for shift in (-1, 0, 1):
        diffs = []
        for st, d, hi, lo, mx, mn in pairs:
            k = (st, shift_key(d, shift))
            if k in ds and ds[k][0] is not None:
                diffs.append(abs(hi - ds[k][0]))
        offs_full[shift] = {"mean": round(sum(diffs) / len(diffs), 3) if diffs else None,
                            "n": len(diffs)}

    # ---- 4) GHCN self-consistency (no internal shift possible) --------------
    # autocorrelation of GHCN daily highs: corr(day, day) should exceed corr(day, day+1)
    # if the series is itself coherent (no half-shifted block).
    import math
    gser = {}
    for st, d, hi, lo in direct:
        if hi is not None:
            gser.setdefault(st, {})[d] = hi

    def corr(xs, ys):
        n = len(xs)
        if n < 3:
            return None
        mx = sum(xs) / n; my = sum(ys) / n
        num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
        den = math.sqrt(sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys))
        return num / den if den else None

    from datetime import date, timedelta
    c0, c1 = [], []
    for st, s in gser.items():
        days = sorted(s)
        for i in range(len(days) - 1):
            d0 = days[i]
            y, mo, dd = map(int, d0.split("-"))
            nxt = (date(y, mo, dd) + timedelta(days=1)).isoformat()
            if nxt in s:
                c0.append((s[d0], s[nxt]))
        for i in range(len(days) - 2):
            d0 = days[i]; d1 = days[i + 1]
            y, mo, dd = map(int, d1.split("-"))
            nxt = (date(y, mo, dd) + timedelta(days=1)).isoformat()
            if nxt in s:
                c1.append((s[d0], s[nxt]))
    autoc = {"lag1": round(corr([a for a, b in c0], [b for a, b in c0]), 4) if c0 else None,
             "lag2": round(corr([a for a, b in c1], [b for a, b in c1]), 4) if c1 else None,
             "n_lag1": len(c0), "n_lag2": len(c1)}

    verdict = "clean" if (
        offs_full[0]["mean"] is not None
        and (offs_full[-1]["mean"] is None or offs_full[0]["mean"] < offs_full[-1]["mean"])
        and (offs_full[1]["mean"] is None or offs_full[0]["mean"] < offs_full[1]["mean"])
    ) else "SHIFTED?"

    art = {
        "generated_by": "scripts/audit_ghcn_alignment_2026-10-15.py",
        "ghcn_profile": prof,
        "metar_daily_stats_profile": ds_prof,
        "window": [win_lo, win_hi],
        "overlap_rows": len(pairs),
        "sample_n": len(sample),
        "same_day_abs_diff_F": stats(dhi),
        "agree_within_1F": agree1,
        "agree_within_1F_rate": round(agree1 / len(sample), 4) if sample else None,
        "agree_within_2F": agree2,
        "agree_within_2F_rate": round(agree2 / len(sample), 4) if sample else None,
        "offset_test_sample": {str(k): v for k, v in offs.items()},
        "offset_test_full": {str(k): v for k, v in offs_full.items()},
        "ghcn_autocorr": autoc,
        "verdict": verdict,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(art, indent=2))
    print(json.dumps({k: art[k] for k in
                      ["overlap_rows", "sample_n", "same_day_abs_diff_F",
                       "agree_within_1F_rate", "agree_within_2F_rate",
                       "offset_test_full", "ghcn_autocorr", "verdict"]}, indent=2))
    print(f"[ghcn] artifact -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
