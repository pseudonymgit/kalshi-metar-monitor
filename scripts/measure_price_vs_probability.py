#!/usr/bin/env python3
"""
READ-ONLY deterministic measurement: entry price vs true settlement probability.

Dan's challenge (2026-09-19): "the down side trades at ~90c" was assumed to mean
price == settlement probability (efficient market). He says he can enter now at
35c. If true, entry price < true P(win), i.e. a PRICING edge not a signal edge.

This script measures the gap between the ACTUAL market price (yes_bid/yes_ask/mid)
and the TRUE settlement outcome (from settlement_truth), for all Kalshi HIGH/LOW
strike-crossing contracts across all 20 stations.

No AI. No writes. Pure SQL + arithmetic.
"""
import sqlite3
import re
from collections import defaultdict
from datetime import datetime

SNAP = 'data/kalshi_market_snapshots.db'
TRUTH = 'data/z8-frozen/z8_settlement_truth.db'

# ticker parse: KXHIGHLAX-26SEP08-T89  /  -B88.5
TICK = re.compile(r'^KX(HIGH|LOW)(\w+)-(\d{2}[A-Z]{3}\d{2})-(T|B)(\d+(?:\.\d+)?)$')

MONTHS = {'JAN':1,'FEB':2,'MAR':3,'APR':4,'MAY':5,'JUN':6,
          'JUL':7,'AUG':8,'SEP':9,'OCT':10,'NOV':11,'DEC':12}

def datecode_to_iso(dc):
    # dc like '26SEP08' -> 2026-09-08
    yy = int(dc[0:2]); mon = MONTHS[dc[2:5]]; dd = int(dc[5:7])
    return f"20{yy:02d}-{mon:02d}-{dd:02d}"

def load_truth():
    """station, market_type, date -> settlement_temp_f"""
    c = sqlite3.connect(TRUTH)
    rows = c.execute("""
        SELECT station, market_type, local_trading_date, settlement_temp_f
        FROM settlement_truth WHERE settlement_temp_f IS NOT NULL
    """).fetchall()
    c.close()
    m = {}
    for st, mt, d, t in rows:
        m[(st, mt, d)] = t
    return m

def load_snapshots():
    """Return list of (snapshot_time, station, market_ticker, yes_bid, yes_ask, mid_price)"""
    c = sqlite3.connect(SNAP)
    rows = c.execute("""
        SELECT snapshot_time, station, market_ticker, yes_bid, yes_ask, mid_price
        FROM market_snapshots WHERE market_type='binary'
    """).fetchall()
    c.close()
    return rows

def settle_outcome(station, hilo, date_iso, tb, strike, truth):
    """True settlement outcome 1.0 if YES wins, 0.0 if NO wins.
       HIGH market: T = temp > strike. LOW market: B = temp < strike."""
    key = (station, hilo, date_iso)
    temp = truth.get(key)
    if temp is None:
        return None  # no settlement truth
    if hilo == 'HIGH':
        # T = greater than strike (temp > strike). B = less/equal (temp <= strike)
        if tb == 'T':
            return 1.0 if temp > strike else 0.0
        else:  # B
            return 1.0 if temp <= strike else 0.0
    else:  # LOW
        # B = less than strike (temp < strike). T = greater/equal (temp >= strike)
        if tb == 'B':
            return 1.0 if temp < strike else 0.0
        else:  # T
            return 1.0 if temp >= strike else 0.0

def main():
    truth = load_truth()
    snaps = load_snapshots()
    print(f"Loaded {len(truth)} settlement_truth rows, {len(snaps)} binary snapshots")

    # Parse each snapshot row into a contract identity
    # contract -> list of (snapshot_time, bid, ask, mid)
    contracts = defaultdict(list)
    parse_fail = 0
    for stime, station, ticker, bid, ask, mid in snaps:
        m = TICK.match(ticker)
        if not m:
            parse_fail += 1
            continue
        hilo, tstn, dc, tb, strike_s = m.groups()
        strike = float(strike_s)
        date_iso = datecode_to_iso(dc)
        contracts[(station, hilo, date_iso, tb, strike)].append((stime, bid, ask, mid))
    print(f"Distinct contracts: {len(contracts)}, parse_fail: {parse_fail}")

    # For each contract, find the settlement outcome and the ENTRY price
    # (earliest snapshot = time position would open). Use yes_ask as the cost to
    # buy YES (more realistic than mid). Report bid/ask/mid all.
    settled = 0
    unsettled = 0
    results = []  # (station, hilo, date, tb, strike, entry_bid, entry_ask, entry_mid, outcome)

    for (station, hilo, date_iso, tb, strike), snaps_list in contracts.items():
        outcome = settle_outcome(station, hilo, date_iso, tb, strike, truth)
        if outcome is None:
            unsettled += 1
            continue
        settled += 1
        # earliest snapshot = entry
        snaps_list.sort(key=lambda x: x[0])
        stime, bid, ask, mid = snaps_list[0]
        results.append((station, hilo, date_iso, tb, strike, bid, ask, mid, outcome, stime))

    print(f"Settled contracts (with truth): {settled}, unsettled (no truth yet): {unsettled}")
    print()

    # Analyze by contract type
    # Group by (hilo, tb) = the "side"
    sides = defaultdict(list)
    for r in results:
        station, hilo, date, tb, strike, bid, ask, mid, outcome, stime = r
        sides[(hilo, tb)].append(r)

    print("=" * 100)
    print("PER-SIDE ANALYSIS: market entry price vs TRUE settlement outcome")
    print("=" * 100)
    print(f"{'side':<14} {'n':>4} {'win_rate':>9} {'avg_entry_ask':>13} {'avg_entry_bid':>13} {'avg_mid':>9} {'avg_gap(ask)':>12}")
    side_summary = {}
    for (hilo, tb), rows in sorted(sides.items()):
        n = len(rows)
        wins = sum(1 for r in rows if r[8] > 0.5)
        winrate = wins / n if n else 0
        asks = [r[6] for r in rows if r[6] is not None]
        bids = [r[5] for r in rows if r[5] is not None]
        mids = [r[7] for r in rows if r[7] is not None]
        avg_ask = sum(asks)/len(asks) if asks else 0
        avg_bid = sum(bids)/len(bids) if bids else 0
        avg_mid = sum(mids)/len(mids) if mids else 0
        gap_ask = winrate - avg_ask  # positive = underpriced (true P > price)
        side_summary[(hilo, tb)] = dict(n=n, winrate=winrate, avg_ask=avg_ask, avg_bid=avg_bid, avg_mid=avg_mid, gap_ask=gap_ask)
        print(f"{hilo}-{tb:<10} {n:>4} {winrate:>9.3f} {avg_ask:>13.3f} {avg_bid:>13.3f} {avg_mid:>9.3f} {gap_ask:>+12.3f}")

    print()
    print("NOTE on semantics:")
    print("  HIGH-T = YES 'temp ABOVE strike'  (upside in a HIGH market)")
    print("  HIGH-B = YES 'temp BELOW/AT strike' (downside in a HIGH market) <- Dan's 'down side'")
    print("  LOW-B  = YES 'temp BELOW strike'  (downside in a LOW market)")
    print("  LOW-T  = YES 'temp ABOVE/AT strike' (upside in a LOW market)")
    print()

    # The specific question: is the 'down side' (HIGH-B and LOW-B) systematically
    # priced below its true settlement probability?
    print("=" * 100)
    print("DAN'S SPECIFIC QUESTION: is the majority/down side underpriced?")
    print("=" * 100)
    # majority side = the side that wins > 50% of the time
    for label, key in [('HIGH "down" (B = below strike)', ('HIGH','B')),
                        ('HIGH "up"   (T = above strike)', ('HIGH','T')),
                        ('LOW  "down" (B = below strike)', ('LOW','B')),
                        ('LOW  "up"   (T = above strike)', ('LOW','T'))]:
        s = side_summary.get(key)
        if not s:
            print(f"{label}: no data")
            continue
        gap = s['winrate'] - s['avg_ask']
        verdict = "UNDERPRICED (true P > price)" if gap > 0.03 else ("OVERPRICED" if gap < -0.03 else "fairly priced")
        print(f"{label:<32} win_rate={s['winrate']:.3f}  entry_ask={s['avg_ask']:.3f}  gap={gap:+.3f}  -> {verdict}")

    # EV using REAL entry price (ask) with fees
    print()
    print("=" * 100)
    print("NET EV USING REAL ENTRY PRICE (yes_ask as cost to buy YES), with fees")
    print("=" * 100)
    # Kalshi fees: typically ~1-2c per contract or a % of P&L. Use a range.
    FEES = [0.0, 0.01, 0.02, 0.03]  # $/contract fee assumptions
    print(f"{'side':<14} {'n':>4} {'win_rate':>9} {'avg_ask':>8} " + " ".join(f"{'EV@'+str(f):>8}" for f in FEES))
    for (hilo, tb), rows in sorted(sides.items()):
        n = len(rows)
        wins = sum(1 for r in rows if r[8] > 0.5)
        winrate = wins/n if n else 0
        asks = [r[6] for r in rows if r[6] is not None]
        avg_ask = sum(asks)/len(asks) if asks else 0
        evs = []
        for f in FEES:
            # EV per contract = winrate*(1 - ask) - (1-winrate)*ask - fee
            # = winrate - ask - fee
            ev = winrate - avg_ask - f
            evs.append(f"{ev:+.3f}")
        print(f"{hilo}-{tb:<10} {n:>4} {winrate:>9.3f} {avg_ask:>8.3f} " + " ".join(f"{e:>8}" for e in evs))

    print()
    print("=" * 100)
    print("PER-STATION BREAKDOWN for HIGH-B (Dan's 'down side in HIGH')")
    print("=" * 100)
    hb = defaultdict(list)
    for r in results:
        if r[1]=='HIGH' and r[3]=='B':
            hb[r[0]].append(r)
    print(f"{'station':<8} {'n':>4} {'win_rate':>9} {'avg_ask':>8} {'gap':>8}")
    for st in sorted(hb):
        rows = hb[st]
        n=len(rows); wr=sum(1 for r in rows if r[8]>0.5)/n
        asks=[r[6] for r in rows if r[6] is not None]
        aa=sum(asks)/len(asks) if asks else 0
        print(f"{st:<8} {n:>4} {wr:>9.3f} {aa:>8.3f} {wr-aa:>+8.3f}")

if __name__ == '__main__':
    main()
