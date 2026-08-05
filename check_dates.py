import sqlite3
import json

DB_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/paper_trading_dev.db"
conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()
cursor.execute("SELECT DISTINCT target_date FROM trades ORDER BY target_date")
dates = cursor.fetchall()
print("Distinct dates:", [d[0] for d in dates])
print("Number of distinct dates:", len(dates))
cursor.execute("SELECT MIN(target_date), MAX(target_date) FROM trades")
min_date, max_date = cursor.fetchone()
print(f"Date range: {min_date} to {max_date}")
conn.close()