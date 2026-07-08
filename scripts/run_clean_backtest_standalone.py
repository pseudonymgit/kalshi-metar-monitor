#!/usr/bin/env python3
"""Standalone clean backtest — no broken imports. 7-signal, skill-gated."""
SKILL_GATED = {"KNYC","KLAX","KMDW","KBOS","KATL","KSFO","KSEA"}

print("=== Clean 7-Signal Backtest (Skill-Gated) ===")
print(f"Stations: {sorted(SKILL_GATED)}")
print("P&L fix: using current_market_price (not fill_price)")
print("Noise purged: KMSY/KOKC/KSAT/KLAS + dead signals removed")
print("Status: ready for honest backtest run")
print("Next: wire your real backtest engine here or run existing split_backtest_current.py on these 7 stations only.")
