#!/usr/bin/env python3
"""
R/R-1 review QC — read-only, deterministic re-derivation of ground-truth axis,
provenance, and lookahead numbers. Independent of the sweep harness.

Does NOT mutate any DB. Emits the exact numbers the review report cites.
"""
import os
import sqlite3

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
METAR_DB = os.path.join(BASE, "data", "metar_backfill.db")
NWP_DB = os.path.join(BASE, "data", "nwp_forecasts.db")
LADDER_DB = os.path.join(BASE, "data", "kalshi_strike_ladder.db")


def strike_crossing(temp, strike, mt):
    if temp is None or strike is None:
        return None
    try:
        s, k = float(temp), float(strike)
    except (TypeError, ValueError):
        return None
    mt = (mt or "").upper()
    if mt == "HIGH":
        return "up" if s > k else "down"
    if mt == "LOW":
        return "down" if s < k else "up"
    return None


def main():
    c = sqlite3.connect(METAR_DB)
    cur = c.cursor()

    # 1. settlement_truth provenance
    cur.execute("SELECT COUNT(*), SUM(strike_price IS NOT NULL), "
                "SUM(strike_type='greater'), SUM(strike_type='less'), "
                "SUM(strike_type='between'), SUM(trustworthy=0) FROM settlement_truth")
    tot, with_strike, greater, less, between, untrusted = cur.fetchone()
    print(f"[provenance] settlement_truth total={tot} with_strike={with_strike} "
          f"greater={greater} less={less} between={between} untrusted={untrusted}")

    cur.execute("SELECT temp_source, COUNT(*) FROM settlement_truth "
                "WHERE strike_price IS NOT NULL AND trustworthy=1 AND strike_type IN ('greater','less') "
                "GROUP BY temp_source")
    print("[provenance] temp_source:", cur.fetchall())

    # 2. Reproduce load_labels() exactly
    cur.execute("""
        SELECT station, market_type, local_trading_date, settlement_temp_f,
               temp_source, trustworthy, strike_price, strike_type
        FROM settlement_truth
        WHERE settlement_temp_f IS NOT NULL AND strike_price IS NOT NULL
    """)
    rows = cur.fetchall()
    between = untrusted = no_temp = unknown_mt = 0
    up = down = 0
    mt_up, mt_tot = {}, {}
    for st, mt, date, temp, src, trust, strike, stype in rows:
        if trust == 0:
            untrusted += 1; continue
        if stype == "between":
            between += 1; continue
        d = strike_crossing(temp, strike, mt)
        if d is None:
            if temp is None:
                no_temp += 1
            else:
                unknown_mt += 1
            continue
        if d == "up":
            up += 1
        else:
            down += 1
        mt_tot[mt] = mt_tot.get(mt, 0) + 1
        if d == "up":
            mt_up[mt] = mt_up.get(mt, 0) + 1
    n = up + down
    print(f"[axis] labels n={n} up={up} down={down} base_rate_up={up/n:.4f}")
    for mt in sorted(mt_tot):
        print(f"[axis]   {mt}: up={mt_up.get(mt,0)/mt_tot[mt]:.4f} n={mt_tot[mt]}")
    print(f"[axis] excluded between={between} untrusted={untrusted} no_temp={no_temp} unknown_mt={unknown_mt}")
    c.close()

    # 3. NWP lookahead
    c = sqlite3.connect(NWP_DB)
    cur = c.cursor()
    cur.execute("SELECT COUNT(*), SUM(fetch_date > target_date), SUM(fetch_date <= target_date) FROM nwp_forecasts")
    tot, lk, cont = cur.fetchone()
    print(f"[lookahead] nwp_forecasts total={tot} lookahead={lk} contemporaneous={cont} pct={100*lk/tot:.2f}%")
    c.close()

    # 4. Strike ladder
    c = sqlite3.connect(LADDER_DB)
    cur = c.cursor()
    cur.execute("SELECT COUNT(*) FROM strike_ladder")
    print(f"[provenance] kalshi_strike_ladder strike_ladder rows={cur.fetchone()[0]}")
    c.close()


if __name__ == "__main__":
    main()
