#!/usr/bin/env python3
"""
TRACK 3 / P3-B — No-skill null baseline.

Before declaring the model has edge, check it beats the trivial "always predict
down" null on the SAME station-days. If the model's directional hit rate is not
significantly above the null, the "edge" is an artifact of base rates.

Uses the correct strike-crossing axis (core/settlement_labels.py): outcome =
1[high > strike] (equality = loss for the YES side).

Run: .venv-gefs/bin/python scripts/null_baseline.py
"""
import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "core"))


def main():
    # Null: "always down" on the strike-crossing axis == always predict high <= strike.
    # We evaluate against the Kalshi-settled outcomes if available, else GHCN highs vs
    # each station's own daily strike grid median.
    snap = REPO / "data" / "kalshi_market_snapshots.db"
    gh = REPO / "data" / "ghcn_settlements.db"
    if not (snap.exists() and gh.exists()):
        print("NULL: SKIP — required DBs missing")
        return 0

    truth = {}
    g = sqlite3.connect(f"file:{gh}?mode=ro", uri=True)
    try:
        for st, d, hi in g.execute("SELECT station, date, high_f FROM ghcn_settlements WHERE high_f IS NOT NULL"):
            truth[(st, d[:10])] = hi
    except Exception:
        pass
    g.close()

    c = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
    # REVIEW FIX (F11): restrict to THRESHOLD ('-T') tickers only. Mixing '-T'
    # (threshold) and '-B' (bucket) tickers puts two different propositions on
    # one grid; the base rate would be meaningless. Matches D1 (threshold default).
    rows = c.execute(
        """SELECT station, market_ticker, strike_temp, mid_price
           FROM market_snapshots
           WHERE market_type IN ('HIGH','binary') AND strike_temp IS NOT NULL
             AND market_ticker LIKE 'KXHIGH%' AND market_ticker LIKE '%-T%'"""
    ).fetchall()
    c.close()

    months = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,"JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
    n = 0
    # Null "always up" (high > strike) and "always down" both collapse to base rate.
    n_up = 0
    for station, ticker, strike, mid in rows:
        try:
            parts = ticker.split("-")
            yy = int(parts[1][:2]); mon = parts[1][2:5]; dd = int(parts[1][5:7])
            date = f"20{yy:02d}-{months[mon]:02d}-{dd:02d}"
        except Exception:
            continue
        y = truth.get((station, date))
        if y is None:
            continue
        n += 1
        if y > strike:  # strict; equality = loss (HIGH YES)
            n_up += 1

    if n == 0:
        print("NULL: SKIP — no resolvable station-day-strikes")
        return 0
    base_up = n_up / n
    best_null = max(base_up, 1 - base_up)
    print(f"NULL BASELINE: n={n} | P(high>strike)={base_up:.3f} | best null accuracy={best_null:.3f}")
    print(f"  -> any model must beat {best_null:.3f} on the SAME station-days to claim edge")
    return 0


if __name__ == "__main__":
    sys.exit(main())
