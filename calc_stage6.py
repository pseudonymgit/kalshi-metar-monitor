import sqlite3
import json
import math
import numpy as np
from collections import defaultdict

DB_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/paper_trading_dev.db"
conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

# Fetch all trades
cursor.execute("""
    SELECT station, target_date, pred_direction, actual_direction, confidence, 
           entry_price, market_price, edge, contracts, correct, net_pnl, bankroll_after
    FROM trades
    ORDER BY target_date, station
""")
rows = cursor.fetchall()
print(f"Total trades: {len(rows)}")

# Compute overall accuracy
correct = sum(1 for row in rows if row[9] == 1)
accuracy = correct / len(rows) if rows else 0
print(f"Correct trades: {correct}")
print(f"Overall directional accuracy: {accuracy:.4f} ({accuracy*100:.2f}%)")

# Compute confidence intervals for overall accuracy (binomial proportion CI)
# Using normal approximation 95% CI: p ± 1.96 * sqrt(p*(1-p)/n)
if rows:
    n = len(rows)
    p = accuracy
    se = math.sqrt(p * (1 - p) / n)
    ci_lower = p - 1.96 * se
    ci_upper = p + 1.96 * se
    print(f"95% CI for accuracy: [{ci_lower:.4f}, {ci_upper:.4f}] ({ci_lower*100:.2f}% - {ci_upper*100:.2f}%)")

# Per-station stats
station_stats = defaultdict(lambda: {'trades': 0, 'correct': 0, 'pnl': 0.0, 'contracts': 0, 'edges': []})
for row in rows:
    station = row[0]
    stat = station_stats[station]
    stat['trades'] += 1
    if row[9] == 1:
        stat['correct'] += 1
    stat['pnl'] += row[10]
    stat['contracts'] += row[8]
    stat['edges'].append(row[7])

print("\nPer-station tradability audit:")
print("Station | Trades | Accuracy | P&L | Contracts | Edge mean | Edge std | Sharpe*")
print("--------|--------|----------|-----|-----------|-----------|----------|--------")
for station in sorted(station_stats.keys()):
    s = station_stats[station]
    acc = s['correct'] / s['trades'] if s['trades'] > 0 else 0
    edge_mean = np.mean(s['edges']) if s['edges'] else 0
    edge_std = np.std(s['edges'], ddof=1) if len(s['edges']) > 1 else 0
    # Sharpe approximation: edge_mean / edge_std * sqrt(trades)
    sharpe = (edge_mean / edge_std * math.sqrt(s['trades'])) if edge_std > 0 else 0
    print(f"{station:6} | {s['trades']:6} | {acc*100:6.2f}% | ${s['pnl']:6.2f} | {s['contracts']:9} | {edge_mean:.4f} | {edge_std:.4f} | {sharpe:.3f}")

# Flag stations below 55% accuracy
print("\nStations below 55% accuracy (track, don't discard):")
low_acc_stations = []
for station, s in station_stats.items():
    acc = s['correct'] / s['trades'] if s['trades'] > 0 else 0
    if acc < 0.55:
        low_acc_stations.append((station, acc))
        print(f"  {station}: {acc*100:.2f}% ({s['trades']} trades)")

# Aggregate P&L, Sharpe, drawdown
daily_pnl = defaultdict(float)
for row in rows:
    date = row[1]
    daily_pnl[date] += row[10]

daily_returns = []
bankroll = 10000.0  # initial
cumulative = bankroll
peak = bankroll
max_drawdown = 0.0
for date in sorted(daily_pnl.keys()):
    pnl = daily_pnl[date]
    ret = pnl / cumulative if cumulative != 0 else 0
    daily_returns.append(ret)
    cumulative += pnl
    peak = max(peak, cumulative)
    drawdown = (peak - cumulative) / peak if peak > 0 else 0
    max_drawdown = max(max_drawdown, drawdown)

# Sharpe (annualized)
if len(daily_returns) > 1:
    mean = np.mean(daily_returns)
    std = np.std(daily_returns, ddof=1)
    sharpe = (mean / std * math.sqrt(252)) if std > 0 else 0
else:
    sharpe = 0
print(f"\nAggregate metrics:")
print(f"  Total P&L: ${sum(daily_pnl.values()):.2f}")
print(f"  Daily Sharpe (annualized): {sharpe:.4f}")
print(f"  Max drawdown: {max_drawdown*100:.2f}%")

# Check gates
print("\nGo/No-Go Gate Status:")
# 1. 30 days paper testing — status?
print("1. 30 days paper testing: ❌ Not yet completed (only 5 days of trading data)")
# 2. ≥60% directional accuracy — status?
if accuracy >= 0.60:
    print(f"2. ≥60% directional accuracy: ✅ PASS ({accuracy*100:.2f}%)")
else:
    print(f"2. ≥60% directional accuracy: ❌ FAIL ({accuracy*100:.2f}%)")
# 3. ≥0.30 Sharpe — status?
if sharpe >= 0.30:
    print(f"3. ≥0.30 Sharpe: ✅ PASS ({sharpe:.4f})")
else:
    print(f"3. ≥0.30 Sharpe: ❌ FAIL ({sharpe:.4f})")
# 4. No >10% single-day drawdown — status?
# compute max single-day loss as percentage of bankroll
max_single_day_loss_pct = 0.0
cumulative = bankroll
for date in sorted(daily_pnl.keys()):
    pnl = daily_pnl[date]
    loss_pct = -pnl / cumulative if cumulative != 0 else 0
    if loss_pct > max_single_day_loss_pct:
        max_single_day_loss_pct = loss_pct
    cumulative += pnl
if max_single_day_loss_pct <= 0.10:
    print(f"4. No >10% single-day drawdown: ✅ PASS (max single-day loss: {max_single_day_loss_pct*100:.2f}%)")
else:
    print(f"4. No >10% single-day drawdown: ❌ FAIL (max single-day loss: {max_single_day_loss_pct*100:.2f}%)")
# 5. Settlement-confirmed accuracy (≥1,000 trades) — status?
if len(rows) >= 1000:
    print(f"5. Settlement-confirmed accuracy (≥1,000 trades): ✅ PASS ({len(rows)} trades)")
else:
    print(f"5. Settlement-confirmed accuracy (≥1,000 trades): ❌ FAIL ({len(rows)} trades)")

conn.close()