#!/usr/bin/env python3
"""
E2E INTRADAY TEST — the system-level test the Gray Room flagged as missing.

Pieces were verified individually but never AS A SYSTEM. This drives ONE real
station-day through the full chain and asserts the system invariants:

    distribution (GEFS ens + ws_p fit)
        -> intraday METAR replay (high_so_far)
        -> adapter update (truncation)
        -> strike selection (EV vs REAL prices, net of fee)
        -> output

Assertions (the F1-class checks):
    (a) P(high >= X) == 1.0 for every X <= observed high_so_far  (no locked win mispriced)
    (b) the selected strike has POSITIVE EV net of Kalshi fee
    (c) the output is sane: picked strike is within the ladder, side is yes/no, p in [0,1]
    (d) EV selection actually USES the constrained distribution (not a plain Gaussian)

Data-adaptive: picks the most recent station-day that has METAR + settlement +
GEFS ensemble. Flags any missing input rather than silently skipping.

Run: SKIP_WEATHER_COLLECTOR=1 .venv-gefs/bin/python -m pytest tests/test_e2e_intraday.py -q -s
"""
import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "core"))

MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
          "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
C2F = lambda c: c * 9 / 5 + 32


def _ro(name):
    return sqlite3.connect(f"file:{ROOT/'data'/name}?mode=ro", uri=True)


def pick_test_day(station="KNYC", min_obs=20):
    """Most recent station-day with METAR + settlement + GEFS(step24) AND enough
    intraday METAR coverage to be a meaningful replay.

    E2E fix (2026-09-28): the first version accepted ANY day with >=1 METAR obs and
    picked 2026-09-25 (only 5 pre-dawn obs) — an incomplete day that produced a
    false-green pass. Require enough observations to actually exercise the intraday
    update.
    """
    m = _ro("metar_backfill.db"); s = _ro("kalshi_settlements.db"); g = _ro("gefs_archive.db")
    n_obs = dict(m.execute(
        "SELECT date_utc, COUNT(*) FROM metar_observations WHERE station=? AND temp_f IS NOT NULL GROUP BY 1",
        (station,)))
    s_ok = {d for (d,) in s.execute(
        "SELECT DISTINCT target_date FROM kalshi_settlements WHERE station=?", (station,))}
    g_ok = {d for (d,) in g.execute(
        "SELECT DISTINCT target_date FROM gefs_archive WHERE station=? AND step=24 AND blob_ok=1", (station,))}
    m.close(); s.close(); g.close()
    common = sorted((d for d, n in n_obs.items()
                     if n >= min_obs and d in s_ok and d in g_ok), reverse=True)
    return common[0] if common else None


def load_ladder(station, iso_date):
    """Real Kalshi threshold ladder for a station-day (strike parsed from ticker).

    E2E/F16: bound `before` to the end of the station's LOCAL day so we read a
    pre-settlement quote, not the 0.5 placeholder written after resolution.
    """
    from core.price_intake import load_strike_ladder, SNAPSHOTS
    import datetime as _dt
    mon = [k for k, v in MONTHS.items() if v == int(iso_date[5:7])][0]
    suffix = f"{iso_date[2:4]}{mon}{iso_date[8:10]}"
    # local day end (EDT = UTC-4) -> 04:00 UTC the next day
    d = _dt.date.fromisoformat(iso_date) + _dt.timedelta(days=1)
    before = f"{d.isoformat()}T04:00:00"
    return load_strike_ladder(SNAPSHOTS, station, suffix, "HIGH",
                              contract_semantic="threshold", before=before)


def replay_metar(station, iso_date):
    """Return the ordered (timestamp, temp_f) METAR series for the day."""
    m = _ro("metar_backfill.db")
    rows = m.execute(
        "SELECT timestamp_utc, temp_f FROM metar_observations "
        "WHERE station=? AND date_utc=? AND temp_f IS NOT NULL ORDER BY timestamp_utc",
        (station, iso_date)).fetchall()
    m.close()
    return rows


def test_e2e_intraday():
    from core.intraday.distribution_adapter import from_ws_p, update_from_intraday, select
    from core.strike_selection_ev import kalshi_fee

    station = "KNYC"
    day = pick_test_day(station)
    assert day, f"no complete station-day for {station} (METAR+settle+GEFS)"

    # --- seed distribution from GEFS ens + ws_p fit ---
    g = _ro("gefs_archive.db")
    row = g.execute("SELECT ensemble_mean, ensemble_min, ensemble_max, n_members "
                    "FROM gefs_archive WHERE station=? AND target_date=? AND step=24 AND blob_ok=1",
                    (station, day)).fetchone()
    g.close()
    assert row, f"no GEFS step24 row for {station} {day}"
    ens_mean_c, ens_min_c, ens_max_c, n_members = row
    ens_mean_f = C2F(ens_mean_c)
    ens_std_f = (C2F(ens_max_c) - C2F(ens_min_c)) / 2.0  # rough spread proxy

    dist = json.load(open(ROOT / "data" / "ws_p_distribution.json"))
    month = int(day[5:7])
    state = from_ws_p(dist, station, ens_mean_f, ens_std_f, month, date=day)

    # --- replay METAR intraday, updating high_so_far by WALL-CLOCK fraction ---
    series = replay_metar(station, day)
    assert series, f"no METAR series for {station} {day}"
    high_so_far = None
    for _ts, temp_f in series:
        high_so_far = temp_f if high_so_far is None else max(high_so_far, temp_f)
    # E2E fix (2026-09-28): day_fraction is WALL-CLOCK time-of-day, not observation
    # ordinal. The old `(i+1)/len(series)` conflated 5 obs spread over a day with a
    # full day, and combined with the compounding sigma bug produced p=1.0 on a
    # strike ABOVE the observed high. Use the last observation's time-of-day.
    def _frac(ts):
        # ts like 2026-09-24T21:51:00+00:00 -> fraction of the local day elapsed
        hh = int(ts[11:13]); mm = int(ts[14:16])
        # KNYC local time ≈ UTC-4 (EDT in Sep); a day runs local 00:00-24:00
        local_h = (hh - 4) % 24
        return min(1.0, (local_h * 60 + mm) / (24 * 60))
    state = update_from_intraday(state, high_so_far, _frac(series[-1][0]))

    observed_high = high_so_far
    print(f"\nE2E {station} {day}: n_metar={len(series)} observed_high={observed_high} "
          f"ens_mean={ens_mean_f:.1f} mu={state.mu:.1f} sigma={state.sigma:.2f}")

    # --- (a) truncation: P(high>=X)==1 for X<=observed_high ---
    for x in [observed_high - 5, observed_high - 1, observed_high]:
        assert state.p_ge(x) == 1.0, f"(a) FAIL: p_ge({x})={state.p_ge(x)} != 1.0 (<= high_so_far={observed_high})"

    # --- ladder + EV selection ---
    ladder = load_ladder(station, day)
    assert ladder, f"(data) no threshold ladder for {station} {day}"
    # E2E/F16 guard: prices must be REAL quotes, not the post-settlement 0.5
    # placeholder. If every mid is exactly 0.5, the bound failed — flag it.
    real = [s for s in ladder if s["yes_price"] not in (0.0, 0.5, 1.0)]
    assert real, f"(data) only placeholder prices (0/0.5/1) for {station} {day} — pre-settlement bound failed"
    best = select(state, ladder)
    assert best is not None, "(b) FAIL: no strike selected"

    # --- (b) selected strike EV must be positive net of fee ---
    for s in ladder:
        if s["strike"] == best["strike"]:
            side = best["side"]
            price = s["yes_price"] if side == "yes" else s["no_price"]
            p = state.p_ge(s["strike"])
            pp = p if side == "yes" else (1 - p)
            fee = kalshi_fee(price, pp) if price > 0 else 0.0
            ev = pp * (1 - price - fee) - (1 - pp) * (price + fee)
            assert ev > 0, f"(b) FAIL: selected {best} has EV={ev:.4f} <= 0 net of fee={fee}"
            print(f"  picked {best['strike']} {side} p={p:.3f} price={price:.3f} fee={fee:.4f} EV={ev:.4f}")
            break

    # --- (c) sanity ---
    assert best["side"] in ("yes", "no")
    assert any(abs(s["strike"] - best["strike"]) < 1e-9 for s in ladder), "(c) FAIL: strike not in ladder"
    assert 0.0 <= best["p"] <= 1.0

    # --- (d) EV path uses the CONSTRAINED distribution (F1 regression guard) ---
    # a strike <= observed_high must price at p=1.0 through select(), not the raw Gaussian
    below = [s for s in ladder if s["strike"] <= observed_high]
    if below:
        s0 = below[0]
        # force a losing book so a p=1.0 win is the only positive-EV choice
        probe = [{"strike": s0["strike"], "yes_price": 0.9, "no_price": 0.1}]
        pb = select(state, probe)
        assert pb is not None and pb["p"] == 1.0, \
            f"(d) F1 REGRESSION: select() priced strike<=high_so_far at p={pb and pb['p']} (must be 1.0)"
    print("  ✅ (a) truncation  (b) EV>0 net fee  (c) sane  (d) constrained path")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-s"]))
